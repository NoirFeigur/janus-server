"""Admin model-grant business logic (service layer)."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession
from starlette import status

from src.admin.grants.repository import GrantRepository
from src.admin.grants.schemas import GrantCreate, GrantUpdate
from src.auth.service import AuthenticatedUser, AuthService, DataScopeFilter
from src.core.pagination import PageResult, page_result
from src.core.query import ListQuery, resolve_sort
from src.db.models.grant import UserModelGrant
from src.enums import ErrorCode
from src.exceptions import AppError

SORT_COLUMNS = {
    "id": UserModelGrant.id,
    "scope": UserModelGrant.scope,
    "scope_id": UserModelGrant.scope_id,
    "logical_model_id": UserModelGrant.logical_model_id,
    "is_default": UserModelGrant.is_default,
    "created_at": UserModelGrant.created_at,
}


class GrantService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.grants = GrantRepository(session)
        self.auth = AuthService(session)

    async def _scope(self, actor: AuthenticatedUser) -> DataScopeFilter:
        return await self.auth.resolve_data_scope(actor)

    async def _require_grant(self, grant_id: int) -> UserModelGrant:
        grant = await self.grants.get(grant_id)
        if grant is None:
            raise AppError(ErrorCode.request_invalid, status.HTTP_404_NOT_FOUND)
        return grant

    async def _require_subject_in_scope(
        self, *, scope: str, scope_id: int, actor: AuthenticatedUser
    ) -> None:
        scope_filter = await self._scope(actor)
        if not await self.grants.subject_in_scope(
            scope=scope,
            scope_id=scope_id,
            scope_filter=scope_filter,
            actor_id=actor.user_id,
        ):
            raise AppError(ErrorCode.auth_forbidden, status.HTTP_403_FORBIDDEN)

    async def _require_visible_grant(
        self, grant_id: int, actor: AuthenticatedUser
    ) -> UserModelGrant:
        grant = await self._require_grant(grant_id)
        await self._require_subject_in_scope(
            scope=grant.scope, scope_id=grant.scope_id, actor=actor
        )
        return grant

    async def _clear_default(
        self, *, scope: str, scope_id: int, actor: AuthenticatedUser
    ) -> None:
        current = await self.grants.get_default(
            scope=scope, scope_id=scope_id, for_update=True
        )
        if current is None:
            return
        await self.grants.update(
            current,
            is_default=False,
            updated_by=actor.user_id,
        )

    async def list_grants(
        self,
        *,
        scope: str | None = None,
        scope_id: int | None = None,
        logical_model_id: int | None = None,
        query: ListQuery | None = None,
        actor: AuthenticatedUser,
    ) -> PageResult[UserModelGrant]:
        query = query or ListQuery()
        scope_filter = await self._scope(actor)
        sort = resolve_sort(query, allowed=SORT_COLUMNS, default="created_at")
        total = await self.grants.count_grants(
            keyword=query.keyword,
            scope=scope,
            scope_id=scope_id,
            logical_model_id=logical_model_id,
            scope_filter=scope_filter,
            actor_id=actor.user_id,
        )
        items = await self.grants.list_grants(
            keyword=query.keyword,
            scope=scope,
            scope_id=scope_id,
            logical_model_id=logical_model_id,
            scope_filter=scope_filter,
            actor_id=actor.user_id,
            sort=sort,
            limit=query.limit,
            offset=query.offset,
        )
        return page_result(
            list(items), total=total, limit=query.limit, offset=query.offset
        )

    async def get_grant(
        self, grant_id: int, *, actor: AuthenticatedUser
    ) -> UserModelGrant:
        return await self._require_visible_grant(grant_id, actor)

    async def create_grant(
        self, payload: GrantCreate, *, actor: AuthenticatedUser
    ) -> UserModelGrant:
        if payload.scope == "user":
            if not await self.grants.user_exists(payload.scope_id):
                raise AppError(ErrorCode.request_invalid, status.HTTP_400_BAD_REQUEST)
        elif payload.scope == "department" and not await self.grants.department_exists(
            payload.scope_id
        ):
            raise AppError(ErrorCode.request_invalid, status.HTTP_400_BAD_REQUEST)

        await self._require_subject_in_scope(
            scope=payload.scope, scope_id=payload.scope_id, actor=actor
        )

        if not await self.grants.model_exists(payload.logical_model_id):
            raise AppError(ErrorCode.request_invalid, status.HTTP_400_BAD_REQUEST)

        existing = await self.grants.get_existing(
            scope=payload.scope,
            scope_id=payload.scope_id,
            logical_model_id=payload.logical_model_id,
        )
        if existing is not None:
            raise AppError(ErrorCode.request_invalid, status.HTTP_400_BAD_REQUEST)

        if payload.is_default:
            await self._clear_default(
                scope=payload.scope,
                scope_id=payload.scope_id,
                actor=actor,
            )

        grant = UserModelGrant(
            scope=payload.scope,
            scope_id=payload.scope_id,
            logical_model_id=payload.logical_model_id,
            is_default=payload.is_default,
            remark=payload.remark,
            created_by=actor.user_id,
            create_dept=actor.department_id,
            updated_by=actor.user_id,
        )
        await self.grants.create(grant)
        return grant

    async def update_grant(
        self,
        grant_id: int,
        payload: GrantUpdate,
        *,
        actor: AuthenticatedUser,
    ) -> UserModelGrant:
        grant = await self._require_visible_grant(grant_id, actor)
        values = payload.model_dump(exclude_unset=True)
        if values.get("is_default") is True and not grant.is_default:
            await self._clear_default(
                scope=grant.scope,
                scope_id=grant.scope_id,
                actor=actor,
            )
        values["updated_by"] = actor.user_id
        await self.grants.update(grant, **values)
        await self.session.refresh(grant)
        return grant

    async def delete_grant(self, grant_id: int, *, actor: AuthenticatedUser) -> None:
        grant = await self._require_visible_grant(grant_id, actor)
        grant.updated_by = actor.user_id
        await self.grants.soft_delete(grant)
