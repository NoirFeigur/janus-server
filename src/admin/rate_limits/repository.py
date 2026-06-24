"""Admin rate limits repository — data access for RateLimitRule."""

from __future__ import annotations

from sqlalchemy import desc, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from src.core.pagination import PageResult, page_result
from src.core.query import ListQuery, resolve_sort
from src.db.models.credential import ApiKey
from src.db.models.identity import Department, User
from src.db.models.rate_limit import RateLimitRule
from src.db.scope import DataScope

SORT_COLUMNS = {
    "id": RateLimitRule.id,
    "subject_type": RateLimitRule.subject_type,
    "status": RateLimitRule.status,
    "created_at": RateLimitRule.created_at,
}


class RateLimitRepository:
    """Data access for rate limit rules."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    def _scope_predicate(
        self, scope_filter: DataScope, *, actor_id: int, include_global: bool
    ) -> ColumnElement[bool] | None:
        if scope_filter.unrestricted:
            if include_global:
                return None
            return RateLimitRule.subject_type != "global"

        clauses: list[ColumnElement[bool]] = []
        if include_global:
            clauses.append(RateLimitRule.subject_type == "global")
        if scope_filter.department_ids:
            visible_users = select(User.id).where(
                User.is_deleted == False,  # noqa: E712
                User.department_id.in_(scope_filter.department_ids),
            )
            visible_keys = select(ApiKey.id).where(
                ApiKey.is_deleted == False,  # noqa: E712
                ApiKey.user_id.in_(visible_users),
            )
            clauses.extend(
                [
                    (RateLimitRule.subject_type == "department")
                    & RateLimitRule.subject_id.in_(scope_filter.department_ids),
                    (RateLimitRule.subject_type == "user")
                    & RateLimitRule.subject_id.in_(visible_users),
                    (RateLimitRule.subject_type == "api_key")
                    & RateLimitRule.subject_id.in_(visible_keys),
                ]
            )
        if scope_filter.include_self:
            own_keys = select(ApiKey.id).where(
                ApiKey.is_deleted == False,  # noqa: E712
                ApiKey.user_id == actor_id,
            )
            clauses.extend(
                [
                    (RateLimitRule.subject_type == "user")
                    & (RateLimitRule.subject_id == actor_id),
                    (RateLimitRule.subject_type == "api_key")
                    & RateLimitRule.subject_id.in_(own_keys),
                ]
            )
        if not clauses:
            return RateLimitRule.id == -1
        return or_(*clauses)

    def _filters(
        self,
        *,
        subject_type: str | None,
        subject_id: int | None,
        logical_model_id: int | None,
        status: str | None,
        scope_filter: DataScope | None = None,
        actor_id: int | None = None,
        include_global: bool = False,
    ) -> list[ColumnElement[bool]]:
        filters: list[ColumnElement[bool]] = [RateLimitRule.is_deleted == False]  # noqa: E712
        if scope_filter is not None and actor_id is not None:
            predicate = self._scope_predicate(
                scope_filter, actor_id=actor_id, include_global=include_global
            )
            if predicate is not None:
                filters.append(predicate)
        if subject_type is not None:
            filters.append(RateLimitRule.subject_type == subject_type)
        if subject_id is not None:
            filters.append(RateLimitRule.subject_id == subject_id)
        if logical_model_id is not None:
            filters.append(RateLimitRule.logical_model_id == logical_model_id)
        if status is not None:
            filters.append(RateLimitRule.status == status)
        return filters

    async def list_rules(
        self,
        query: ListQuery,
        *,
        subject_type: str | None = None,
        subject_id: int | None = None,
        logical_model_id: int | None = None,
        status: str | None = None,
        scope_filter: DataScope | None = None,
        actor_id: int | None = None,
        include_global: bool = False,
    ) -> PageResult[RateLimitRule]:
        filters = self._filters(
            subject_type=subject_type,
            subject_id=subject_id,
            logical_model_id=logical_model_id,
            status=status,
            scope_filter=scope_filter,
            actor_id=actor_id,
            include_global=include_global,
        )
        stmt = select(RateLimitRule)
        for predicate in filters:
            stmt = stmt.where(predicate)

        order_col, is_desc = resolve_sort(query, allowed=SORT_COLUMNS, default="id")
        stmt = stmt.order_by(desc(order_col) if is_desc else order_col)

        # Count
        count_stmt = select(func.count()).select_from(RateLimitRule)
        for predicate in filters:
            count_stmt = count_stmt.where(predicate)

        total = await self.session.scalar(count_stmt) or 0

        stmt = stmt.offset(query.offset).limit(query.limit)
        result = await self.session.execute(stmt)
        items = list(result.scalars().all())

        return page_result(
            items=items, total=int(total), limit=query.limit, offset=query.offset
        )

    async def get_by_id(self, rule_id: int) -> RateLimitRule | None:
        result = await self.session.execute(
            select(RateLimitRule)
            .where(RateLimitRule.id == rule_id)
            .where(RateLimitRule.is_deleted == False)  # noqa: E712
        )
        return result.scalar_one_or_none()

    async def user_exists(self, user_id: int) -> bool:
        stmt = select(User.id).where(
            User.id == user_id, User.is_deleted == False  # noqa: E712
        )
        return await self.session.scalar(stmt) is not None

    async def department_exists(self, dept_id: int) -> bool:
        stmt = select(Department.id).where(
            Department.id == dept_id, Department.is_deleted == False  # noqa: E712
        )
        return await self.session.scalar(stmt) is not None

    async def api_key_exists(self, key_id: int) -> bool:
        stmt = select(ApiKey.id).where(
            ApiKey.id == key_id, ApiKey.is_deleted == False  # noqa: E712
        )
        return await self.session.scalar(stmt) is not None

    async def subject_in_scope(
        self,
        *,
        subject_type: str,
        subject_id: int | None,
        scope_filter: DataScope,
        actor_id: int,
    ) -> bool:
        if subject_type == "global":
            return False
        if scope_filter.unrestricted:
            return True
        if subject_id is None:
            return False
        if subject_type == "department":
            return subject_id in scope_filter.department_ids
        if subject_type == "user":
            clauses: list[ColumnElement[bool]] = []
            if scope_filter.department_ids:
                clauses.append(User.department_id.in_(scope_filter.department_ids))
            if scope_filter.include_self:
                clauses.append(User.id == actor_id)
            if not clauses:
                return False
            stmt = select(User.id).where(
                User.id == subject_id,
                User.is_deleted == False,  # noqa: E712
                or_(*clauses),
            )
            return await self.session.scalar(stmt) is not None
        if subject_type == "api_key":
            clauses = []
            if scope_filter.department_ids:
                owner_ids = select(User.id).where(
                    User.is_deleted == False,  # noqa: E712
                    User.department_id.in_(scope_filter.department_ids),
                )
                clauses.append(ApiKey.user_id.in_(owner_ids))
            if scope_filter.include_self:
                clauses.append(ApiKey.user_id == actor_id)
            if not clauses:
                return False
            stmt = select(ApiKey.id).where(
                ApiKey.id == subject_id,
                ApiKey.is_deleted == False,  # noqa: E712
                or_(*clauses),
            )
            return await self.session.scalar(stmt) is not None
        return False

    async def create(self, rule: RateLimitRule) -> RateLimitRule:
        self.session.add(rule)
        await self.session.flush()
        return rule

    async def soft_delete(self, rule: RateLimitRule) -> None:
        rule.is_deleted = True
        await self.session.flush()
