from __future__ import annotations

from decimal import Decimal
from typing import Any

from redis.exceptions import RedisError
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette import status

from src.auth.service import AuthenticatedUser
from src.core.logging import get_logger
from src.db.models.model_catalog import LogicalModel
from src.db.models.quota import Quota
from src.db.models.rate_limit import RateLimitRule
from src.enums import ErrorCode
from src.exceptions import AppError
from src.gateway.cache import (
    get_cached_quota_config,
    get_cached_resolution,
    set_cached_quota_config,
    set_cached_resolution,
)
from src.gateway.quota import QuotaCheckResult, QuotaEnforcer, QuotaLimitExceeded
from src.gateway.repository import GatewayRepository

_log = get_logger(__name__)

# Sentinel cache-key fragment for the "no model requested → resolve the user's
# default" path. A real model name can never collide (names are validated; NUL
# is not a legal model name). The default resolution depends on grants, which
# the cache key already versions via the grant generation counter, so caching it
# under this sentinel stays correct across grant changes.
_DEFAULT_MODEL_SENTINEL = "\x00__janus_default__"


def _logical_model_to_cache(model: LogicalModel) -> dict[str, Any]:
    """Serialize the subset of LogicalModel fields the hot path reads.

    Downstream consumers only touch id/name/price_input/price_output, so we cache
    exactly those. Decimal prices are stored as strings to preserve the full
    Numeric(12,6) precision across the JSON round-trip.
    """
    return {
        "id": model.id,
        "name": model.name,
        "price_input": str(model.price_input) if model.price_input is not None else None,
        "price_output": str(model.price_output) if model.price_output is not None else None,
    }


def _logical_model_from_cache(data: dict[str, Any]) -> LogicalModel:
    """Rebuild a transient (session-detached) LogicalModel from cached fields.

    Never flushed/persisted — only the cached scalar fields are read downstream.
    """
    price_input = data.get("price_input")
    price_output = data.get("price_output")
    return LogicalModel(
        id=data["id"],
        name=data["name"],
        price_input=Decimal(price_input) if price_input is not None else None,
        price_output=Decimal(price_output) if price_output is not None else None,
    )


def _quota_to_cache(quota: Quota) -> dict[str, Any]:
    """Serialize the Quota rule fields needed for keying, checking, and errors.

    limit_value (Numeric(14,6)) is stored as a string to keep exact precision.
    """
    return {
        "id": quota.id,
        "scope": quota.scope,
        "scope_id": quota.scope_id,
        "logical_model_id": quota.logical_model_id,
        "period": quota.period,
        "metric": quota.metric,
        "limit_value": str(quota.limit_value),
        "enforce": quota.enforce,
    }


def _quota_from_cache(data: dict[str, Any]) -> Quota:
    """Rebuild a transient (session-detached) Quota from cached fields."""
    return Quota(
        id=data["id"],
        scope=data["scope"],
        scope_id=data["scope_id"],
        logical_model_id=data["logical_model_id"],
        period=data["period"],
        metric=data["metric"],
        limit_value=Decimal(data["limit_value"]),
        enforce=data["enforce"],
    )


