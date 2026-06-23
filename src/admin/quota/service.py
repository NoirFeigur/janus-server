"""Admin quota rule business logic."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession
from starlette import status

from src.admin.quota.repository import QuotaRepository
from src.admin.quota.schemas import QuotaCreate, QuotaUpdate
from src.auth.service import AuthenticatedUser, AuthService, DataScopeFilter
from src.core.pagination import PageResult, page_result
from src.core.query import ListQuery, resolve_sort
from src.db.models.quota import Quota
from src.enums import ErrorCode
from src.exceptions import AppError

SORT_COLUMNS = {
    "id": Quota.id,
    "scope": Quota.scope,
    "period": Quota.period,
    "metric": Quota.metric,
    "limit_value": Quota.limit_value,
    "created_at": Quota.created_at,
    "updated_at": Quota.updated_at,
}


class QuotaService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.repo = QuotaRepository(session)
        self.auth = AuthService(session)

    async def _scope(self, actor: AuthenticatedUser) -> DataScopeFilter:
        return await self.auth.resolve_data_scope(actor)

    async def _require(self, quota_id: int) -> Quota:
        quota = await self.repo.get(quota_id)
        if quota is None:
            raise AppError(ErrorCode.request_invalid, status.HTTP_404_NOT_FOUND)
        return quota

    def _validate_scope(self, scope: str, scope_id: int | None) -> None:
        if scope == "global" and scope_id is not None:
            raise AppError(ErrorCode.request_invalid, status.HTTP_400_BAD_REQUEST)
        if scope != "global" and scope_id is None:
            raise AppError(ErrorCode.request_invalid, status.HTTP_400_BAD_REQUEST)

    async def _require_subject_in_scope(
        self, *, scope: str, scope_id: int | None, actor: AuthenticatedUser
    ) -> None:
        if scope == "global":
            if not actor.is_superuser:
                raise AppError(ErrorCode.auth_forbidden, status.HTTP_403_FORBIDDEN)
            return
        scope_filter = await self._scope(actor)
        if not await self.repo.subject_in_scope(
            scope=scope,
            scope_id=scope_id,
            scope_filter=scope_filter,
            actor_id=actor.user_id,
        ):
            raise AppError(ErrorCode.auth_forbidden, status.HTTP_403_FORBIDDEN)

    async def _require_visible_quota(
        self, quota_id: int, actor: AuthenticatedUser
    ) -> Quota:
        quota = await self._require(quota_id)
        await self._require_subject_in_scope(
            scope=quota.scope, scope_id=quota.scope_id, actor=actor
        )
        return quota

    async def list_quotas(
        self,
        *,
        scope: str | None = None,
        scope_id: int | None = None,
        logical_model_id: int | None = None,
        status_filter: str | None = None,
        query: ListQuery | None = None,
        actor: AuthenticatedUser,
    ) -> PageResult[Quota]:
        query = query or ListQuery()
        scope_filter = await self._scope(actor)
        sort = resolve_sort(query, allowed=SORT_COLUMNS, default="created_at")
        total = await self.repo.count_quotas(
            scope=scope,
            scope_id=scope_id,
            logical_model_id=logical_model_id,
            status=status_filter,
            scope_filter=scope_filter,
            actor_id=actor.user_id,
            include_global=actor.is_superuser,
        )
        items = await self.repo.list_quotas(
            scope=scope,
            scope_id=scope_id,
            logical_model_id=logical_model_id,
            status=status_filter,
            scope_filter=scope_filter,
            actor_id=actor.user_id,
            include_global=actor.is_superuser,
            sort=sort,
            limit=query.limit,
            offset=query.offset,
        )
        return page_result(
            list(items), total=total, limit=query.limit, offset=query.offset
        )

    async def get_quota(self, quota_id: int, *, actor: AuthenticatedUser) -> Quota:
        return await self._require_visible_quota(quota_id, actor)

    async def create_quota(
        self, payload: QuotaCreate, *, actor: AuthenticatedUser
    ) -> Quota:
        self._validate_scope(payload.scope, payload.scope_id)
        await self._require_subject_in_scope(
            scope=payload.scope, scope_id=payload.scope_id, actor=actor
        )
        if payload.scope == "user" and payload.scope_id is not None:
            if not await self.repo.user_exists(payload.scope_id):
                raise AppError(ErrorCode.request_invalid, status.HTTP_400_BAD_REQUEST)
        elif (
            payload.scope == "department"
            and payload.scope_id is not None
            and not await self.repo.department_exists(payload.scope_id)
        ):
            raise AppError(ErrorCode.request_invalid, status.HTTP_400_BAD_REQUEST)

        if payload.logical_model_id is not None and not await self.repo.model_exists(
            payload.logical_model_id
        ):
            raise AppError(ErrorCode.request_invalid, status.HTTP_400_BAD_REQUEST)

        existing = await self.repo.get_existing(
            scope=payload.scope,
            scope_id=payload.scope_id,
            logical_model_id=payload.logical_model_id,
            period=payload.period,
            metric=payload.metric,
        )
        if existing is not None:
            raise AppError(ErrorCode.request_conflict, status.HTTP_409_CONFLICT)
        quota = Quota(
            scope=payload.scope,
            scope_id=payload.scope_id,
            logical_model_id=payload.logical_model_id,
            period=payload.period,
            metric=payload.metric,
            limit_value=payload.limit_value,
            enforce=payload.enforce,
            status=payload.status,
            remark=payload.remark,
            created_by=actor.user_id,
            create_dept=actor.department_id,
            updated_by=actor.user_id,
        )
        await self.repo.create(quota)
        await self.session.flush()
        return quota

    async def update_quota(
        self, quota_id: int, payload: QuotaUpdate, *, actor: AuthenticatedUser
    ) -> Quota:
        quota = await self._require_visible_quota(quota_id, actor)
        values = payload.model_dump(exclude_unset=True)
        values["updated_by"] = actor.user_id
        await self.repo.update(quota, **values)
        await self.session.flush()
        await self.session.refresh(quota)
        return quota

    async def delete_quota(self, quota_id: int, *, actor: AuthenticatedUser) -> None:
        quota = await self._require_visible_quota(quota_id, actor)
        quota.updated_by = actor.user_id
        await self.repo.soft_delete(quota)
