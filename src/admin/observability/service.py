"""Admin observability service — gateway logs query, DLQ, queue health."""

from __future__ import annotations

from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.pagination import PageResult, page_result
from src.core.query import ListQuery, resolve_sort
from src.db.models.gateway_observability import GatewayRequestLog
from src.gateway.events import (
    LOG_DLQ_KEY,
    LOG_QUEUE_KEY,
    USAGE_DLQ_KEY,
    USAGE_QUEUE_KEY,
    flush_dlq,
    get_queue_length,
    peek_dlq,
)

LOG_SORT_COLUMNS = {
    "id": GatewayRequestLog.id,
    "created_at": GatewayRequestLog.created_at,
    "latency_ms": GatewayRequestLog.latency_ms,
    "status_code": GatewayRequestLog.status_code,
}


class ObservabilityService:
    """Admin service for gateway observability data."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def list_logs(
        self,
        query: ListQuery,
        *,
        user_id: int | None = None,
        model: str | None = None,
        channel_id: int | None = None,
        provider: str | None = None,
        status_code: int | None = None,
        error_code: str | None = None,
    ) -> PageResult[GatewayRequestLog]:
        stmt = select(GatewayRequestLog)

        if user_id is not None:
            stmt = stmt.where(GatewayRequestLog.user_id == user_id)
        if model is not None:
            stmt = stmt.where(GatewayRequestLog.model == model)
        if channel_id is not None:
            stmt = stmt.where(GatewayRequestLog.channel_id == channel_id)
        if provider is not None:
            stmt = stmt.where(GatewayRequestLog.provider == provider)
        if status_code is not None:
            stmt = stmt.where(GatewayRequestLog.status_code == status_code)
        if error_code is not None:
            stmt = stmt.where(GatewayRequestLog.error_code == error_code)

        order_col = resolve_sort(LOG_SORT_COLUMNS, query.sort_by, default=GatewayRequestLog.id)
        if query.sort_order == "desc":
            stmt = stmt.order_by(desc(order_col))
        else:
            stmt = stmt.order_by(order_col)

        count_result = await self.session.execute(
            select(GatewayRequestLog.id).where(stmt.whereclause) if stmt.whereclause is not None
            else select(GatewayRequestLog.id)
        )
        total = len(count_result.all())

        stmt = stmt.offset(query.offset).limit(query.limit)
        result = await self.session.execute(stmt)
        items = list(result.scalars().all())

        return page_result(items=items, total=total, limit=query.limit, offset=query.offset)

    async def get_log_by_request_id(self, request_id: str) -> GatewayRequestLog | None:
        result = await self.session.execute(
            select(GatewayRequestLog).where(GatewayRequestLog.request_id == request_id)
        )
        return result.scalar_one_or_none()

    # ------------------------------------------------------------------
    # Queue health
    # ------------------------------------------------------------------

    async def get_queue_health(self) -> dict[str, int]:
        return {
            "usage_pending": await get_queue_length(USAGE_QUEUE_KEY),
            "log_pending": await get_queue_length(LOG_QUEUE_KEY),
            "usage_dlq": await get_queue_length(USAGE_DLQ_KEY),
            "log_dlq": await get_queue_length(LOG_DLQ_KEY),
        }

    # ------------------------------------------------------------------
    # DLQ operations
    # ------------------------------------------------------------------

    async def peek_usage_dlq(self, count: int = 20) -> list[dict[str, Any]]:
        return await peek_dlq(USAGE_DLQ_KEY, count)

    async def peek_log_dlq(self, count: int = 20) -> list[dict[str, Any]]:
        return await peek_dlq(LOG_DLQ_KEY, count)

    async def flush_usage_dlq(self) -> int:
        return await flush_dlq(USAGE_DLQ_KEY)

    async def flush_log_dlq(self) -> int:
        return await flush_dlq(LOG_DLQ_KEY)
