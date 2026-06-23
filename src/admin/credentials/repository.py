"""Admin API key credential data access (repository layer)."""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import func, or_, select
from sqlalchemy.orm.attributes import InstrumentedAttribute
from sqlalchemy.sql.elements import ColumnElement

from src.db.models.credential import ApiKey
from src.db.models.identity import User
from src.db.repository import BaseRepository
from src.db.scope import DataScope

Sort = tuple[InstrumentedAttribute[object], bool]


class ApiKeyRepository(BaseRepository[ApiKey]):
    model = ApiKey

    def _user_scope_predicate(
        self, scope_filter: DataScope, *, actor_id: int
    ) -> ColumnElement[bool] | None:
        if scope_filter.unrestricted:
            return None
        visible_users = select(User.id).where(User.is_deleted.is_(False))
        clauses: list[ColumnElement[bool]] = []
        if scope_filter.department_ids:
            clauses.append(User.department_id.in_(scope_filter.department_ids))
        if scope_filter.include_self:
            clauses.append(User.id == actor_id)
        if not clauses:
            return ApiKey.id == -1
        visible_users = visible_users.where(or_(*clauses))
        return ApiKey.user_id.in_(visible_users)

    def _filters(
        self,
        *,
        user_id: int | None,
        status: str | None,
        keyword: str | None,
        scope_filter: DataScope | None = None,
        actor_id: int | None = None,
    ) -> list[ColumnElement[bool]]:
        filters: list[ColumnElement[bool]] = [ApiKey.is_deleted.is_(False)]
        if scope_filter is not None and actor_id is not None:
            predicate = self._user_scope_predicate(scope_filter, actor_id=actor_id)
            if predicate is not None:
                filters.append(predicate)
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
        scope_filter: DataScope | None = None,
        actor_id: int | None = None,
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
            scope_filter=scope_filter,
            actor_id=actor_id,
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
        scope_filter: DataScope | None = None,
        actor_id: int | None = None,
    ) -> int:
        """Count API keys using the same filters as :meth:`list_keys`."""
        stmt = select(func.count()).select_from(ApiKey)
        for predicate in self._filters(
            user_id=user_id,
            status=status,
            keyword=keyword,
            scope_filter=scope_filter,
            actor_id=actor_id,
        ):
            stmt = stmt.where(predicate)
        total = await self.session.scalar(stmt)
        return int(total or 0)

    async def user_exists(self, user_id: int) -> bool:
        stmt = select(User.id).where(User.id == user_id, User.is_deleted.is_(False))
        return await self.session.scalar(stmt) is not None

    async def user_in_scope(
        self, user_id: int, scope_filter: DataScope, *, actor_id: int
    ) -> bool:
        if scope_filter.unrestricted:
            return True
        stmt = select(User.id).where(User.id == user_id, User.is_deleted.is_(False))
        clauses: list[ColumnElement[bool]] = []
        if scope_filter.department_ids:
            clauses.append(User.department_id.in_(scope_filter.department_ids))
        if scope_filter.include_self:
            clauses.append(User.id == actor_id)
        if not clauses:
            return False
        stmt = stmt.where(or_(*clauses))
        return await self.session.scalar(stmt) is not None
