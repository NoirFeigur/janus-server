from __future__ import annotations

import logging
from collections.abc import Awaitable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, cast

from src.core.redis import AsyncRedis, get_redis
from src.db.models.quota import Quota
from src.enums import QuotaMetric, QuotaPeriod, QuotaScope

logger = logging.getLogger(__name__)

_QUOTA_CHECK_LUA = """
-- KEYS: quota redis keys (one per quota rule)
-- ARGV: [num_quotas, ttl1, limit1, enforce1, ttl2, limit2, enforce2, ...]
-- Returns: {0=pass/1=fail, failed_index (0-based, -1 if pass), current_count}
local n = tonumber(ARGV[1])
local incremented = {}
for i = 1, n do
    local base = 1 + (i-1)*3
    local ttl = tonumber(ARGV[base+1])
    local limit = tonumber(ARGV[base+2])
    local enforce = tonumber(ARGV[base+3])
    local count = redis.call('INCRBY', KEYS[i], 1)
    if count == 1 and ttl > 0 then
        redis.call('EXPIRE', KEYS[i], ttl)
    end
    table.insert(incremented, i)
    if enforce == 1 and count > limit then
        for _, idx in ipairs(incremented) do
            redis.call('DECRBY', KEYS[idx], 1)
        end
        return {1, i-1, count}
    end
end
return {0, -1, -1}
"""


@dataclass(frozen=True, slots=True)
class QuotaWarning:
    quota_id: int
    metric: str
    period: str
    current: Decimal
    limit: Decimal


@dataclass(frozen=True, slots=True)
class QuotaReservation:
    """A quota counter reserved at check time.

    Captures the *resolved* Redis key plus the scalar quota attributes needed to
    settle.  Settlement targets this exact key, so it is immune to quota config
    hot-reloads or period rollover between check and settle (the divergence
    window is unbounded for long streams).  Reservation and settlement always
    balance on the same counter.
    """

    key: str
    quota_id: int
    metric: str
    scope: str
    enforce: bool
    limit_value: Decimal


@dataclass(frozen=True, slots=True)
class QuotaCheckResult:
    passed: bool
    warnings: list[QuotaWarning] = field(default_factory=list)
    reservations: list[QuotaReservation] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class QuotaExceeded:
    quota: Quota
    current: Decimal


class QuotaLimitExceeded(RuntimeError):
    def __init__(self, exceeded: QuotaExceeded) -> None:
        self.exceeded = exceeded
        super().__init__("quota exceeded")


