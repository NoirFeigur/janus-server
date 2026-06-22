"""Usage record data access (append-only ledger)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import InstrumentedAttribute
from sqlalchemy.sql.elements import ColumnElement

from src.db.models.usage import UsageRecord


class UsageRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    def _filters(
        self,
        *,
        user_id: int | None,
        logical_model_id: int | None,
        status: str | None = None,
        date_from: datetime | None,
        date_to: datetime | None,
    ) -> list[ColumnElement[bool]]:
        filters: list[ColumnElement[bool]] = []
        if user_id is not None:
            filters.append(UsageRecord.user_id == user_id)
        if logical_model_id is not None:
            filters.append(UsageRecord.logical_model_id == logical_model_id)
        if status is not None:
            filters.append(UsageRecord.status == status)
        if date_from is not None:
            filters.append(UsageRecord.created_at >= date_from)
        if date_to is not None:
            filters.append(UsageRecord.created_at <= date_to)
        return filters

    async def list_records(
        self,
        *,
        user_id: int | None = None,
        logical_model_id: int | None = None,
        status: str | None = None,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        sort: tuple[InstrumentedAttribute[object], bool] | None = None,
        limit: int,
        offset: int,
    ) -> Sequence[UsageRecord]:
        """List usage rows with optional filters and pagination."""
        stmt = select(UsageRecord)
        for predicate in self._filters(
            user_id=user_id,
            logical_model_id=logical_model_id,
            status=status,
            date_from=date_from,
            date_to=date_to,
        ):
            stmt = stmt.where(predicate)
        if sort is None:
            stmt = stmt.order_by(UsageRecord.created_at.desc())
        else:
            column, descending = sort
            stmt = stmt.order_by(column.desc() if descending else column.asc())
        stmt = stmt.limit(limit).offset(offset)
        result = await self.session.scalars(stmt)
        return result.all()

    async def count_records(
        self,
        *,
        user_id: int | None = None,
        logical_model_id: int | None = None,
        status: str | None = None,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
    ) -> int:
        """Count usage rows using the same filters as list_records."""
        stmt = select(func.count()).select_from(UsageRecord)
        for predicate in self._filters(
            user_id=user_id,
            logical_model_id=logical_model_id,
            status=status,
            date_from=date_from,
            date_to=date_to,
        ):
            stmt = stmt.where(predicate)
        total = await self.session.scalar(stmt)
        return int(total or 0)

    async def aggregate_stats(
        self,
        *,
        user_id: int | None = None,
        logical_model_id: int | None = None,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
    ) -> dict[str, Any]:
        """Aggregate token, cost, latency, and result-count statistics."""
        success_expr = case((UsageRecord.status == "success", 1), else_=0)
        error_expr = case((UsageRecord.status != "success", 1), else_=0)
        stmt = select(
            func.count().label("total_requests"),
            func.sum(UsageRecord.total_tokens).label("total_tokens"),
            func.sum(UsageRecord.cost).label("total_cost"),
            func.avg(UsageRecord.latency_ms).label("avg_latency_ms"),
            func.sum(success_expr).label("success_count"),
            func.sum(error_expr).label("error_count"),
        ).select_from(UsageRecord)
        for predicate in self._filters(
            user_id=user_id,
            logical_model_id=logical_model_id,
            date_from=date_from,
            date_to=date_to,
        ):
            stmt = stmt.where(predicate)
        row = (await self.session.execute(stmt)).one()
        return {
            "total_requests": int(row.total_requests or 0),
            "total_tokens": int(row.total_tokens or 0),
            "total_cost": row.total_cost
            if isinstance(row.total_cost, Decimal)
            else None,
            "avg_latency_ms": float(row.avg_latency_ms)
            if row.avg_latency_ms is not None
            else None,
            "success_count": int(row.success_count or 0),
            "error_count": int(row.error_count or 0),
        }
