from __future__ import annotations

from collections.abc import Awaitable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import cast

from src.core.redis import get_redis
from src.db.models.quota import Quota
from src.enums import QuotaMetric, QuotaPeriod


@dataclass(frozen=True, slots=True)
class QuotaWarning:
    quota_id: int
    metric: str
    period: str
    current: Decimal
    limit: Decimal


@dataclass(frozen=True, slots=True)
class QuotaCheckResult:
    passed: bool
    warnings: list[QuotaWarning] = field(default_factory=list)


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

    async def check_and_increment(
        self, user_id: int, logical_model_id: int, quotas: Sequence[Quota]
    ) -> QuotaCheckResult:
        redis = get_redis()
        warnings: list[QuotaWarning] = []
        for quota in quotas:
            key = self._key(user_id, logical_model_id, quota)
            count = await cast("Awaitable[int]", redis.incr(key))
            if count == 1:
                ttl = self._ttl_seconds(quota.period)
                if ttl is not None:
                    await redis.expire(key, ttl)
            current = Decimal(count)
            if current > quota.limit_value:
                if quota.enforce:
                    raise QuotaLimitExceeded(QuotaExceeded(quota=quota, current=current))
                warnings.append(
                    QuotaWarning(
                        quota_id=quota.id,
                        metric=quota.metric,
                        period=quota.period,
                        current=current,
                        limit=quota.limit_value,
                    )
                )
        return QuotaCheckResult(passed=True, warnings=warnings)

    async def compensate(
        self, user_id: int, logical_model_id: int, quotas: Sequence[Quota]
    ) -> None:
        redis = get_redis()
        for quota in quotas:
            await redis.decr(self._key(user_id, logical_model_id, quota))

    @staticmethod
    def _key(user_id: int, logical_model_id: int, quota: Quota) -> str:
        model_id = quota.logical_model_id or logical_model_id
        period_key = QuotaEnforcer._period_key(quota.period)
        return f"quota:{user_id}:{model_id}:{period_key}:{quota.metric}"

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
