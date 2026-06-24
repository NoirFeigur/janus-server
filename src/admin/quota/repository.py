"""Admin quota rule data access."""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import func, or_, select
from sqlalchemy.orm.attributes import InstrumentedAttribute
from sqlalchemy.sql.elements import ColumnElement

from src.db.models.identity import Department, User
from src.db.models.model_catalog import LogicalModel
from src.db.models.quota import Quota
from src.db.repository import BaseRepository
from src.db.scope import DataScope


class QuotaRepository(BaseRepository[Quota]):
    model = Quota

    def _scope_predicate(
        self, scope_filter: DataScope, *, actor_id: int, include_global: bool
    ) -> ColumnElement[bool] | None:
        if scope_filter.unrestricted:
            # An `all_data` role sees every department/user quota, but
            # platform-level `global` quotas stay superuser-only. ``include_global``
            # is set iff the actor is a superuser, so an unrestricted *non*-superuser
            # must still have global rows filtered out (write/get paths already gate
            # global on is_superuser; this closes the list-path leak).
            if include_global:
                return None
            return Quota.scope != "global"
        clauses: list[ColumnElement[bool]] = []
        if include_global:
            clauses.append(Quota.scope == "global")
        if scope_filter.department_ids:
            clauses.append(
                (Quota.scope == "department")
                & Quota.scope_id.in_(scope_filter.department_ids)
            )
            user_ids = select(User.id).where(
                User.is_deleted.is_(False),
                User.department_id.in_(scope_filter.department_ids),
            )
            clauses.append((Quota.scope == "user") & Quota.scope_id.in_(user_ids))
        if scope_filter.include_self:
            clauses.append((Quota.scope == "user") & (Quota.scope_id == actor_id))
        if not clauses:
            return Quota.id == -1
        return or_(*clauses)

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
        scope_filter: DataScope | None = None,
        actor_id: int | None = None,
        include_global: bool = False,
    ) -> list[ColumnElement[bool]]:
        filters: list[ColumnElement[bool]] = [Quota.is_deleted.is_(False)]
        if scope_filter is not None and actor_id is not None:
            predicate = self._scope_predicate(
                scope_filter, actor_id=actor_id, include_global=include_global
            )
            if predicate is not None:
                filters.append(predicate)
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
        scope_filter: DataScope | None = None,
        actor_id: int | None = None,
        include_global: bool = False,
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
            scope_filter=scope_filter,
            actor_id=actor_id,
            include_global=include_global,
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
        scope_filter: DataScope | None = None,
        actor_id: int | None = None,
        include_global: bool = False,
    ) -> int:
        stmt = select(func.count()).select_from(Quota)
        for predicate in self._filters(
            scope=scope,
            scope_id=scope_id,
            logical_model_id=logical_model_id,
            status=status,
            scope_filter=scope_filter,
            actor_id=actor_id,
            include_global=include_global,
        ):
            stmt = stmt.where(predicate)
        total = await self.session.scalar(stmt)
        return int(total or 0)

    async def user_exists(self, user_id: int) -> bool:
        stmt = select(User.id).where(User.id == user_id, User.is_deleted.is_(False))
        return await self.session.scalar(stmt) is not None

    async def department_exists(self, dept_id: int) -> bool:
        stmt = select(Department.id).where(
            Department.id == dept_id, Department.is_deleted.is_(False)
        )
        return await self.session.scalar(stmt) is not None

    async def model_exists(self, model_id: int) -> bool:
        stmt = select(LogicalModel.id).where(
            LogicalModel.id == model_id, LogicalModel.is_deleted.is_(False)
        )
        return await self.session.scalar(stmt) is not None

    async def subject_in_scope(
        self, *, scope: str, scope_id: int | None, scope_filter: DataScope, actor_id: int
    ) -> bool:
        if scope_filter.unrestricted:
            return True
        if scope == "department" and scope_id is not None:
            return scope_id in scope_filter.department_ids
        if scope != "user" or scope_id is None:
            return False
        clauses: list[ColumnElement[bool]] = []
        if scope_filter.department_ids:
            clauses.append(User.department_id.in_(scope_filter.department_ids))
        if scope_filter.include_self:
            clauses.append(User.id == actor_id)
        if not clauses:
            return False
        stmt = select(User.id).where(
            User.id == scope_id, User.is_deleted.is_(False), or_(*clauses)
        )
        return await self.session.scalar(stmt) is not None
