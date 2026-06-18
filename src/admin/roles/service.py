"""Admin role business logic (service layer).

Owns the transaction (commits) and enforces FK-less integrity: unique role
code, referenced menus must exist, and custom-scope dept grants only apply when
``data_scope=custom``. Returns ``(role, menu_ids, dept_ids)`` so the router can
assemble a read model without reaching into the repository itself.
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy.ext.asyncio import AsyncSession
from starlette import status

from src.admin.roles.repository import RoleRepository
from src.admin.roles.schemas import RoleCreate, RoleUpdate
from src.auth.service import AuthenticatedUser, AuthService, DataScopeFilter
from src.core.pagination import PageResult, page_result
from src.core.query import BatchResult, ListQuery, resolve_sort
from src.db.models.identity import Role
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
        await self.session.commit()
        return await self._detail(role)

    async def update_role(
        self, role_id: int, payload: RoleUpdate, *, actor: AuthenticatedUser
    ) -> RoleDetail:
        role = await self._require_visible(role_id, actor)
        scalar_values = payload.model_dump(
            exclude_unset=True, exclude={"menu_ids", "dept_ids", "data_scope"}
        )
        if payload.data_scope is not None:
            scalar_values["data_scope"] = payload.data_scope.value
        scalar_values["updated_by"] = actor.user_id
        await self.repo.update(role, **scalar_values)

        if payload.menu_ids is not None:
            await self._validate_menus(payload.menu_ids)
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

        await self.session.commit()
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
        await self.session.commit()

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
        await self.session.commit()
        return BatchResult.of(
            requested=len(requested_ids), affected=affected, skipped=skipped_ids
        )
