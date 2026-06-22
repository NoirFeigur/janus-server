"""Admin catalog data access (repository layer)."""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import func, or_, select
from sqlalchemy.orm.attributes import InstrumentedAttribute
from sqlalchemy.sql.elements import ColumnElement

from src.db.models.model_catalog import (
    ChannelKey,
    LogicalModel,
    ModelDeployment,
    UpstreamChannel,
)
from src.db.repository import BaseRepository

Sort = tuple[InstrumentedAttribute[object], bool]


def _normalize_keyword(keyword: str | None) -> str | None:
    if keyword is None:
        return None
    normalized = keyword.strip().lower()
    return normalized or None


class UpstreamChannelRepository(BaseRepository[UpstreamChannel]):
    model = UpstreamChannel

    async def get_by_name(
        self, name: str, *, include_deleted: bool = False
    ) -> UpstreamChannel | None:
        stmt = select(UpstreamChannel).where(UpstreamChannel.name == name)
        if not include_deleted:
            stmt = stmt.where(UpstreamChannel.is_deleted.is_(False))
        channel: UpstreamChannel | None = await self.session.scalar(stmt)
        return channel

    def _filters(self, *, keyword: str | None) -> list[ColumnElement[bool]]:
        filters: list[ColumnElement[bool]] = [UpstreamChannel.is_deleted.is_(False)]
        normalized = _normalize_keyword(keyword)
        if normalized is not None:
            pattern = f"%{normalized}%"
            filters.append(
                or_(
                    func.lower(UpstreamChannel.name).like(pattern),
                    func.lower(UpstreamChannel.provider).like(pattern),
                )
            )
        return filters

    async def list_channels(
        self,
        *,
        keyword: str | None = None,
        sort: Sort | None = None,
        limit: int,
        offset: int,
    ) -> Sequence[UpstreamChannel]:
        stmt = select(UpstreamChannel)
        for predicate in self._filters(keyword=keyword):
            stmt = stmt.where(predicate)
        if sort is None:
            stmt = stmt.order_by(UpstreamChannel.name.asc())
        else:
            column, descending = sort
            stmt = stmt.order_by(column.desc() if descending else column.asc())
        stmt = stmt.limit(limit).offset(offset)
        result = await self.session.scalars(stmt)
        return result.all()

    async def count_channels(self, *, keyword: str | None = None) -> int:
        stmt = select(func.count()).select_from(UpstreamChannel)
        for predicate in self._filters(keyword=keyword):
            stmt = stmt.where(predicate)
        total = await self.session.scalar(stmt)
        return int(total or 0)


class ChannelKeyRepository(BaseRepository[ChannelKey]):
    model = ChannelKey

    def _filters(
        self, *, channel_id: int | None, keyword: str | None
    ) -> list[ColumnElement[bool]]:
        filters: list[ColumnElement[bool]] = [ChannelKey.is_deleted.is_(False)]
        if channel_id is not None:
            filters.append(ChannelKey.channel_id == channel_id)
        normalized = _normalize_keyword(keyword)
        if normalized is not None:
            pattern = f"%{normalized}%"
            filters.append(func.lower(ChannelKey.alias).like(pattern))
        return filters

    async def list_keys(
        self,
        *,
        channel_id: int | None = None,
        keyword: str | None = None,
        sort: Sort | None = None,
        limit: int,
        offset: int,
    ) -> Sequence[ChannelKey]:
        stmt = select(ChannelKey)
        for predicate in self._filters(channel_id=channel_id, keyword=keyword):
            stmt = stmt.where(predicate)
        if sort is None:
            stmt = stmt.order_by(ChannelKey.priority.asc(), ChannelKey.id.asc())
        else:
            column, descending = sort
            stmt = stmt.order_by(column.desc() if descending else column.asc())
        stmt = stmt.limit(limit).offset(offset)
        result = await self.session.scalars(stmt)
        return result.all()

    async def count_keys(
        self, *, channel_id: int | None = None, keyword: str | None = None
    ) -> int:
        stmt = select(func.count()).select_from(ChannelKey)
        for predicate in self._filters(channel_id=channel_id, keyword=keyword):
            stmt = stmt.where(predicate)
        total = await self.session.scalar(stmt)
        return int(total or 0)


