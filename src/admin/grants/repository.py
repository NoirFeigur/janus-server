"""Admin model-grant data access (repository layer)."""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import func, or_, select
from sqlalchemy.orm.attributes import InstrumentedAttribute
from sqlalchemy.sql.elements import ColumnElement

from src.db.models.grant import UserModelGrant
from src.db.models.identity import Department, User
from src.db.models.model_catalog import LogicalModel
from src.db.repository import BaseRepository
from src.db.scope import DataScope

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
        self, *, scope: str, scope_id: int, for_update: bool = False
    ) -> UserModelGrant | None:
        stmt = select(UserModelGrant).where(
            UserModelGrant.scope == scope,
            UserModelGrant.scope_id == scope_id,
            UserModelGrant.is_default.is_(True),
            UserModelGrant.is_deleted.is_(False),
        )
        if for_update:
            stmt = stmt.with_for_update()
        grant: UserModelGrant | None = await self.session.scalar(stmt)
        return grant

    def _scope_predicate(
        self, scope_filter: DataScope, *, actor_id: int
    ) -> ColumnElement[bool] | None:
        if scope_filter.unrestricted:
            return None
        clauses: list[ColumnElement[bool]] = []
        if scope_filter.department_ids:
            clauses.append(
                (UserModelGrant.scope == "department")
                & UserModelGrant.scope_id.in_(scope_filter.department_ids)
            )
            user_ids = select(User.id).where(
                User.is_deleted.is_(False),
                User.department_id.in_(scope_filter.department_ids),
            )
            clauses.append(
                (UserModelGrant.scope == "user")
                & UserModelGrant.scope_id.in_(user_ids)
            )
        if scope_filter.include_self:
            clauses.append(
                (UserModelGrant.scope == "user") & (UserModelGrant.scope_id == actor_id)
            )
        if not clauses:
            return UserModelGrant.id == -1
        return or_(*clauses)

    def _filters(
        self,
        *,
        keyword: str | None,
        scope: str | None,
        scope_id: int | None,
        logical_model_id: int | None,
        scope_filter: DataScope | None = None,
        actor_id: int | None = None,
    ) -> list[ColumnElement[bool]]:
        filters: list[ColumnElement[bool]] = [UserModelGrant.is_deleted.is_(False)]
        if scope_filter is not None and actor_id is not None:
            predicate = self._scope_predicate(scope_filter, actor_id=actor_id)
            if predicate is not None:
                filters.append(predicate)
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
        scope_filter: DataScope | None = None,
        actor_id: int | None = None,
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
            scope_filter=scope_filter,
            actor_id=actor_id,
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
        scope_filter: DataScope | None = None,
        actor_id: int | None = None,
    ) -> int:
        stmt = select(func.count()).select_from(UserModelGrant)
        for predicate in self._filters(
            keyword=keyword,
            scope=scope,
            scope_id=scope_id,
            logical_model_id=logical_model_id,
            scope_filter=scope_filter,
            actor_id=actor_id,
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
        self, *, scope: str, scope_id: int, scope_filter: DataScope, actor_id: int
    ) -> bool:
        if scope_filter.unrestricted:
            return True
        if scope == "department":
            return scope_id in scope_filter.department_ids
        if scope != "user":
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
