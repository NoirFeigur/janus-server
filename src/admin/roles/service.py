"""Admin role business logic (service layer).

Enforces FK-less integrity: unique role code, referenced menus must exist, and
custom-scope dept grants only apply when ``data_scope=custom``. Returns
``(role, menu_ids, dept_ids)`` so the router can assemble a read model without
reaching into the repository itself.

The transaction is owned by the request-level Unit of Work, not here: this layer
only ``flush()``es so snowflake ids/association rows materialize for the read
model it returns; the request edge commits (or rolls back) atomically.
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy.ext.asyncio import AsyncSession
from starlette import status

from src.admin.roles.repository import RoleRepository
from src.admin.roles.schemas import RoleCreate, RoleUpdate
from src.auth import perm_cache
from src.auth.constants import SUPERADMIN_ROLE_CODE
from src.auth.service import AuthenticatedUser, AuthService, DataScopeFilter
from src.core.pagination import PageResult, page_result
from src.core.query import BatchResult, ListQuery, resolve_sort
from src.db.models.identity import Role
from src.db.session import add_after_commit_hook
from src.enums import DataScope, ErrorCode
from src.exceptions import AppError

RoleDetail = tuple[Role, list[int], list[int]]
RolePage = PageResult[RoleDetail]

ROLE_SORT_COLUMNS = {
    "id": Role.id,
    "name": Role.name,
    "code": Role.code,
    "sort_order": Role.sort_order,
    "status": Role.status,
    "created_at": Role.created_at,
}


class RoleService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.repo = RoleRepository(session)
        self.auth = AuthService(session)

    async def _scope(self, actor: AuthenticatedUser) -> DataScopeFilter:
        return await self.auth.resolve_data_scope(actor)

    async def _require(self, role_id: int) -> Role:
        role = await self.repo.get(role_id)
        if role is None:
            raise AppError(ErrorCode.request_invalid, status.HTTP_404_NOT_FOUND)
        return role

    async def _require_visible(self, role_id: int, actor: AuthenticatedUser) -> Role:
        role = await self._require(role_id)
        scope = await self._scope(actor)
        if not self.repo.is_visible(role, scope, actor_id=actor.user_id):
            raise AppError(ErrorCode.auth_forbidden, status.HTTP_403_FORBIDDEN)
        return role

    async def _validate_menus(self, menu_ids: Sequence[int]) -> None:
        if not menu_ids:
            return
        existing = await self.repo.existing_menu_ids(menu_ids)
        if existing != set(menu_ids):
            raise AppError(ErrorCode.request_invalid, status.HTTP_400_BAD_REQUEST)

    async def _validate_depts(self, dept_ids: Sequence[int]) -> None:
        if not dept_ids:
            return
        existing = await self.repo.existing_dept_ids(dept_ids)
        if existing != set(dept_ids):
            raise AppError(ErrorCode.request_invalid, status.HTTP_400_BAD_REQUEST)

    def _require_assignable_code(self, code: str, actor: AuthenticatedUser) -> None:
        """Reject minting/renaming a role into the reserved ``superadmin`` code.

        Super-admin status is code-based (``AuthenticatedUser.is_superuser`` tests
        the role code, not a perm chain), so a role carrying the ``superadmin``
        code IS the super-admin grant. Only an existing super-admin may create one
        — otherwise a scoped admin could self-mint a no-menu ``superadmin`` role
        (which confers zero perms, so the menu-subset guard waves it through) and
        then assign it, escalating to full super-admin.
        """
        if actor.is_superuser:
            return
        if code == SUPERADMIN_ROLE_CODE:
            raise AppError(ErrorCode.auth_forbidden, status.HTTP_403_FORBIDDEN)

    async def _require_assignable_menus(
        self, menu_ids: Sequence[int], actor: AuthenticatedUser
    ) -> None:
        """Privilege-escalation guard for a role's menu (permission) grants.

        Mirror of the user-assignment guard: an actor may only put menus on a
        role whose conferred permission codes are a subset of the actor's own.
        Super-admin may grant anything; everyone else cannot mint a permission
        they lack onto a role (so a non-admin cannot fabricate a ``*:*:*`` role
        and then assign it). Empty grant is trivially allowed.
        """
        if actor.is_superuser or not menu_ids:
            return
        conferred = await self.auth.permissions_for_menus(menu_ids)
        if not conferred.issubset(actor.permissions):
            raise AppError(ErrorCode.auth_forbidden, status.HTTP_403_FORBIDDEN)

    async def _require_assignable_scope(
        self,
        data_scope: DataScope,
        dept_ids: Sequence[int],
        actor: AuthenticatedUser,
    ) -> None:
        """Privilege-escalation guard for a role's data-scope breadth.

        An unrestricted actor (super-admin or an ``all`` role) may grant any
        scope. For a scoped actor the prospective grant is resolved against the
        actor itself as the stand-in holder (the role is not yet bound to a user),
        and the resulting department visibility must be a subset of the actor's
        own. ``all`` is refused outright. ``self_only`` confers no department
        visibility and always passes.

        Treating the actor as the holder is sound because the per-assignment guard
        (:meth:`UserService._require_assignable_roles`) re-resolves the scope
        against the REAL holder when the role is later assigned — so a relative
        scope that happens to be safe for the actor-as-holder but broader for some
        other holder is still caught at assignment time.
        """
        actor_scope = await self._scope(actor)
        if actor_scope.unrestricted:
            return
        granted = await self.auth.resolve_role_scope(
            data_scope, dept_ids, holder_department_id=actor.department_id
        )
        if granted.unrestricted or not granted.department_ids.issubset(
            actor_scope.department_ids
        ):
            raise AppError(ErrorCode.auth_forbidden, status.HTTP_403_FORBIDDEN)

    async def _detail(self, role: Role) -> RoleDetail:
        menu_ids = await self.repo.list_menu_ids(role.id)
        dept_ids = await self.repo.list_dept_ids(role.id)
        return role, menu_ids, dept_ids

    async def _require_role_dominance(
        self, role: Role, actor: AuthenticatedUser
    ) -> None:
        """Reject editing/deleting a role that outranks the actor (dominance).

        ``_require_visible`` only answers "may the actor SEE this role"; it does
        NOT answer "may the actor MANAGE it". Without this guard a scoped admin
        holding ``system:role:edit/remove`` could delete or rewrite a role more
        powerful than any it could create — including the ``superadmin`` role
        itself — as long as the role fell inside its data scope.

        The dominance test is the role-creation escalation test applied to the
        role's CURRENT shape: an actor may manage a role only if it could have
        MINTED that role itself. This reuses the same three guards used on
        create/update (reserved-code + menu-perm subset + data-scope breadth), so
        a role carrying ``superadmin``, an ``all`` scope, or any permission the
        actor lacks is unmanageable. Super-admin actors pass trivially.
        """
        if actor.is_superuser:
            return
        self._require_assignable_code(role.code, actor)
        menu_ids = await self.repo.list_menu_ids(role.id)
        await self._require_assignable_menus(menu_ids, actor)
        dept_ids = await self.repo.list_dept_ids(role.id)
        await self._require_assignable_scope(
            DataScope(role.data_scope), dept_ids, actor
        )

    async def _dominates_role(
        self,
        role: Role,
        menu_ids: Sequence[int],
        dept_ids: Sequence[int],
        actor: AuthenticatedUser,
    ) -> bool:
        """Boolean form of :meth:`_require_role_dominance` for bulk pre-filtering.

        Takes pre-fetched menu/dept ids (one bulk lookup for the whole batch, no
        1+N) and returns a verdict instead of raising — so a batch quietly skips
        roles the actor cannot manage. Super-admin dominates everything.
        """
        if actor.is_superuser:
            return True
        try:
            self._require_assignable_code(role.code, actor)
            await self._require_assignable_menus(menu_ids, actor)
            await self._require_assignable_scope(
                DataScope(role.data_scope), dept_ids, actor
            )
        except AppError:
            return False
        return True

    async def list_roles(
        self,
        actor: AuthenticatedUser,
        *,
        query: ListQuery | None = None,
    ) -> RolePage:
        query = query or ListQuery()
        scope = await self._scope(actor)
        sort = resolve_sort(query, allowed=ROLE_SORT_COLUMNS, default="sort_order")
        total = await self.repo.count_in_scope(
            scope, actor_id=actor.user_id, keyword=query.keyword
        )
        roles = await self.repo.list_in_scope(
            scope,
            actor_id=actor.user_id,
            keyword=query.keyword,
            sort=sort,
            limit=query.limit,
            offset=query.offset,
        )
        # Two bulk lookups for the whole page (was 1+2R: two queries per role).
        role_ids = [role.id for role in roles]
        menu_map = await self.repo.list_menu_ids_for_roles(role_ids)
        dept_map = await self.repo.list_dept_ids_for_roles(role_ids)
        items = [
            (role, menu_map.get(role.id, []), dept_map.get(role.id, []))
            for role in roles
        ]
        return page_result(items, total=total, limit=query.limit, offset=query.offset)

    async def get_role(self, role_id: int, actor: AuthenticatedUser) -> RoleDetail:
        return await self._detail(await self._require_visible(role_id, actor))

    async def create_role(self, payload: RoleCreate, *, actor: AuthenticatedUser) -> RoleDetail:
        if await self.repo.get_by_code(payload.code) is not None:
            raise AppError(ErrorCode.request_invalid, status.HTTP_400_BAD_REQUEST)
        # The ``superadmin`` code is the *identity* of super-admin (is_superuser is
        # code-based, not perm-based), so minting a role with it is minting the
        # super-admin grant itself. A non-super-admin must never do this: the
        # menu/scope escalation guards below are blind to it (a no-menu superadmin
        # role confers zero perms, trivially passing the subset check), so a scoped
        # admin could otherwise self-create a superadmin role and assign it. Only a
        # super-admin may create a super-admin-coded role.
        self._require_assignable_code(payload.code, actor)
        await self._validate_menus(payload.menu_ids)
        await self._require_assignable_menus(payload.menu_ids, actor)
        await self._require_assignable_scope(
            payload.data_scope, payload.dept_ids, actor
        )
        role = Role(
            name=payload.name,
            code=payload.code,
            data_scope=payload.data_scope.value,
            sort_order=payload.sort_order,
            status=payload.status.value,
            remark=payload.remark,
            created_by=actor.user_id,
            create_dept=actor.department_id,
            updated_by=actor.user_id,
        )
        await self.repo.create(role)
        await self.repo.replace_menus(role.id, payload.menu_ids)
        if payload.data_scope == DataScope.custom:
            await self._validate_depts(payload.dept_ids)
            await self.repo.replace_depts(role.id, payload.dept_ids)
        await self.session.flush()
        return await self._detail(role)

    async def update_role(
        self, role_id: int, payload: RoleUpdate, *, actor: AuthenticatedUser
    ) -> RoleDetail:
        role = await self._require_visible(role_id, actor)

        # Dominance: the actor must already out-rank the role's CURRENT shape
        # before it may rewrite it (else a scoped admin could edit a role more
        # powerful than any it could mint). Runs before the new-value guards.
        await self._require_role_dominance(role, actor)

        # Privilege-escalation guards run BEFORE any mutation so a rejected edit
        # leaves no partial flush behind.
        if payload.menu_ids is not None:
            await self._validate_menus(payload.menu_ids)
            await self._require_assignable_menus(payload.menu_ids, actor)
        if payload.data_scope is not None or payload.dept_ids is not None:
            effective = (
                payload.data_scope
                if payload.data_scope is not None
                else DataScope(role.data_scope)
            )
            # When the effective scope is custom but the payload doesn't carry new
            # dept_ids (e.g. editing only data_scope or menus), the guard must see
            # the role's CURRENT dept grants — using [] would let a scope change
            # past the breadth check while the existing custom depts silently
            # persist (and exceed the actor's scope).
            if payload.dept_ids is not None:
                guard_depts: Sequence[int] = payload.dept_ids
            elif effective == DataScope.custom:
                guard_depts = await self.repo.list_dept_ids(role.id)
            else:
                guard_depts = []
            await self._require_assignable_scope(effective, guard_depts, actor)

        scalar_values = payload.model_dump(
            exclude_unset=True, exclude={"menu_ids", "dept_ids", "data_scope"}
        )
        if payload.data_scope is not None:
            scalar_values["data_scope"] = payload.data_scope.value
        scalar_values["updated_by"] = actor.user_id
        await self.repo.update(role, **scalar_values)

        if payload.menu_ids is not None:
            await self.repo.replace_menus(role_id, payload.menu_ids)

        # Effective scope after this update decides whether dept grants persist.
        effective_scope = (
            payload.data_scope.value if payload.data_scope is not None else role.data_scope
        )
        if effective_scope == DataScope.custom.value:
            if payload.dept_ids is not None:
                await self._validate_depts(payload.dept_ids)
                await self.repo.replace_depts(role_id, payload.dept_ids)
        else:
            # Leaving custom scope clears any stale custom dept grants.
            await self.repo.replace_depts(role_id, [])

        await self.session.flush()
        # A role's status or menu (permission) bindings changing alters the
        # conferred permission/role codes for EVERY holder of this role. Rather
        # than enumerate holders (a RoleMenu⋈UserRole walk over potentially many
        # users), bump the global generation once — one INCR invalidates the whole
        # population's perm snapshots after commit. Data-scope/dept changes do NOT
        # touch the perm cache (they feed resolve_data_scope / dept_tree_cache).
        if payload.status is not None or payload.menu_ids is not None:
            add_after_commit_hook(self.session, perm_cache.invalidate_all)
        return await self._detail(role)

    async def delete_role(self, role_id: int, *, actor: AuthenticatedUser) -> None:
        role = await self._require_visible(role_id, actor)
        await self._require_role_dominance(role, actor)
        # Drop all association rows (physical) — including user assignments so no
        # stale role id lingers on users — then soft-delete the role itself.
        await self.repo.replace_menus(role_id, [])
        await self.repo.replace_depts(role_id, [])
        await self.repo.delete_user_links(role_id)
        role.updated_by = actor.user_id
        await self.repo.soft_delete(role)
        await self.session.flush()
        # Soft-deleting a role removes it from every former holder's role/perm
        # codes — invalidate all snapshots after commit (global gen bump).
        add_after_commit_hook(self.session, perm_cache.invalidate_all)

    async def batch_delete_roles(
        self, ids: Sequence[int], *, actor: AuthenticatedUser
    ) -> BatchResult:
        requested_ids = list(dict.fromkeys(ids))
        scope = await self._scope(actor)

        # Dominance pre-filter (bulk): a scoped actor may only delete roles it
        # could have minted itself — same guard the single delete enforces, so an
        # outranking role (e.g. ``superadmin``) is skipped, not swept out. Loads
        # the page's roles + menu/dept grants once each (no 1+N). Super-admin
        # dominates everything (skip the lookups entirely).
        dominance_skipped: list[int] = []
        deletable_ids = requested_ids
        if not actor.is_superuser:
            roles = await self.repo.list_by_ids(requested_ids)
            role_by_id = {role.id: role for role in roles}
            menu_map = await self.repo.list_menu_ids_for_roles(requested_ids)
            dept_map = await self.repo.list_dept_ids_for_roles(requested_ids)
            deletable_ids = []
            for role_id in requested_ids:
                role = role_by_id.get(role_id)
                if role is not None and await self._dominates_role(
                    role,
                    menu_map.get(role_id, []),
                    dept_map.get(role_id, []),
                    actor,
                ):
                    deletable_ids.append(role_id)
                else:
                    dominance_skipped.append(role_id)

        affected, scope_skipped_ids = await self.repo.soft_delete_many(
            deletable_ids,
            scope_predicate=self.repo._scope_predicate(scope, actor_id=actor.user_id),
        )
        skipped_ids = scope_skipped_ids + dominance_skipped
        skipped = set(skipped_ids)
        for role_id in requested_ids:
            if role_id not in skipped:
                await self.repo.replace_menus(role_id, [])
                await self.repo.replace_depts(role_id, [])
                await self.repo.delete_user_links(role_id)
        await self.session.flush()
        # Any role actually deleted affects its holders' codes; one global bump
        # covers the whole batch (skip the hook if nothing was deleted).
        if affected > 0:
            add_after_commit_hook(self.session, perm_cache.invalidate_all)
        return BatchResult.of(
            requested=len(requested_ids), affected=affected, skipped=skipped_ids
        )
