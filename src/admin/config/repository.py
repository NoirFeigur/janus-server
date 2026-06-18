"""Platform config data access (repository layer)."""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import func, or_, select
from sqlalchemy.orm.attributes import InstrumentedAttribute
from sqlalchemy.sql.elements import ColumnElement

from src.db.models.sys_config import SysConfig
from src.db.repository import BaseRepository


class SysConfigRepository(BaseRepository[SysConfig]):
    model = SysConfig

    async def get_by_key(
        self, config_key: str, *, include_deleted: bool = False
    ) -> SysConfig | None:
        """Fetch one config row by its unique ``config_key``."""
        stmt = select(SysConfig).where(SysConfig.config_key == config_key)
        if not include_deleted:
            stmt = stmt.where(SysConfig.is_deleted.is_(False))
        config: SysConfig | None = await self.session.scalar(stmt)
        return config

    def _filters(self, *, keyword: str | None) -> list[ColumnElement[bool]]:
        filters: list[ColumnElement[bool]] = [SysConfig.is_deleted.is_(False)]
        normalized = self._normalize_keyword(keyword)
        if normalized is not None:
            pattern = f"%{normalized}%"
            filters.append(
                or_(
                    func.lower(SysConfig.config_key).like(pattern),
                    func.lower(SysConfig.config_name).like(pattern),
                )
            )
        return filters

    def _normalize_keyword(self, keyword: str | None) -> str | None:
        if keyword is None:
            return None
        normalized = keyword.strip().lower()
        return normalized or None

    async def list_configs(
        self,
        *,
        keyword: str | None = None,
        sort: tuple[InstrumentedAttribute[object], bool] | None = None,
        limit: int,
        offset: int,
    ) -> Sequence[SysConfig]:
        """List config rows with optional keyword filter and pagination."""
        stmt = select(SysConfig)
        for predicate in self._filters(keyword=keyword):
            stmt = stmt.where(predicate)
        if sort is None:
            stmt = stmt.order_by(SysConfig.config_key.asc())
        else:
            column, descending = sort
            stmt = stmt.order_by(column.desc() if descending else column.asc())
        stmt = stmt.limit(limit).offset(offset)
        result = await self.session.scalars(stmt)
        return result.all()

    async def count_configs(self, *, keyword: str | None = None) -> int:
        """Count config rows using the same filter as :meth:`list_configs`."""
        stmt = select(func.count()).select_from(SysConfig)
        for predicate in self._filters(keyword=keyword):
            stmt = stmt.where(predicate)
        total = await self.session.scalar(stmt)
        return int(total or 0)
