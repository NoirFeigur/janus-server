"""Admin role business logic (service layer).

Enforces FK-less integrity: unique role code and referenced menus must exist.
Returns ``(role, menu_ids)`` so the router can assemble a read model without
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
from src.auth.service import AuthenticatedUser, AuthService
from src.core.pagination import PageResult, page_result
from src.core.query import BatchResult, ListQuery, resolve_sort
from src.db.models.identity import Role
from src.db.session import add_after_commit_hook
from src.enums import ErrorCode
from src.exceptions import AppError

RoleDetail = tuple[Role, list[int]]
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

    async def _require(self, role_id: int) -> Role:
        role = await self.repo.get(role_id)
        if role is None:
            raise AppError(ErrorCode.request_invalid, status.HTTP_404_NOT_FOUND)
        return role

    async def _validate_menus(self, menu_ids: Sequence[int]) -> None:
        if not menu_ids:
            return
        existing = await self.repo.existing_menu_ids(menu_ids)
        if existing != set(menu_ids):
            raise AppError(ErrorCode.request_invalid, status.HTTP_400_BAD_REQUEST)

    def _require_assignable_code(self, code: str, actor: AuthenticatedUser) -> None:
        """Reject minting/renaming a role into the reserved ``superadmin`` code.

        Super-admin status is code-based (``AuthenticatedUser.is_superuser`` tests
        the role code, not a perm chain), so a role carrying the ``superadmin``
        code IS the super-admin grant. Only an existing super-admin may create one
        — otherwise an admin could self-mint a no-menu ``superadmin`` role (which
        confers zero perms, so the menu-subset guard waves it through) and then
        assign it, escalating to full super-admin.
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

    async def _detail(self, role: Role) -> RoleDetail:
        menu_ids = await self.repo.list_menu_ids(role.id)
        return role, menu_ids

    async def _require_role_dominance(
        self, role: Role, actor: AuthenticatedUser
    ) -> None:
        """Reject editing/deleting a role that outranks the actor (dominance).

        ``_require`` only answers "does this role exist"; it does NOT answer "may
        the actor MANAGE it". Without this guard an admin holding
        ``system:role:edit/remove`` could delete or rewrite a role more powerful
        than any it could create — including the ``superadmin`` role itself.

        The dominance test is the role-creation escalation test applied to the
        role's CURRENT shape: an actor may manage a role only if it could have
        MINTED that role itself. This reuses the same two guards used on
        create/update (reserved-code + menu-perm subset), so a role carrying
        ``superadmin`` or any permission the actor lacks is unmanageable.
        Super-admin actors pass trivially.
        """
        if actor.is_superuser:
            return
        self._require_assignable_code(role.code, actor)
        menu_ids = await self.repo.list_menu_ids(role.id)
        await self._require_assignable_menus(menu_ids, actor)

    async def _dominates_role(
        self,
        role: Role,
        menu_ids: Sequence[int],
        actor: AuthenticatedUser,
    ) -> bool:
        """Boolean form of :meth:`_require_role_dominance` for bulk pre-filtering.

        Takes pre-fetched menu ids (one bulk lookup for the whole batch, no 1+N)
        and returns a verdict instead of raising — so a batch quietly skips roles
        the actor cannot manage. Super-admin dominates everything.
        """
        if actor.is_superuser:
            return True
        try:
            self._require_assignable_code(role.code, actor)
            await self._require_assignable_menus(menu_ids, actor)
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
        sort = resolve_sort(query, allowed=ROLE_SORT_COLUMNS, default="sort_order")
        total = await self.repo.count_roles(keyword=query.keyword)
        roles = await self.repo.list_roles(
            keyword=query.keyword,
            sort=sort,
            limit=query.limit,
            offset=query.offset,
        )
        # One bulk lookup for the whole page (was 1+R: one query per role).
        role_ids = [role.id for role in roles]
        menu_map = await self.repo.list_menu_ids_for_roles(role_ids)
        items = [(role, menu_map.get(role.id, [])) for role in roles]
        return page_result(items, total=total, limit=query.limit, offset=query.offset)

    async def get_role(self, role_id: int, actor: AuthenticatedUser) -> RoleDetail:
        return await self._detail(await self._require(role_id))

    async def create_role(
        self, payload: RoleCreate, *, actor: AuthenticatedUser
    ) -> RoleDetail:
        if await self.repo.get_by_code(payload.code) is not None:
            raise AppError(ErrorCode.request_invalid, status.HTTP_400_BAD_REQUEST)
        # The ``superadmin`` code is the *identity* of super-admin (is_superuser is
        # code-based, not perm-based), so minting a role with it is minting the
        # super-admin grant itself. A non-super-admin must never do this: the
        # menu escalation guard below is blind to it (a no-menu superadmin role
        # confers zero perms, trivially passing the subset check), so an admin
        # could otherwise self-create a superadmin role and assign it. Only a
        # super-admin may create a super-admin-coded role.
        self._require_assignable_code(payload.code, actor)
        await self._validate_menus(payload.menu_ids)
        await self._require_assignable_menus(payload.menu_ids, actor)
        role = Role(
            name=payload.name,
            code=payload.code,
            sort_order=payload.sort_order,
            status=payload.status.value,
            remark=payload.remark,
            created_by=actor.user_id,
            create_dept=actor.department_id,
            updated_by=actor.user_id,
        )
        await self.repo.create(role)
        await self.repo.replace_menus(role.id, payload.menu_ids)
        await self.session.flush()
        return await self._detail(role)

    async def update_role(
        self, role_id: int, payload: RoleUpdate, *, actor: AuthenticatedUser
    ) -> RoleDetail:
        role = await self._require(role_id)

        # Dominance: the actor must already out-rank the role's CURRENT shape
        # before it may rewrite it (else an admin could edit a role more powerful
        # than any it could mint). Runs before the new-value guards.
        await self._require_role_dominance(role, actor)

        # Privilege-escalation guards run BEFORE any mutation so a rejected edit
        # leaves no partial flush behind.
        if payload.menu_ids is not None:
            await self._validate_menus(payload.menu_ids)
            await self._require_assignable_menus(payload.menu_ids, actor)

        scalar_values = payload.model_dump(
            exclude_unset=True, exclude={"menu_ids"}
        )
        scalar_values["updated_by"] = actor.user_id
        await self.repo.update(role, **scalar_values)

        if payload.menu_ids is not None:
            await self.repo.replace_menus(role_id, payload.menu_ids)

        await self.session.flush()
        # A role's status or menu (permission) bindings changing alters the
        # conferred permission/role codes for EVERY holder of this role. Rather
        # than enumerate holders (a RoleMenu⋈UserRole walk over potentially many
        # users), bump the global generation once — one INCR invalidates the whole
        # population's perm snapshots after commit.
        if payload.status is not None or payload.menu_ids is not None:
            add_after_commit_hook(self.session, perm_cache.invalidate_all)
        return await self._detail(role)

    async def delete_role(self, role_id: int, *, actor: AuthenticatedUser) -> None:
        role = await self._require(role_id)
        await self._require_role_dominance(role, actor)
        # Drop all association rows (physical) — including user assignments so no
        # stale role id lingers on users — then soft-delete the role itself.
        await self.repo.replace_menus(role_id, [])
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

        # Dominance pre-filter (bulk): a scoped actor may only delete roles it
        # could have minted itself — same guard the single delete enforces, so an
        # outranking role (e.g. ``superadmin``) is skipped, not swept out. Loads
        # the page's roles + menu grants once each (no 1+N). Super-admin dominates
        # everything (skip the lookups entirely).
        dominance_skipped: list[int] = []
        deletable_ids = requested_ids
        if not actor.is_superuser:
            roles = await self.repo.list_by_ids(requested_ids)
            role_by_id = {role.id: role for role in roles}
            menu_map = await self.repo.list_menu_ids_for_roles(requested_ids)
            deletable_ids = []
            for role_id in requested_ids:
                role = role_by_id.get(role_id)
                if role is not None and await self._dominates_role(
                    role,
                    menu_map.get(role_id, []),
                    actor,
                ):
                    deletable_ids.append(role_id)
                else:
                    dominance_skipped.append(role_id)

        affected, missing_skipped_ids = await self.repo.soft_delete_many(deletable_ids)
        skipped_ids = missing_skipped_ids + dominance_skipped
        skipped = set(skipped_ids)
        for role_id in requested_ids:
            if role_id not in skipped:
                await self.repo.replace_menus(role_id, [])
                await self.repo.delete_user_links(role_id)
        await self.session.flush()
        # Any role actually deleted affects its holders' codes; one global bump
        # covers the whole batch (skip the hook if nothing was deleted).
        if affected > 0:
            add_after_commit_hook(self.session, perm_cache.invalidate_all)
        return BatchResult.of(
            requested=len(requested_ids), affected=affected, skipped=skipped_ids
        )
