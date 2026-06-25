"""Admin observability service — gateway logs query, DLQ, queue health."""

from __future__ import annotations

from typing import Any

from sqlalchemy import desc, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from src.auth.service import AuthenticatedUser, AuthService, DataScopeFilter
from src.core.pagination import PageResult, page_result
from src.core.query import ListQuery, resolve_sort
from src.db.models.gateway_observability import GatewayRequestLog
from src.db.models.identity import User
from src.gateway.events import (
    LOG_DLQ_KEY,
    LOG_INFLIGHT_KEY,
    LOG_QUEUE_KEY,
    USAGE_DLQ_KEY,
    USAGE_INFLIGHT_KEY,
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
        self.auth = AuthService(session)

    def _scope_predicate(
        self, scope: DataScopeFilter, *, actor_id: int
    ) -> ColumnElement[bool] | None:
        """WHERE predicate limiting logs to users visible under ``scope``.

        Gateway logs carry only ``user_id`` (a ``LogEntity`` with no audit
        columns), so visibility keys off the log owner's department / identity —
        the same shape as the credential scope predicate. ``user_id`` is nullable
        (system / unauthenticated requests); those rows are intentionally hidden
        from restricted actors because they have no owner to attribute them to.
        """
        if scope.unrestricted:
            return None
        clauses: list[ColumnElement[bool]] = []
        if scope.department_ids:
            visible_users = select(User.id).where(
                User.is_deleted.is_(False),
                User.department_id.in_(scope.department_ids),
            )
            clauses.append(GatewayRequestLog.user_id.in_(visible_users))
        if scope.include_self:
            clauses.append(GatewayRequestLog.user_id == actor_id)
        if not clauses:
            # Restricted actor with neither dept nor self visibility sees nothing.
            return GatewayRequestLog.id == -1
        return or_(*clauses)

    async def list_logs(
        self,
        query: ListQuery,
        *,
        actor: AuthenticatedUser,
        user_id: int | None = None,
        model: str | None = None,
        channel_id: int | None = None,
        provider: str | None = None,
        status_code: int | None = None,
        error_code: str | None = None,
    ) -> PageResult[GatewayRequestLog]:
        stmt = select(GatewayRequestLog)

        scope = await self.auth.resolve_data_scope(actor)
        scope_predicate = self._scope_predicate(scope, actor_id=actor.user_id)
        if scope_predicate is not None:
            stmt = stmt.where(scope_predicate)
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

        order_col, is_desc = resolve_sort(query, allowed=LOG_SORT_COLUMNS, default="id")
        stmt = stmt.order_by(desc(order_col) if is_desc else order_col)

        count_stmt = select(func.count()).select_from(GatewayRequestLog)
        if stmt.whereclause is not None:
            count_stmt = count_stmt.where(stmt.whereclause)
        total = await self.session.scalar(count_stmt) or 0

        stmt = stmt.offset(query.offset).limit(query.limit)
        result = await self.session.execute(stmt)
        items = list(result.scalars().all())

        return page_result(
            items=items, total=int(total), limit=query.limit, offset=query.offset
        )

    async def get_log_by_request_id(
        self, request_id: str, *, actor: AuthenticatedUser
    ) -> GatewayRequestLog | None:
        stmt = select(GatewayRequestLog).where(
            GatewayRequestLog.request_id == request_id
        )
        scope = await self.auth.resolve_data_scope(actor)
        scope_predicate = self._scope_predicate(scope, actor_id=actor.user_id)
        if scope_predicate is not None:
            stmt = stmt.where(scope_predicate)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    # ------------------------------------------------------------------
    # Queue health
    # ------------------------------------------------------------------

    async def get_queue_health(self) -> dict[str, int]:
        return {
            "usage_pending": await get_queue_length(USAGE_QUEUE_KEY),
            "usage_inflight": await get_queue_length(USAGE_INFLIGHT_KEY),
            "log_pending": await get_queue_length(LOG_QUEUE_KEY),
            "log_inflight": await get_queue_length(LOG_INFLIGHT_KEY),
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