class GatewayService:
    def __init__(self, session: AsyncSession) -> None:
        self.repo = GatewayRepository(session)
        self.quota = QuotaEnforcer()

    async def resolve_model(
        self, user: AuthenticatedUser, requested_model: str | None
    ) -> LogicalModel:
        # Cache key fragment: real model name, or the default-resolution sentinel.
        # The cache key embeds catalog + grant generations, so a catalog edit or a
        # grant change to this user/dept makes prior entries unaddressable — which
        # is why caching the positive (resolved + granted) result is safe.
        cache_model_key = (
            requested_model if requested_model is not None else _DEFAULT_MODEL_SENTINEL
        )
        cached = await get_cached_resolution(user.user_id, user.department_id, cache_model_key)
        if cached is not None:
            return _logical_model_from_cache(cached)

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
        # Cache the positive result only (resolved + granted). Negative results
        # (not found / not granted) are not cached: they are cheap to recompute
        # and must reflect grant/catalog changes immediately.
        await set_cached_resolution(
            user.user_id,
            user.department_id,
            cache_model_key,
            _logical_model_to_cache(logical_model),
        )
        return logical_model

    async def check_quota(
        self,
        user_id: int,
        department_id: int | None,
        logical_model_id: int,
        estimated_tokens: int = 0,
        estimated_cost: Decimal | None = None,
    ) -> QuotaCheckResult:
        quotas = await self._get_active_quotas_cached(
            user_id, department_id, logical_model_id
        )
        try:
            return await self.quota.check_and_increment(
                user_id,
                department_id,
                logical_model_id,
                quotas,
                estimated_tokens=estimated_tokens,
                estimated_cost=estimated_cost,
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
        except RedisError as exc:
            # Quota is spend control: fail CLOSED on a Redis outage. Allowing
            # traffic through with the counter blind risks unbounded overspend, so
            # reject with a transient, retryable 503 rather than a 500 (which would
            # read as an unexpected bug) — and never silently bypass the limit.
            #
            # NOTE (operational blind spot): rate limiting fails OPEN on a Redis
            # outage (see rate_limit.check_rate_limits) while quota fails CLOSED
            # here. The combination is safe ONLY when a quota rule exists for the
            # caller — quota then back-stops the open rate limiter. A user/model
            # with NO quota rule configured has nothing to fail closed: during a
            # Redis outage the open rate limiter admits the request and quota
            # returns "no rules → pass", so that path is effectively unlimited.
            # Mitigation is operational (ensure a global quota exists, monitor
            # Redis), not code — documented so it is a known risk, not a surprise.
            _log.warning("quota.redis_unavailable", user_id=user_id)
            raise AppError(
                ErrorCode.service_unavailable,
                status.HTTP_503_SERVICE_UNAVAILABLE,
            ) from exc

    async def _get_active_quotas_cached(
        self,
        user_id: int,
        department_id: int | None,
        logical_model_id: int,
    ) -> list[Quota]:
        """Fetch active quota *rules* via cache-aside (counters stay live in Redis).

        Only the rule config is cached — ``check_and_increment`` always runs the
        Redis Lua against live counters, so caching the config never stales a
        count. The empty-list result is cached too: a user with no quota rules is
        the common case, and recomputing it every request would defeat the cache.
        Generation counters (bumped on quota admin writes) version the key, so a
        rule change invalidates prior entries immediately.
        """
        cached = await get_cached_quota_config(user_id, department_id, logical_model_id)
        if cached is not None:
            return [_quota_from_cache(item) for item in cached]
        quotas = await self.repo.get_active_quotas(user_id, department_id, logical_model_id)
        await set_cached_quota_config(
            user_id,
            department_id,
            logical_model_id,
            [_quota_to_cache(q) for q in quotas],
        )
        return list(quotas)

    async def get_rate_limit_rules(
        self,
        user_id: int,
        department_id: int | None,
        logical_model_id: int,
        api_key_id: int | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch active rate limit rules applicable to this user/model (fail-open).

        Returns matching rules as dicts for the rate_limit module.
        Lookup order: user-specific → department → global, all filtered by model.

        Fail-open is deliberate: a DB/Redis blip must not wedge the gateway, so a
        lookup failure yields ``[]`` (no limits enforced) rather than a 500. But
        narrow the catch to operational errors (``SQLAlchemyError``) and log it —
        a blanket ``except Exception`` would also swallow programming errors
        (e.g. a bad column reference) as a silent "no limits", masking real bugs.
        """
        session = self.repo.session
        stmt = (
            select(RateLimitRule)
            .where(RateLimitRule.is_deleted == False)  # noqa: E712
            .where(RateLimitRule.status == "active")
            .where(RateLimitRule.enforce == True)  # noqa: E712
            .where(
                (RateLimitRule.logical_model_id == logical_model_id)
                | (RateLimitRule.logical_model_id.is_(None))
            )
            .where(
                (
                    (RateLimitRule.subject_type == "user")
                    & (RateLimitRule.subject_id == user_id)
                )
                | (
                    (RateLimitRule.subject_type == "department")
                    & (RateLimitRule.subject_id == department_id)
                )
                | (
                    (RateLimitRule.subject_type == "api_key")
                    & (RateLimitRule.subject_id == api_key_id)
                )
                | (RateLimitRule.subject_type == "global")
            )
        )
        try:
            result = await session.execute(stmt)
            rules = result.scalars().all()
        except (SQLAlchemyError, RedisError) as exc:
            _log.warning(
                "gateway.rate_limit_rules.lookup_failed",
                user_id=user_id,
                logical_model_id=logical_model_id,
                error=str(exc),
            )
            return []
        return [
            {
                "id": r.id,
                "subject_type": r.subject_type,
                "subject_id": r.subject_id,
                "logical_model_id": r.logical_model_id,
                "rpm_limit": r.rpm_limit,
                "tpm_limit": r.tpm_limit,
                "tpm_burst_limit": r.tpm_burst_limit,
                "max_concurrent": r.max_concurrent,
            }
            for r in rules
        ]
