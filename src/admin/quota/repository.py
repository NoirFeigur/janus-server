"""Admin quota rule data access."""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import func, select
from sqlalchemy.orm.attributes import InstrumentedAttribute
from sqlalchemy.sql.elements import ColumnElement

from src.db.models.quota import Quota
from src.db.repository import BaseRepository


class QuotaRepository(BaseRepository[Quota]):
    model = Quota

    async def get_existing(
        self,
        *,
        scope: str,
        scope_id: int | None,
        logical_model_id: int | None,
        period: str,
        metric: str,
    ) -> Quota | None:
        stmt = select(Quota).where(
            Quota.is_deleted.is_(False),
            Quota.scope == scope,
            Quota.scope_id.is_(None)
            if scope_id is None
            else Quota.scope_id == scope_id,
            Quota.logical_model_id.is_(None)
            if logical_model_id is None
            else Quota.logical_model_id == logical_model_id,
            Quota.period == period,
            Quota.metric == metric,
        )
        quota: Quota | None = await self.session.scalar(stmt)
        return quota

    def _filters(
        self,
        *,
        scope: str | None,
        scope_id: int | None,
        logical_model_id: int | None,
        status: str | None,
    ) -> list[ColumnElement[bool]]:
        filters: list[ColumnElement[bool]] = [Quota.is_deleted.is_(False)]
        if scope is not None:
            filters.append(Quota.scope == scope)
        if scope_id is not None:
            filters.append(Quota.scope_id == scope_id)
        if logical_model_id is not None:
            filters.append(Quota.logical_model_id == logical_model_id)
        if status is not None:
            filters.append(Quota.status == status)
        return filters

    async def list_quotas(
        self,
        *,
        scope: str | None = None,
        scope_id: int | None = None,
        logical_model_id: int | None = None,
        status: str | None = None,
        sort: tuple[InstrumentedAttribute[object], bool] | None = None,
        limit: int,
        offset: int,
    ) -> Sequence[Quota]:
        stmt = select(Quota)
        for predicate in self._filters(
            scope=scope,
            scope_id=scope_id,
            logical_model_id=logical_model_id,
            status=status,
        ):
            stmt = stmt.where(predicate)
        if sort is None:
            stmt = stmt.order_by(Quota.created_at.desc())
        else:
            column, descending = sort
            stmt = stmt.order_by(column.desc() if descending else column.asc())
        stmt = stmt.limit(limit).offset(offset)
        result = await self.session.scalars(stmt)
        return result.all()

    async def count_quotas(
        self,
        *,
        scope: str | None = None,
        scope_id: int | None = None,
        logical_model_id: int | None = None,
        status: str | None = None,
    ) -> int:
        stmt = select(func.count()).select_from(Quota)
        for predicate in self._filters(
            scope=scope,
            scope_id=scope_id,
            logical_model_id=logical_model_id,
            status=status,
        ):
            stmt = stmt.where(predicate)
        total = await self.session.scalar(stmt)
        return int(total or 0)
