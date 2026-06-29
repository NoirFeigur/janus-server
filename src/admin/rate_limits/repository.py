"""Admin rate limits repository — data access for RateLimitRule."""

from __future__ import annotations

from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from src.core.pagination import PageResult, page_result
from src.core.query import ListQuery, resolve_sort
from src.db.models.credential import ApiKey
from src.db.models.identity import Department, User
from src.db.models.rate_limit import RateLimitRule

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

    def _global_predicate(self, *, include_global: bool) -> ColumnElement[bool] | None:
        """Platform-level ``global`` rate-limit rules are superuser-only.

        ``include_global`` is set iff the actor is a superuser. Non-superuser
        admins manage every department/user/api_key rule but never the
        platform-level ``global`` rows (write/get paths already gate global on
        is_superuser; this closes the list-path leak).
        """
        if include_global:
            return None
        return RateLimitRule.subject_type != "global"

    def _filters(
        self,
        *,
        subject_type: str | None,
        subject_id: int | None,
        logical_model_id: int | None,
        status: str | None,
        include_global: bool = False,
    ) -> list[ColumnElement[bool]]:
        filters: list[ColumnElement[bool]] = [RateLimitRule.is_deleted == False]  # noqa: E712
        predicate = self._global_predicate(include_global=include_global)
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
        include_global: bool = False,
    ) -> PageResult[RateLimitRule]:
        filters = self._filters(
            subject_type=subject_type,
            subject_id=subject_id,
            logical_model_id=logical_model_id,
            status=status,
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

    async def create(self, rule: RateLimitRule) -> RateLimitRule:
        self.session.add(rule)
        await self.session.flush()
        return rule

    async def soft_delete(self, rule: RateLimitRule) -> None:
        rule.is_deleted = True
        await self.session.flush()
