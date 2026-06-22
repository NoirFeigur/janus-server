"""Admin model-grant data access (repository layer)."""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import func, select
from sqlalchemy.orm.attributes import InstrumentedAttribute
from sqlalchemy.sql.elements import ColumnElement

from src.db.models.grant import UserModelGrant
from src.db.repository import BaseRepository

Sort = tuple[InstrumentedAttribute[object], bool]


class GrantRepository(BaseRepository[UserModelGrant]):
    model = UserModelGrant

    async def get_existing(
        self,
        *,
        scope: str,
        scope_id: int,
        logical_model_id: int,
    ) -> UserModelGrant | None:
        stmt = select(UserModelGrant).where(
            UserModelGrant.scope == scope,
            UserModelGrant.scope_id == scope_id,
            UserModelGrant.logical_model_id == logical_model_id,
            UserModelGrant.is_deleted.is_(False),
        )
        grant: UserModelGrant | None = await self.session.scalar(stmt)
        return grant

    async def get_default(
        self, *, scope: str, scope_id: int
    ) -> UserModelGrant | None:
        stmt = select(UserModelGrant).where(
            UserModelGrant.scope == scope,
            UserModelGrant.scope_id == scope_id,
            UserModelGrant.is_default.is_(True),
            UserModelGrant.is_deleted.is_(False),
        )
        grant: UserModelGrant | None = await self.session.scalar(stmt)
        return grant

    def _filters(
        self,
        *,
        keyword: str | None,
        scope: str | None,
        scope_id: int | None,
        logical_model_id: int | None,
    ) -> list[ColumnElement[bool]]:
        filters: list[ColumnElement[bool]] = [UserModelGrant.is_deleted.is_(False)]
        if scope is not None:
            filters.append(UserModelGrant.scope == scope)
        if scope_id is not None:
            filters.append(UserModelGrant.scope_id == scope_id)
        if logical_model_id is not None:
            filters.append(UserModelGrant.logical_model_id == logical_model_id)
        return filters

    async def list_grants(
        self,
        *,
        keyword: str | None = None,
        scope: str | None = None,
        scope_id: int | None = None,
        logical_model_id: int | None = None,
        sort: Sort | None = None,
        limit: int,
        offset: int,
    ) -> Sequence[UserModelGrant]:
        stmt = select(UserModelGrant)
        for predicate in self._filters(
            keyword=keyword,
            scope=scope,
            scope_id=scope_id,
            logical_model_id=logical_model_id,
        ):
            stmt = stmt.where(predicate)
        if sort is None:
            stmt = stmt.order_by(UserModelGrant.created_at.desc())
        else:
            column, descending = sort
            stmt = stmt.order_by(column.desc() if descending else column.asc())
        stmt = stmt.limit(limit).offset(offset)
        result = await self.session.scalars(stmt)
        return result.all()

    async def count_grants(
        self,
        *,
        keyword: str | None = None,
        scope: str | None = None,
        scope_id: int | None = None,
        logical_model_id: int | None = None,
    ) -> int:
        stmt = select(func.count()).select_from(UserModelGrant)
        for predicate in self._filters(
            keyword=keyword,
            scope=scope,
            scope_id=scope_id,
            logical_model_id=logical_model_id,
        ):
            stmt = stmt.where(predicate)
        total = await self.session.scalar(stmt)
        return int(total or 0)
