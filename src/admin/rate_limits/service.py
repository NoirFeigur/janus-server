"""Admin rate limits service — business logic for rate limit CRUD."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession
from starlette import status

from src.admin.rate_limits.repository import RateLimitRepository
from src.admin.rate_limits.schemas import RateLimitRuleCreate, RateLimitRuleUpdate
from src.auth.service import AuthenticatedUser
from src.core.pagination import PageResult
from src.core.query import ListQuery
from src.db.models.rate_limit import RateLimitRule
from src.enums import ErrorCode
from src.exceptions import AppError


class RateLimitService:
    """Admin service for rate limit rule management."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.repo = RateLimitRepository(session)

    async def list_rules(
        self,
        query: ListQuery,
        *,
        subject_type: str | None = None,
        subject_id: int | None = None,
        logical_model_id: int | None = None,
        rule_status: str | None = None,
    ) -> PageResult[RateLimitRule]:
        return await self.repo.list_rules(
            query,
            subject_type=subject_type,
            subject_id=subject_id,
            logical_model_id=logical_model_id,
            status=rule_status,
        )

    async def get_rule(self, rule_id: int) -> RateLimitRule:
        rule = await self.repo.get_by_id(rule_id)
        if rule is None:
            raise AppError(ErrorCode.resource_not_found, status.HTTP_404_NOT_FOUND)
        return rule

    async def create_rule(
        self, payload: RateLimitRuleCreate, *, actor: AuthenticatedUser
    ) -> RateLimitRule:
        rule = RateLimitRule(
            subject_type=payload.subject_type,
            subject_id=payload.subject_id,
            logical_model_id=payload.logical_model_id,
            rpm_limit=payload.rpm_limit,
            tpm_limit=payload.tpm_limit,
            tpm_burst_limit=payload.tpm_burst_limit,
            max_concurrent=payload.max_concurrent,
            enforce=payload.enforce,
            remark=payload.remark,
            status="active",
            created_by=actor.user_id,
            create_dept=actor.department_id,
        )
        return await self.repo.create(rule)

    async def update_rule(
        self, rule_id: int, payload: RateLimitRuleUpdate, *, actor: AuthenticatedUser
    ) -> RateLimitRule:
        rule = await self.get_rule(rule_id)

        if payload.rpm_limit is not None:
            rule.rpm_limit = payload.rpm_limit
        if payload.tpm_limit is not None:
            rule.tpm_limit = payload.tpm_limit
        if payload.tpm_burst_limit is not None:
            rule.tpm_burst_limit = payload.tpm_burst_limit
        if payload.max_concurrent is not None:
            rule.max_concurrent = payload.max_concurrent
        if payload.enforce is not None:
            rule.enforce = payload.enforce
        if payload.status is not None:
            rule.status = payload.status
        if payload.remark is not None:
            rule.remark = payload.remark

        rule.updated_by = actor.user_id
        await self.session.flush()
        return rule

    async def delete_rule(self, rule_id: int, *, actor: AuthenticatedUser) -> None:
        rule = await self.get_rule(rule_id)
        rule.updated_by = actor.user_id
        await self.repo.soft_delete(rule)