class QuotaEnforcer:
    """Redis-backed quota checking and compensation primitives."""

    def __init__(self) -> None:
        self._check_script: Any | None = None

    async def check_and_increment(
        self,
        user_id: int,
        department_id: int | None,
        logical_model_id: int | Sequence[Quota],
        quotas: Sequence[Quota] | None = None,
    ) -> QuotaCheckResult:
        legacy_key_format = quotas is None
        if quotas is None:
            quotas = cast("Sequence[Quota]", logical_model_id)
            model_id = int(department_id or 0)
            department_id = None
        else:
            # mypy: narrow the int | Sequence[Quota] union now that we know the
            # caller used the modern (logical_model_id, quotas=...) form.
            assert isinstance(logical_model_id, int)
            model_id = logical_model_id
        if not quotas:
            return QuotaCheckResult(passed=True)
        redis = get_redis()
        keys = [
            self._key_for_quota(
                user_id,
                department_id,
                model_id,
                quota,
                legacy_key_format=legacy_key_format,
            )
            for quota in quotas
        ]
        argv: list[str | int] = [len(quotas)]
        for quota in quotas:
            # Cost quotas use micro-units (×1_000_000) for Redis integer precision.
            limit = (
                int(quota.limit_value * 1_000_000)
                if quota.metric == QuotaMetric.cost.value
                else int(quota.limit_value)
            )
            argv.extend(
                [
                    self._ttl_seconds(quota.period) or 0,
                    limit,
                    int(quota.enforce),
                ]
            )
        result = await self._run_check(redis, keys, argv)
        failed = result[0] == 1
        if failed:
            failed_index = result[1]
            failed_quota = quotas[failed_index]
            # result[2] is the raw Redis counter (micro-units for cost); convert
            # back to points so `current` is comparable with `limit_value`.
            current = self._points_from_redis(
                failed_quota.metric, Decimal(result[2])
            )
            raise QuotaLimitExceeded(
                QuotaExceeded(quota=failed_quota, current=current)
            )

        warnings: list[QuotaWarning] = []
        for key, quota in zip(keys, quotas, strict=True):
            count = await self._count(redis, key)
            current = self._points_from_redis(quota.metric, Decimal(count))
            if not quota.enforce and current > quota.limit_value:
                warnings.append(
                    QuotaWarning(
                        quota_id=quota.id,
                        metric=quota.metric,
                        period=quota.period,
                        current=current,
                        limit=quota.limit_value,
                    )
                )
        reservations = [
            QuotaReservation(
                key=key,
                quota_id=quota.id,
                metric=quota.metric,
                scope=quota.scope,
                enforce=quota.enforce,
                limit_value=quota.limit_value,
            )
            for key, quota in zip(keys, quotas, strict=True)
        ]
        return QuotaCheckResult(passed=True, warnings=warnings, reservations=reservations)

    async def compensate(
        self,
        user_id: int,
        department_id: int | None,
        logical_model_id: int | Sequence[Quota],
        quotas: Sequence[Quota] | None = None,
    ) -> None:
        legacy_key_format = quotas is None
        if quotas is None:
            quotas = cast("Sequence[Quota]", logical_model_id)
            model_id = int(department_id or 0)
            department_id = None
        else:
            assert isinstance(logical_model_id, int)
            model_id = logical_model_id
        redis = get_redis()
        for quota in quotas:
            await redis.decr(
                self._key_for_quota(
                    user_id,
                    department_id,
                    model_id,
                    quota,
                    legacy_key_format=legacy_key_format,
                )
            )

    async def settle(
        self,
        user_id: int,
        department_id: int | None,
        logical_model_id: int,
        quotas: Sequence[Quota],
        actual_tokens: int,
        actual_cost: Decimal | None,
    ) -> None:
        """Adjust reservation to actual usage. Called after LLM response completes."""

        redis = get_redis()
        for quota in quotas:
            # Pre-flight reserves 1 unit for fairness; post-flight settles actual.
            # Under high concurrency near limits, brief over-quota is possible.
            adjustment = self._settlement_adjustment(quota, actual_tokens, actual_cost)
            if adjustment == 0:
                continue
            key = self._key_for_quota(user_id, department_id, logical_model_id, quota)
            await redis.incrby(
                key,
                adjustment,
            )
            current = Decimal(await self._count(redis, key))
            limit = self._redis_limit(quota)
            if quota.enforce and current > limit:
                logger.warning(
                    "quota exceeded after settlement",
                    extra={
                        "quota_id": quota.id,
                        "metric": quota.metric,
                        "scope": quota.scope,
                        "current": str(current),
                        "limit": str(limit),
                    },
                )

    async def settle_reservations(
        self,
        reservations: Sequence[QuotaReservation],
        actual_tokens: int,
        actual_cost: Decimal | None,
    ) -> None:
        """Settle against the exact keys reserved at check time (hot-reload safe).

        Targets the resolved Redis keys captured by ``check_and_increment`` so
        reservation and settlement always balance on the same counter, even if
        the quota config hot-reloaded or the period rolled over mid-request.
        """
        redis = get_redis()
        for res in reservations:
            adjustment = self._settlement_adjustment_for_metric(
                res.metric, actual_tokens, actual_cost
            )
            if adjustment == 0:
                continue
            await redis.incrby(res.key, adjustment)
            current = Decimal(await self._count(redis, res.key))
            limit = self._redis_limit_for(res.metric, res.limit_value)
            if res.enforce and current > limit:
                logger.warning(
                    "quota exceeded after settlement",
                    extra={
                        "quota_id": res.quota_id,
                        "metric": res.metric,
                        "scope": res.scope,
                        "current": str(current),
                        "limit": str(limit),
                    },
                )

    async def compensate_reservations(
        self,
        reservations: Sequence[QuotaReservation],
    ) -> None:
        """Give back the +1 pre-flight reservation on the exact keys reserved."""
        redis = get_redis()
        for res in reservations:
            await redis.decr(res.key)

    @staticmethod
    def _settlement_adjustment_for_metric(
        metric: str, actual_tokens: int, actual_cost: Decimal | None
    ) -> int:
        if metric == QuotaMetric.requests.value:
            return 0
        if metric == QuotaMetric.tokens.value:
            return actual_tokens - 1
        if metric == QuotaMetric.cost.value:
            # Cost stored in micro-units (×1_000_000); reservation was 1 micro-unit.
            micro_cost = int((actual_cost or Decimal(0)) * 1_000_000)
            return micro_cost - 1
        return 0

    @staticmethod
    def _redis_limit_for(metric: str, limit_value: Decimal) -> Decimal:
        if metric == QuotaMetric.cost.value:
            return limit_value * Decimal(1_000_000)
        return limit_value

    @staticmethod
    def _settlement_adjustment(
        quota: Quota, actual_tokens: int, actual_cost: Decimal | None
    ) -> int:
        if quota.metric == QuotaMetric.requests.value:
            return 0
        if quota.metric == QuotaMetric.tokens.value:
            return actual_tokens - 1
        if quota.metric == QuotaMetric.cost.value:
            # Cost stored in micro-units (×1_000_000) for Redis integer precision.
            # Reservation was 1 micro-unit; settle to actual micro-cost.
            micro_cost = int((actual_cost or Decimal(0)) * 1_000_000)
            return micro_cost - 1
        return 0

    @staticmethod
    def _key_for_quota(
        user_id: int,
        department_id: int | None,
        logical_model_id: int,
        quota: Quota,
        *,
        legacy_key_format: bool = False,
    ) -> str:
        model_id = quota.logical_model_id if quota.logical_model_id is not None else 0
        period_key = QuotaEnforcer._period_key(quota.period)
        if legacy_key_format:
            return f"quota:{user_id}:{model_id}:{period_key}:{quota.metric}"
        if quota.scope == QuotaScope.user.value:
            return f"quota:u:{user_id}:{model_id}:{period_key}:{quota.metric}"
        if quota.scope == QuotaScope.department.value:
            dept_id = quota.scope_id if quota.scope_id is not None else department_id
            return f"quota:d:{dept_id}:{model_id}:{period_key}:{quota.metric}"
        return f"quota:g:{model_id}:{period_key}:{quota.metric}"

    @staticmethod
    def _key(user_id: int, logical_model_id: int, quota: Quota) -> str:
        return QuotaEnforcer._key_for_quota(
            user_id,
            None,
            logical_model_id,
            quota,
            legacy_key_format=True,
        )

    async def _run_check(
        self, redis: AsyncRedis, keys: Sequence[str], argv: Sequence[str | int]
    ) -> list[int]:
        if hasattr(redis, "register_script"):
            return await cast(
                "Awaitable[list[int]]",
                self._script(redis)(keys=keys, args=argv),
            )
        return await self._run_check_fallback(redis, keys, argv)

    async def _run_check_fallback(
        self, redis: AsyncRedis, keys: Sequence[str], argv: Sequence[str | int]
    ) -> list[int]:
        incremented: list[str] = []
        quota_count = int(argv[0])
        for index in range(quota_count):
            base = 1 + index * 3
            ttl = int(argv[base])
            limit = int(argv[base + 1])
            enforce = int(argv[base + 2])
            count = int(await redis.incr(keys[index]))
            if count == 1 and ttl > 0:
                await redis.expire(keys[index], ttl)
            incremented.append(keys[index])
            if enforce == 1 and count > limit:
                for key in incremented:
                    await redis.decr(key)
                return [1, index, count]
        return [0, -1, -1]

    async def _count(self, redis: AsyncRedis, key: str) -> int:
        if hasattr(redis, "get"):
            return int(await redis.get(key) or 0)
        data = getattr(redis, "_data", {})
        return int(data.get(key, 0))

    def _script(self, redis: AsyncRedis) -> Any:
        if self._check_script is None:
            self._check_script = redis.register_script(_QUOTA_CHECK_LUA)
        return self._check_script

    @staticmethod
    def _redis_limit(quota: Quota) -> Decimal:
        if quota.metric == QuotaMetric.cost.value:
            return quota.limit_value * Decimal(1_000_000)
        return quota.limit_value

    @staticmethod
    def _points_from_redis(metric: str, raw: Decimal) -> Decimal:
        """Convert a raw Redis counter back to user-facing units.

        Cost is stored in micro-units (×1_000_000) for integer precision; every
        value surfaced to a caller (429 ``current``, soft-limit warnings) must be
        divided back to cost points so it is comparable with ``limit_value``.
        Tokens/requests are stored 1:1 and pass through unchanged.
        """
        if metric == QuotaMetric.cost.value:
            return raw / Decimal(1_000_000)
        return raw

    @staticmethod
    def _period_key(period: str) -> str:
        now = datetime.now(tz=UTC)
        if period == QuotaPeriod.daily.value:
            return now.strftime("%Y-%m-%d")
        if period == QuotaPeriod.monthly.value:
            return now.strftime("%Y-%m")
        return QuotaPeriod.total.value

    @staticmethod
    def _ttl_seconds(period: str) -> int | None:
        now = datetime.now(tz=UTC)
        if period == QuotaPeriod.daily.value:
            tomorrow = (now + timedelta(days=1)).date()
            end = datetime(tomorrow.year, tomorrow.month, tomorrow.day, tzinfo=UTC)
            return max(1, int((end - now).total_seconds()))
        if period == QuotaPeriod.monthly.value:
            year = now.year + (1 if now.month == 12 else 0)
            month = 1 if now.month == 12 else now.month + 1
            end = datetime(year, month, 1, tzinfo=UTC)
            return max(1, int((end - now).total_seconds()))
        return None


def quota_increment_amount(metric: str) -> int:
    if metric == QuotaMetric.requests.value:
        return 1
    return 1
