"""Admin usage business logic."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from src.admin.usage.repository import UsageRepository
from src.admin.usage.schemas import UsageStats
from src.auth.service import AuthenticatedUser, AuthService
from src.core.pagination import PageResult, page_result
from src.core.query import ListQuery, resolve_sort
from src.db.models.usage import UsageRecord

SORT_COLUMNS = {
    "id": UsageRecord.id,
    "user_id": UsageRecord.user_id,
    "logical_model_id": UsageRecord.logical_model_id,
    "total_tokens": UsageRecord.total_tokens,
    "cost": UsageRecord.cost,
    "latency_ms": UsageRecord.latency_ms,
    "created_at": UsageRecord.created_at,
}


class UsageService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.repo = UsageRepository(session)
        self.auth = AuthService(session)

    async def list_records(
        self,
        *,
        user_id: int | None = None,
        logical_model_id: int | None = None,
        status: str | None = None,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        query: ListQuery | None = None,
        actor: AuthenticatedUser,
    ) -> PageResult[UsageRecord]:
        query = query or ListQuery()
        sort = resolve_sort(query, allowed=SORT_COLUMNS, default="created_at")
        total = await self.repo.count_records(
            user_id=user_id,
            logical_model_id=logical_model_id,
            status=status,
            date_from=date_from,
            date_to=date_to,
        )
        items = await self.repo.list_records(
            user_id=user_id,
            logical_model_id=logical_model_id,
            status=status,
            date_from=date_from,
            date_to=date_to,
            sort=sort,
            limit=query.limit,
            offset=query.offset,
        )
        return page_result(
            list(items), total=total, limit=query.limit, offset=query.offset
        )

    async def get_stats(
        self,
        *,
        user_id: int | None = None,
        logical_model_id: int | None = None,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        actor: AuthenticatedUser,
    ) -> UsageStats:
        stats = await self.repo.aggregate_stats(
            user_id=user_id,
            logical_model_id=logical_model_id,
            date_from=date_from,
            date_to=date_to,
        )
        total_requests = stats["total_requests"]
        error_count = stats["error_count"]
        error_rate = error_count / total_requests * 100 if total_requests else 0.0
        return UsageStats(**stats, error_rate=error_rate)
