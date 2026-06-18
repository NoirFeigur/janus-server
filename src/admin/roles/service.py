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
        scope. A scoped actor may not:
        - grant ``all`` (would mint a role strictly broader than the actor's own
          visibility), nor
        - grant ``custom`` departments outside their own visible department set
          (no write-outside-scope hole).
        Relative scopes (``dept`` / ``dept_and_child`` / ``self`` /
        ``dept_and_child_or_self``) are bounded by the eventual role holder's own
        position, so they confer no breadth the actor lacks and are allowed.
        """
        actor_scope = await self._scope(actor)
        if actor_scope.unrestricted:
            return
        if data_scope == DataScope.all_data:
            raise AppError(ErrorCode.auth_forbidden, status.HTTP_403_FORBIDDEN)
        if data_scope == DataScope.custom and not set(dept_ids).issubset(
            actor_scope.department_ids
        ):
            raise AppError(ErrorCode.auth_forbidden, status.HTTP_403_FORBIDDEN)

    async def _detail(self, role: Role) -> RoleDetail:
        menu_ids = await self.repo.list_menu_ids(role.id)
        dept_ids = await self.repo.list_dept_ids(role.id)
        return role, menu_ids, dept_ids

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
            guard_depts = payload.dept_ids if payload.dept_ids is not None else []
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
        affected, skipped_ids = await self.repo.soft_delete_many(
            requested_ids,
            scope_predicate=self.repo._scope_predicate(scope, actor_id=actor.user_id),
        )
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
