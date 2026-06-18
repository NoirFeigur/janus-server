"""Standalone audit log repository.

Audit log models inherit ``LogEntity`` (append-only), not ``BaseEntity``. This
repository deliberately does not subclass ``BaseRepository`` because audit rows
have no soft-delete column and no update semantics.
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import InstrumentedAttribute
from sqlalchemy.sql.elements import ColumnElement

from src.db.models.audit import LoginLog, OperLog


class AuditRepository:
    """Append-only audit log persistence and read-side queries."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    def _oper_filters(
        self,
        *,
        keyword: str | None,
        module: str | None,
        status: str | None,
        actor_id: int | None,
    ) -> list[ColumnElement[bool]]:
        filters: list[ColumnElement[bool]] = []
        if module is not None:
            filters.append(OperLog.module == module)
        if status is not None:
            filters.append(OperLog.status == status)
        if actor_id is not None:
            filters.append(OperLog.actor_id == actor_id)
        normalized = self._normalize_keyword(keyword)
        if normalized is not None:
            pattern = f"%{normalized}%"
            filters.append(
                or_(
                    func.lower(OperLog.actor_name).like(pattern),
                    func.lower(OperLog.path).like(pattern),
                )
            )
        return filters

    def _login_filters(
        self,
        *,
        keyword: str | None,
        status: str | None,
        user_id: int | None,
    ) -> list[ColumnElement[bool]]:
        filters: list[ColumnElement[bool]] = []
        if status is not None:
            filters.append(LoginLog.status == status)
        if user_id is not None:
            filters.append(LoginLog.user_id == user_id)
        normalized = self._normalize_keyword(keyword)
        if normalized is not None:
            filters.append(func.lower(LoginLog.username).like(f"%{normalized}%"))
        return filters

    def _normalize_keyword(self, keyword: str | None) -> str | None:
        if keyword is None:
            return None
        normalized = keyword.strip().lower()
        if not normalized:
            return None
        return normalized

    async def append_oper_log(self, row: OperLog) -> OperLog:
        """Append one operation audit row and flush the snowflake id."""
        self.session.add(row)
        await self.session.flush()
        return row

    async def list_oper_logs(
        self,
        *,
        keyword: str | None = None,
        module: str | None = None,
        status: str | None = None,
        actor_id: int | None = None,
        sort: tuple[InstrumentedAttribute[object], bool] | None = None,
        limit: int,
        offset: int,
    ) -> Sequence[OperLog]:
        """List operation audit logs with optional filters and pagination."""
        stmt = select(OperLog)
        for predicate in self._oper_filters(
            keyword=keyword,
            module=module,
            status=status,
            actor_id=actor_id,
        ):
            stmt = stmt.where(predicate)
        if sort is None:
            stmt = stmt.order_by(OperLog.created_at.desc())
        else:
            sort_column, descending = sort
            stmt = stmt.order_by(sort_column.desc() if descending else sort_column.asc())
        stmt = stmt.limit(limit).offset(offset)
        result = await self.session.scalars(stmt)
        return result.all()

    async def count_oper_logs(
        self,
        *,
        keyword: str | None = None,
        module: str | None = None,
        status: str | None = None,
        actor_id: int | None = None,
    ) -> int:
        """Count operation audit logs using the same filters as list."""
        stmt = select(func.count()).select_from(OperLog)
        for predicate in self._oper_filters(
            keyword=keyword,
            module=module,
            status=status,
            actor_id=actor_id,
        ):
            stmt = stmt.where(predicate)
        total = await self.session.scalar(stmt)
        return int(total or 0)

    async def append_login_log(self, row: LoginLog) -> LoginLog:
        """Append one login audit row and flush the snowflake id."""
        self.session.add(row)
        await self.session.flush()
        return row

    async def list_login_logs(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        user_id: int | None = None,
        sort: tuple[InstrumentedAttribute[object], bool] | None = None,
        limit: int,
        offset: int,
    ) -> Sequence[LoginLog]:
        """List login audit logs with optional filters and pagination."""
        stmt = select(LoginLog)
        for predicate in self._login_filters(
            keyword=keyword,
            status=status,
            user_id=user_id,
        ):
            stmt = stmt.where(predicate)
        if sort is None:
            stmt = stmt.order_by(LoginLog.created_at.desc())
        else:
            sort_column, descending = sort
            stmt = stmt.order_by(sort_column.desc() if descending else sort_column.asc())
        stmt = stmt.limit(limit).offset(offset)
        result = await self.session.scalars(stmt)
        return result.all()

    async def count_login_logs(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        user_id: int | None = None,
    ) -> int:
        """Count login audit logs using the same filters as list."""
        stmt = select(func.count()).select_from(LoginLog)
        for predicate in self._login_filters(
            keyword=keyword,
            status=status,
            user_id=user_id,
        ):
            stmt = stmt.where(predicate)
        total = await self.session.scalar(stmt)
        return int(total or 0)
