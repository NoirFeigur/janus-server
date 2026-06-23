"""Admin quota rule business logic."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette import status

from src.admin.quota.repository import QuotaRepository
from src.admin.quota.schemas import QuotaCreate, QuotaUpdate
from src.auth.service import AuthenticatedUser
from src.core.pagination import PageResult, page_result
from src.core.query import ListQuery, resolve_sort
from src.db.models.identity import Department, User
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

    async def list_quotas(
        self,
        *,
        scope: str | None = None,
        scope_id: int | None = None,
        logical_model_id: int | None = None,
        status_filter: str | None = None,
        query: ListQuery | None = None,
    ) -> PageResult[Quota]:
        query = query or ListQuery()
        sort = resolve_sort(query, allowed=SORT_COLUMNS, default="created_at")
        total = await self.repo.count_quotas(
            scope=scope,
            scope_id=scope_id,
            logical_model_id=logical_model_id,
            status=status_filter,
        )
        items = await self.repo.list_quotas(
            scope=scope,
            scope_id=scope_id,
            logical_model_id=logical_model_id,
            status=status_filter,
            sort=sort,
            limit=query.limit,
            offset=query.offset,
        )
        return page_result(
            list(items), total=total, limit=query.limit, offset=query.offset
        )

    async def get_quota(self, quota_id: int) -> Quota:
        return await self._require(quota_id)

    async def create_quota(
        self, payload: QuotaCreate, *, actor: AuthenticatedUser
    ) -> Quota:
        self._validate_scope(payload.scope, payload.scope_id)
        if payload.scope == "user" and payload.scope_id is not None:
            exists = await self.session.scalar(
                select(User.id).where(
                    User.id == payload.scope_id,
                    User.is_deleted.is_(False),
                )
            )
            if exists is None:
                raise AppError(ErrorCode.request_invalid, status.HTTP_400_BAD_REQUEST)
        elif payload.scope == "department" and payload.scope_id is not None:
            exists = await self.session.scalar(
                select(Department.id).where(
                    Department.id == payload.scope_id,
                    Department.is_deleted.is_(False),
                )
            )
            if exists is None:
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
        quota = await self._require(quota_id)
        values = payload.model_dump(exclude_unset=True)
        values["updated_by"] = actor.user_id
        await self.repo.update(quota, **values)
        await self.session.flush()
        await self.session.refresh(quota)
        return quota

    async def delete_quota(self, quota_id: int, *, actor: AuthenticatedUser) -> None:
        quota = await self._require(quota_id)
        quota.updated_by = actor.user_id
        await self.repo.soft_delete(quota)
