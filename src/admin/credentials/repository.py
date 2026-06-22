"""Admin API key credential data access (repository layer)."""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import func, or_, select
from sqlalchemy.orm.attributes import InstrumentedAttribute
from sqlalchemy.sql.elements import ColumnElement

from src.db.models.credential import ApiKey
from src.db.repository import BaseRepository

Sort = tuple[InstrumentedAttribute[object], bool]


class ApiKeyRepository(BaseRepository[ApiKey]):
    model = ApiKey

    def _filters(
        self,
        *,
        user_id: int | None,
        status: str | None,
        keyword: str | None,
    ) -> list[ColumnElement[bool]]:
        filters: list[ColumnElement[bool]] = [ApiKey.is_deleted.is_(False)]
        if user_id is not None:
            filters.append(ApiKey.user_id == user_id)
        if status is not None:
            filters.append(ApiKey.status == status)
        normalized = self._normalize_keyword(keyword)
        if normalized is not None:
            pattern = f"%{normalized}%"
            filters.append(
                or_(
                    func.lower(ApiKey.name).like(pattern),
                    func.lower(ApiKey.key_prefix).like(pattern),
                )
            )
        return filters

    def _normalize_keyword(self, keyword: str | None) -> str | None:
        if keyword is None:
            return None
        normalized = keyword.strip().lower()
        return normalized or None

    async def list_keys(
        self,
        *,
        user_id: int | None = None,
        status: str | None = None,
        keyword: str | None = None,
        sort: Sort | None = None,
        limit: int,
        offset: int,
    ) -> Sequence[ApiKey]:
        """List API keys with optional filters and pagination."""
        stmt = select(ApiKey)
        for predicate in self._filters(
            user_id=user_id,
            status=status,
            keyword=keyword,
        ):
            stmt = stmt.where(predicate)
        if sort is None:
            stmt = stmt.order_by(ApiKey.created_at.desc())
        else:
            column, descending = sort
            stmt = stmt.order_by(column.desc() if descending else column.asc())
        stmt = stmt.limit(limit).offset(offset)
        result = await self.session.scalars(stmt)
        return result.all()

    async def count_keys(
        self,
        *,
        user_id: int | None = None,
        status: str | None = None,
        keyword: str | None = None,
    ) -> int:
        """Count API keys using the same filters as :meth:`list_keys`."""
        stmt = select(func.count()).select_from(ApiKey)
        for predicate in self._filters(
            user_id=user_id,
            status=status,
            keyword=keyword,
        ):
            stmt = stmt.where(predicate)
        total = await self.session.scalar(stmt)
        return int(total or 0)
