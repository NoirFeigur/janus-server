"""Admin rate limits service — business logic for rate limit CRUD."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession
from starlette import status

from src.admin.rate_limits.repository import RateLimitRepository
from src.admin.rate_limits.schemas import RateLimitRuleCreate, RateLimitRuleUpdate
from src.auth.service import AuthenticatedUser, AuthService, DataScopeFilter
from src.core.pagination import PageResult
from src.core.query import ListQuery
from src.db.models.rate_limit import RateLimitRule
from src.enums import ErrorCode, RateLimitScope
from src.exceptions import AppError


class RateLimitService:
    """Admin service for rate limit rule management."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.repo = RateLimitRepository(session)
        self.auth = AuthService(session)

    async def _scope(self, actor: AuthenticatedUser) -> DataScopeFilter:
        return await self.auth.resolve_data_scope(actor)

    async def _validate_subject(
        self, payload: RateLimitRuleCreate, *, actor: AuthenticatedUser
    ) -> None:
        """Authorize + validate the rule's subject before persisting.

        - A platform-wide (``global``) rule is a blast-radius-wide control, so it
          is restricted to super-admins; a scoped admin must not throttle the
          whole platform.
        - For concrete scopes the referenced subject must actually exist, else a
          rule silently targets a non-existent id (schema already guarantees a
          non-null ``subject_id`` for these scopes).
        """
        if payload.subject_type == RateLimitScope.global_:
            if not actor.is_superuser:
                raise AppError(ErrorCode.auth_forbidden, status.HTTP_403_FORBIDDEN)
            return

        subject_id = payload.subject_id
        assert subject_id is not None  # guaranteed by RateLimitRuleCreate validator
        if payload.subject_type == RateLimitScope.user:
            exists = await self.repo.user_exists(subject_id)
        elif payload.subject_type == RateLimitScope.department:
            exists = await self.repo.department_exists(subject_id)
        else:  # RateLimitScope.api_key (enum is exhaustive; global_ returned above)
            exists = await self.repo.api_key_exists(subject_id)
        if not exists:
            raise AppError(ErrorCode.request_invalid, status.HTTP_400_BAD_REQUEST)
        scope_filter = await self._scope(actor)
        if not await self.repo.subject_in_scope(
            subject_type=payload.subject_type.value,
            subject_id=subject_id,
            scope_filter=scope_filter,
            actor_id=actor.user_id,
        ):
            raise AppError(ErrorCode.auth_forbidden, status.HTTP_403_FORBIDDEN)

    async def _require_visible_rule(
        self, rule_id: int, *, actor: AuthenticatedUser
    ) -> RateLimitRule:
        rule = await self.get_rule(rule_id)
        if rule.subject_type == RateLimitScope.global_.value:
            if not actor.is_superuser:
                raise AppError(ErrorCode.auth_forbidden, status.HTTP_403_FORBIDDEN)
            return rule
        scope_filter = await self._scope(actor)
        if not await self.repo.subject_in_scope(
            subject_type=rule.subject_type,
            subject_id=rule.subject_id,
            scope_filter=scope_filter,
            actor_id=actor.user_id,
        ):
            raise AppError(ErrorCode.auth_forbidden, status.HTTP_403_FORBIDDEN)
        return rule

    async def list_rules(
        self,
        query: ListQuery,
        *,
        subject_type: str | None = None,
        subject_id: int | None = None,
        logical_model_id: int | None = None,
        rule_status: str | None = None,
        actor: AuthenticatedUser | None = None,
    ) -> PageResult[RateLimitRule]:
        scope_filter = await self._scope(actor) if actor is not None else None
        return await self.repo.list_rules(
            query,
            subject_type=subject_type,
            subject_id=subject_id,
            logical_model_id=logical_model_id,
            status=rule_status,
            scope_filter=scope_filter,
            actor_id=actor.user_id if actor is not None else None,
            include_global=actor.is_superuser if actor is not None else False,
        )

    async def get_rule(self, rule_id: int) -> RateLimitRule:
        rule = await self.repo.get_by_id(rule_id)
        if rule is None:
            raise AppError(ErrorCode.request_invalid, status.HTTP_404_NOT_FOUND)
        return rule

    async def create_rule(
        self, payload: RateLimitRuleCreate, *, actor: AuthenticatedUser
    ) -> RateLimitRule:
        await self._validate_subject(payload, actor=actor)
        rule = RateLimitRule(
            subject_type=payload.subject_type.value,
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
        rule = await self._require_visible_rule(rule_id, actor=actor)

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
        rule = await self._require_visible_rule(rule_id, actor=actor)
        rule.updated_by = actor.user_id
        await self.repo.soft_delete(rule)
