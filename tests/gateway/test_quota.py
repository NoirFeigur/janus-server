from __future__ import annotations

from decimal import Decimal

import pytest

from src.db.models.quota import Quota
from src.enums import QuotaMetric, QuotaPeriod, QuotaScope
from src.gateway.quota import QuotaEnforcer, QuotaLimitExceeded

pytestmark = pytest.mark.asyncio


class FakeRedis:
    def __init__(self) -> None:
        self._data: dict[str, int] = {}
        self.expirations: dict[str, int] = {}

    async def incr(self, key: str) -> int:
        self._data[key] = self._data.get(key, 0) + 1
        return self._data[key]

    async def decr(self, key: str) -> int:
        self._data[key] = self._data.get(key, 0) - 1
        return self._data[key]

    async def expire(self, key: str, ttl: int) -> bool:
        self.expirations[key] = ttl
        return True


def _quota(*, limit: str, enforce: bool = True) -> Quota:
    return Quota(
        id=1,
        scope=QuotaScope.user.value,
        scope_id=100,
        logical_model_id=10,
        period=QuotaPeriod.daily.value,
        metric=QuotaMetric.requests.value,
        limit_value=Decimal(limit),
        enforce=enforce,
    )


async def test_check_and_increment_passes_under_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = FakeRedis()
    monkeypatch.setattr("src.gateway.quota.get_redis", lambda: redis)

    result = await QuotaEnforcer().check_and_increment(100, 10, [_quota(limit="2")])

    assert result.passed is True
    assert result.warnings == []
    assert redis._data["quota:100:10:%s:requests" % QuotaEnforcer._period_key("daily")] == 1


async def test_check_and_increment_raises_when_hard_quota_exceeded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = FakeRedis()
    monkeypatch.setattr("src.gateway.quota.get_redis", lambda: redis)
    quota = _quota(limit="1")
    await QuotaEnforcer().check_and_increment(100, 10, [quota])

    with pytest.raises(QuotaLimitExceeded) as exc_info:
        await QuotaEnforcer().check_and_increment(100, 10, [quota])

    assert exc_info.value.exceeded.quota is quota
    assert exc_info.value.exceeded.current == Decimal("2")


async def test_check_and_increment_warns_when_soft_quota_exceeded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = FakeRedis()
    monkeypatch.setattr("src.gateway.quota.get_redis", lambda: redis)
    quota = _quota(limit="1", enforce=False)
    await QuotaEnforcer().check_and_increment(100, 10, [quota])

    result = await QuotaEnforcer().check_and_increment(100, 10, [quota])

    assert len(result.warnings) == 1
    assert result.warnings[0].current == Decimal("2")
    assert result.warnings[0].limit == Decimal("1")


async def test_compensate_decrements_counter(monkeypatch: pytest.MonkeyPatch) -> None:
    redis = FakeRedis()
    monkeypatch.setattr("src.gateway.quota.get_redis", lambda: redis)
    quota = _quota(limit="2")
    enforcer = QuotaEnforcer()
    await enforcer.check_and_increment(100, 10, [quota])

    await enforcer.compensate(100, 10, [quota])

    assert redis._data[enforcer._key(100, 10, quota)] == 0


async def test_period_key_daily_format() -> None:
    period_key = QuotaEnforcer._period_key(QuotaPeriod.daily.value)

    assert len(period_key) == 10
    assert period_key[4] == "-"
    assert period_key[7] == "-"


async def test_period_key_monthly_format() -> None:
    period_key = QuotaEnforcer._period_key(QuotaPeriod.monthly.value)

    assert len(period_key) == 7
    assert period_key[4] == "-"
