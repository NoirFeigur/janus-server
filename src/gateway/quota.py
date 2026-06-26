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

# Grace window added to every period-end quota key TTL (Oracle #6). A reservation
# key must outlive the LONGEST possible request: settlement runs at finalize via
# INCRBY on the exact key reserved at check time, and INCRBY on an *expired* key
# silently recreates it with NO TTL — an immortal orphan that poisons the next
# period's count. Covering the max stream duration (1800s, mirrored in
# router._STREAM_MAX_DURATION_SECONDS) plus settle/finalize latency slack keeps
# the key alive (TTL intact, since INCRBY preserves an existing TTL) through
# settlement, after which it expires naturally a few minutes into the new period.
_QUOTA_TTL_GRACE_SECONDS = 1800 + 300

_QUOTA_CHECK_LUA = """
-- KEYS: quota redis keys (one per quota rule)
-- ARGV: [num_quotas, ttl1, limit1, enforce1, reserve1, ttl2, limit2, enforce2, reserve2, ...]
-- Returns: {0=pass/1=fail, failed_index (0-based, -1 if pass), current_count}
local n = tonumber(ARGV[1])
local incremented = {}
for i = 1, n do
    local base = 1 + (i-1)*4
    local ttl = tonumber(ARGV[base+1])
    local limit = tonumber(ARGV[base+2])
    local enforce = tonumber(ARGV[base+3])
    local reserve = tonumber(ARGV[base+4])
    local count = redis.call('INCRBY', KEYS[i], reserve)
    if count == reserve and ttl > 0 then
        redis.call('EXPIRE', KEYS[i], ttl)
    end
    table.insert(incremented, {i, reserve})
    if enforce == 1 and count > limit then
        for _, pair in ipairs(incremented) do
            redis.call('DECRBY', KEYS[pair[1]], pair[2])
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

    ``reserved`` is the amount actually incremented at check time (in Redis
    units: tokens/requests 1:1, cost in micro-units). Settlement adjusts by
    ``actual - reserved`` and compensation gives back exactly ``reserved`` — so
    a request never overshoots by more than its own estimate error, not by its
    full real usage (Oracle #2).
    """

    key: str
    quota_id: int
    metric: str
    scope: str
    enforce: bool
    limit_value: Decimal
    reserved: int = 1


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
    """Redis-backed quota checking and compensation primitives.

    **Quota reserves the request's ESTIMATED cost up front, then reconciles.**
    Pre-flight ``check_and_increment`` increments each token/cost counter by the
    request's estimate (``estimated_tokens`` / ``estimated_cost``), not a flat
    ``+1`` — so N concurrent in-flight requests reserve N estimates, and the
    aggregate cannot silently blow past the limit while every request sits at
    ``+1`` (Oracle #2). ``requests`` counters still reserve ``+1`` (one request
    is one request). After the upstream call completes, ``settle_reservations``
    adjusts each counter by ``actual - reserved`` so the final count reflects
    real usage; ``compensate_reservations`` gives back exactly ``reserved`` on
    the error path. A single request can still overshoot by its own estimate
    *error* (actual minus estimate), but never by its full real usage, and the
    overshoot no longer scales with concurrency. ``settle_reservations`` logs
    (does not reject) a post-settlement breach; the *next* request is rejected.
    """

    def __init__(self) -> None:
        self._check_script: Any | None = None

    async def check_and_increment(
        self,
        user_id: int,
        department_id: int | None,
        logical_model_id: int,
        quotas: Sequence[Quota],
        estimated_tokens: int = 0,
        estimated_cost: Decimal | None = None,
    ) -> QuotaCheckResult:
        if not quotas:
            return QuotaCheckResult(passed=True)
        redis = get_redis()
        keys = [
            self._key_for_quota(
                user_id,
                department_id,
                logical_model_id,
                quota,
            )
            for quota in quotas
        ]
        reserves = [
            self._reserve_for_metric(quota.metric, estimated_tokens, estimated_cost)
            for quota in quotas
        ]
        argv: list[str | int] = [len(quotas)]
        for quota, reserve in zip(quotas, reserves, strict=True):
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
                    reserve,
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
                reserved=reserve,
            )
            for key, quota, reserve in zip(keys, quotas, reserves, strict=True)
        ]
        return QuotaCheckResult(passed=True, warnings=warnings, reservations=reservations)

    async def settle_reservations(
        self,
        reservations: Sequence[QuotaReservation],
        actual_tokens: int,
        actual_cost: Decimal | None,
    ) -> None:
        """Settle against the exact keys reserved at check time (hot-reload safe).

        Targets the resolved Redis keys captured by ``check_and_increment`` so
        reservation and settlement always balance on the same counter, even if
        the quota config hot-reloaded or the period rolled over mid-request. Each
        counter is adjusted by ``actual - reserved`` so the final count reflects
        real usage regardless of the pre-flight estimate (Oracle #2).
        """
        redis = get_redis()
        for res in reservations:
            adjustment = self._settlement_adjustment_for_metric(
                res.metric, res.reserved, actual_tokens, actual_cost
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
        """Give back the full pre-flight reservation on the exact keys reserved."""
        redis = get_redis()
        for res in reservations:
            if res.reserved:
                await redis.incrby(res.key, -res.reserved)

    @staticmethod
    def _reserve_for_metric(
        metric: str, estimated_tokens: int, estimated_cost: Decimal | None
    ) -> int:
        """Amount to reserve at check time, in Redis units (Oracle #2).

        ``requests`` reserves ``+1`` (one request is one request). ``tokens``
        reserves the token estimate; ``cost`` reserves the estimated cost in
        micro-units. A zero/absent estimate floors at ``1`` so the counter still
        advances and freshness/TTL logic holds.
        """
        if metric == QuotaMetric.requests.value:
            return 1
        if metric == QuotaMetric.tokens.value:
            return max(1, estimated_tokens)
        if metric == QuotaMetric.cost.value:
            micro = int((estimated_cost or Decimal(0)) * 1_000_000)
            return max(1, micro)
        return 1

    @staticmethod
    def _settlement_adjustment_for_metric(
        metric: str, reserved: int, actual_tokens: int, actual_cost: Decimal | None
    ) -> int:
        if metric == QuotaMetric.requests.value:
            return 0
        if metric == QuotaMetric.tokens.value:
            return actual_tokens - reserved
        if metric == QuotaMetric.cost.value:
            # Cost stored in micro-units (×1_000_000); reservation was `reserved`.
            micro_cost = int((actual_cost or Decimal(0)) * 1_000_000)
            return micro_cost - reserved
        return 0

    @staticmethod
    def _redis_limit_for(metric: str, limit_value: Decimal) -> Decimal:
        if metric == QuotaMetric.cost.value:
            return limit_value * Decimal(1_000_000)
        return limit_value

    @staticmethod
    def _key_for_quota(
        user_id: int,
        department_id: int | None,
        logical_model_id: int,
        quota: Quota,
    ) -> str:
        model_id = quota.logical_model_id if quota.logical_model_id is not None else 0
        period_key = QuotaEnforcer._period_key(quota.period)
        if quota.scope == QuotaScope.user.value:
            return f"quota:u:{user_id}:{model_id}:{period_key}:{quota.metric}"
        if quota.scope == QuotaScope.department.value:
            dept_id = quota.scope_id if quota.scope_id is not None else department_id
            return f"quota:d:{dept_id}:{model_id}:{period_key}:{quota.metric}"
        return f"quota:g:{model_id}:{period_key}:{quota.metric}"

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
        incremented: list[tuple[str, int]] = []
        quota_count = int(argv[0])
        for index in range(quota_count):
            base = 1 + index * 4
            ttl = int(argv[base])
            limit = int(argv[base + 1])
            enforce = int(argv[base + 2])
            reserve = int(argv[base + 3])
            count = int(await redis.incrby(keys[index], reserve))
            if count == reserve and ttl > 0:
                await redis.expire(keys[index], ttl)
            incremented.append((keys[index], reserve))
            if enforce == 1 and count > limit:
                for key, amount in incremented:
                    await redis.incrby(key, -amount)
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
            return max(1, int((end - now).total_seconds())) + _QUOTA_TTL_GRACE_SECONDS
        if period == QuotaPeriod.monthly.value:
            year = now.year + (1 if now.month == 12 else 0)
            month = 1 if now.month == 12 else now.month + 1
            end = datetime(year, month, 1, tzinfo=UTC)
            return max(1, int((end - now).total_seconds())) + _QUOTA_TTL_GRACE_SECONDS
        return None


def quota_increment_amount(metric: str) -> int:
    if metric == QuotaMetric.requests.value:
        return 1
    return 1