class LogicalModelRepository(BaseRepository[LogicalModel]):
    model = LogicalModel

    async def get_by_name(
        self, name: str, *, include_deleted: bool = False
    ) -> LogicalModel | None:
        stmt = select(LogicalModel).where(LogicalModel.name == name)
        if not include_deleted:
            stmt = stmt.where(LogicalModel.is_deleted.is_(False))
        model: LogicalModel | None = await self.session.scalar(stmt)
        return model

    def _filters(self, *, keyword: str | None) -> list[ColumnElement[bool]]:
        filters: list[ColumnElement[bool]] = [LogicalModel.is_deleted.is_(False)]
        normalized = _normalize_keyword(keyword)
        if normalized is not None:
            pattern = f"%{normalized}%"
            filters.append(
                or_(
                    func.lower(LogicalModel.name).like(pattern),
                    func.lower(LogicalModel.display_name).like(pattern),
                )
            )
        return filters

    async def list_models(
        self,
        *,
        keyword: str | None = None,
        sort: Sort | None = None,
        limit: int,
        offset: int,
    ) -> Sequence[LogicalModel]:
        stmt = select(LogicalModel)
        for predicate in self._filters(keyword=keyword):
            stmt = stmt.where(predicate)
        if sort is None:
            stmt = stmt.order_by(LogicalModel.sort_order.asc(), LogicalModel.name.asc())
        else:
            column, descending = sort
            stmt = stmt.order_by(column.desc() if descending else column.asc())
        stmt = stmt.limit(limit).offset(offset)
        result = await self.session.scalars(stmt)
        return result.all()

    async def count_models(self, *, keyword: str | None = None) -> int:
        stmt = select(func.count()).select_from(LogicalModel)
        for predicate in self._filters(keyword=keyword):
            stmt = stmt.where(predicate)
        total = await self.session.scalar(stmt)
        return int(total or 0)


class ModelDeploymentRepository(BaseRepository[ModelDeployment]):
    model = ModelDeployment

    async def get_by_logical_model_channel(
        self,
        *,
        logical_model_id: int,
        channel_id: int,
        include_deleted: bool = False,
    ) -> ModelDeployment | None:
        stmt = select(ModelDeployment).where(
            ModelDeployment.logical_model_id == logical_model_id,
            ModelDeployment.channel_id == channel_id,
        )
        if not include_deleted:
            stmt = stmt.where(ModelDeployment.is_deleted.is_(False))
        deployment: ModelDeployment | None = await self.session.scalar(stmt)
        return deployment

    def _filters(
        self, *, logical_model_id: int | None, keyword: str | None
    ) -> list[ColumnElement[bool]]:
        filters: list[ColumnElement[bool]] = [ModelDeployment.is_deleted.is_(False)]
        if logical_model_id is not None:
            filters.append(ModelDeployment.logical_model_id == logical_model_id)
        normalized = _normalize_keyword(keyword)
        if normalized is not None:
            pattern = f"%{normalized}%"
            filters.append(func.lower(ModelDeployment.upstream_model).like(pattern))
        return filters

    async def list_deployments(
        self,
        *,
        logical_model_id: int | None = None,
        keyword: str | None = None,
        sort: Sort | None = None,
        limit: int,
        offset: int,
    ) -> Sequence[ModelDeployment]:
        stmt = select(ModelDeployment)
        for predicate in self._filters(
            logical_model_id=logical_model_id, keyword=keyword
        ):
            stmt = stmt.where(predicate)
        if sort is None:
            stmt = stmt.order_by(
                ModelDeployment.priority.asc(), ModelDeployment.id.asc()
            )
        else:
            column, descending = sort
            stmt = stmt.order_by(column.desc() if descending else column.asc())
        stmt = stmt.limit(limit).offset(offset)
        result = await self.session.scalars(stmt)
        return result.all()

    async def count_deployments(
        self, *, logical_model_id: int | None = None, keyword: str | None = None
    ) -> int:
        stmt = select(func.count()).select_from(ModelDeployment)
        for predicate in self._filters(
            logical_model_id=logical_model_id, keyword=keyword
        ):
            stmt = stmt.where(predicate)
        total = await self.session.scalar(stmt)
        return int(total or 0)
