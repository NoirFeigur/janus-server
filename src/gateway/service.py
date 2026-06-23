from __future__ import annotations

from contextlib import suppress
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession
from starlette import status

from src.auth.service import AuthenticatedUser
from src.db.models.model_catalog import LogicalModel
from src.db.session import async_session_factory
from src.enums import ErrorCode
from src.exceptions import AppError
from src.gateway.quota import QuotaCheckResult, QuotaEnforcer, QuotaLimitExceeded
from src.gateway.repository import GatewayRepository


class GatewayService:
    def __init__(self, session: AsyncSession) -> None:
        self.repo = GatewayRepository(session)
        self.quota = QuotaEnforcer()

    async def resolve_model(
        self, user: AuthenticatedUser, requested_model: str | None
    ) -> LogicalModel:
        if requested_model is None:
            default_model_id = await self.repo.get_default_model_id(
                user.user_id, user.department_id
            )
            if default_model_id is None:
                raise AppError(ErrorCode.model_not_found, status.HTTP_404_NOT_FOUND)
            logical_model = await self.repo.get_logical_model_by_id(default_model_id)
        else:
            logical_model = await self.repo.get_logical_model_by_name(requested_model)
        if logical_model is None:
            raise AppError(ErrorCode.model_not_found, status.HTTP_404_NOT_FOUND)
        grants = await self.repo.get_user_granted_models(user.user_id, user.department_id)
        if logical_model.id not in grants:
            raise AppError(
                ErrorCode.model_not_granted,
                status.HTTP_403_FORBIDDEN,
                params={"model": logical_model.name},
            )
        return logical_model

    async def check_quota(
        self,
        user_id: int,
        department_id: int | None,
        logical_model_id: int | None = None,
    ) -> QuotaCheckResult:
        legacy_call = logical_model_id is None
        if logical_model_id is None:
            logical_model_id = int(department_id or 0)
            department_id = None
        quotas = await self.repo.get_active_quotas(
            user_id, department_id, logical_model_id
        )
        try:
            if legacy_call:
                return await self.quota.check_and_increment(
                    user_id, logical_model_id, quotas
                )
            return await self.quota.check_and_increment(
                user_id, department_id, logical_model_id, quotas
            )
        except QuotaLimitExceeded as exc:
            raise AppError(
                ErrorCode.quota_exceeded,
                status.HTTP_429_TOO_MANY_REQUESTS,
                params={
                    "quota_id": exc.exceeded.quota.id,
                    "metric": exc.exceeded.quota.metric,
                    "period": exc.exceeded.quota.period,
                    "limit": str(exc.exceeded.quota.limit_value),
                    "current": str(exc.exceeded.current),
                },
            ) from exc

    async def settle_quota(
        self,
        user_id: int,
        department_id: int | None,
        logical_model_id: int,
        actual_tokens: int,
        actual_cost: Decimal | None,
    ) -> None:
        # Re-querying active quotas can use a newer quota set after hot reloads.
        # The 30s rebuild interval and period-stamped Redis keys make this acceptable.
        quotas = await self.repo.get_active_quotas(
            user_id, department_id, logical_model_id
        )
        await self.quota.settle(
            user_id,
            department_id,
            logical_model_id,
            quotas,
            actual_tokens,
            actual_cost,
        )


async def settle_quota_independent(
    *,
    user_id: int,
    department_id: int | None,
    logical_model_id: int,
    actual_tokens: int,
    actual_cost: Decimal | None,
) -> None:
    """Settle quota using an independent session after request scope closes."""
    # Re-querying active quotas can use a newer quota set after hot reloads.
    # The 30s rebuild interval and period-stamped Redis keys make this acceptable.
    with suppress(Exception):
        async with async_session_factory() as session:
            quotas = await GatewayRepository(session).get_active_quotas(
                user_id, department_id, logical_model_id
            )
            await QuotaEnforcer().settle(
                user_id,
                department_id,
                logical_model_id,
                quotas,
                actual_tokens,
                actual_cost,
            )
