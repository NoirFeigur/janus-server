"""Admin rate limits repository — data access for RateLimitRule."""

from __future__ import annotations

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.pagination import PageResult, page_result
from src.core.query import ListQuery, resolve_sort
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

    async def list_rules(
        self,
        query: ListQuery,
        *,
        subject_type: str | None = None,
        subject_id: int | None = None,
        logical_model_id: int | None = None,
        status: str | None = None,
    ) -> PageResult[RateLimitRule]:
        stmt = select(RateLimitRule).where(RateLimitRule.is_deleted == False)  # noqa: E712

        if subject_type is not None:
            stmt = stmt.where(RateLimitRule.subject_type == subject_type)
        if subject_id is not None:
            stmt = stmt.where(RateLimitRule.subject_id == subject_id)
        if logical_model_id is not None:
            stmt = stmt.where(RateLimitRule.logical_model_id == logical_model_id)
        if status is not None:
            stmt = stmt.where(RateLimitRule.status == status)

        order_col = resolve_sort(SORT_COLUMNS, query.sort_by, default=RateLimitRule.id)
        if query.sort_order == "desc":
            stmt = stmt.order_by(desc(order_col))
        else:
            stmt = stmt.order_by(order_col)

        # Count
        count_stmt = select(RateLimitRule.id).where(RateLimitRule.is_deleted == False)  # noqa: E712
        if subject_type is not None:
            count_stmt = count_stmt.where(RateLimitRule.subject_type == subject_type)
        if subject_id is not None:
            count_stmt = count_stmt.where(RateLimitRule.subject_id == subject_id)
        if logical_model_id is not None:
            count_stmt = count_stmt.where(RateLimitRule.logical_model_id == logical_model_id)
        if status is not None:
            count_stmt = count_stmt.where(RateLimitRule.status == status)

        count_result = await self.session.execute(count_stmt)
        total = len(count_result.all())

        stmt = stmt.offset(query.offset).limit(query.limit)
        result = await self.session.execute(stmt)
        items = list(result.scalars().all())

        return page_result(items=items, total=total, limit=query.limit, offset=query.offset)

    async def get_by_id(self, rule_id: int) -> RateLimitRule | None:
        result = await self.session.execute(
            select(RateLimitRule)
            .where(RateLimitRule.id == rule_id)
            .where(RateLimitRule.is_deleted == False)  # noqa: E712
        )
        return result.scalar_one_or_none()

    async def create(self, rule: RateLimitRule) -> RateLimitRule:
        self.session.add(rule)
        await self.session.flush()
        return rule

    async def soft_delete(self, rule: RateLimitRule) -> None:
        rule.is_deleted = True
        await self.session.flush()
