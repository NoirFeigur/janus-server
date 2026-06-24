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

    async def incrby(self, key: str, amount: int) -> int:
        self._data[key] = self._data.get(key, 0) + amount
        return self._data[key]

    async def get(self, key: str) -> int:
        return self._data.get(key, 0)

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
    period = QuotaEnforcer._period_key("daily")
    assert redis._data[f"quota:100:10:{period}:requests"] == 1


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


# ---------------------------------------------------------------------------
# M1: reservation-based settlement (hot-reload + period-rollover safe)
# ---------------------------------------------------------------------------


def _token_quota(*, limit: str) -> Quota:
    return Quota(
        id=7,
        scope=QuotaScope.user.value,
        scope_id=100,
        logical_model_id=10,
        period=QuotaPeriod.monthly.value,
        metric=QuotaMetric.tokens.value,
        limit_value=Decimal(limit),
        enforce=True,
    )


async def test_check_returns_reservations_with_resolved_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """check_and_increment exposes the resolved Redis key for each reserved quota."""
    redis = FakeRedis()
    monkeypatch.setattr("src.gateway.quota.get_redis", lambda: redis)
    quota = _token_quota(limit="100000")

    result = await QuotaEnforcer().check_and_increment(100, None, 10, [quota])

    assert len(result.reservations) == 1
    res = result.reservations[0]
    assert res.quota_id == 7
    assert res.metric == QuotaMetric.tokens.value
    # The reserved key must be exactly the one incremented at check time.
    assert redis._data[res.key] == 1


async def test_settle_reservations_targets_exact_reserved_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Settlement adjusts the exact key reserved at check (immune to S1 != S2)."""
    redis = FakeRedis()
    monkeypatch.setattr("src.gateway.quota.get_redis", lambda: redis)
    enforcer = QuotaEnforcer()
    quota = _token_quota(limit="100000")

    result = await enforcer.check_and_increment(100, None, 10, [quota])
    reserved_key = result.reservations[0].key
    assert redis._data[reserved_key] == 1  # +1 pre-flight reservation

    # Settle with 50 actual tokens: adjustment = actual - 1 = 49.
    await enforcer.settle_reservations(result.reservations, actual_tokens=50, actual_cost=None)

    # Final counter == actual tokens (reservation +1 replaced by actual 50).
    assert redis._data[reserved_key] == 50


async def test_settle_reservations_immune_to_quota_config_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even if the live quota set changes after check, settle targets the reserved key.

    Simulates a hot-reload where a fresh get_active_quotas would return a
    DIFFERENT quota (different id/scope -> different key).  Reservation-based
    settle ignores that and balances the originally reserved counter.
    """
    redis = FakeRedis()
    monkeypatch.setattr("src.gateway.quota.get_redis", lambda: redis)
    enforcer = QuotaEnforcer()
    quota = _token_quota(limit="100000")

    result = await enforcer.check_and_increment(100, None, 10, [quota])
    reserved_key = result.reservations[0].key

    # A divergent quota that would be returned by a re-query (different id).
    divergent_key = "quota:u:100:10:1999-12:tokens"
    redis._data[divergent_key] = 0

    await enforcer.settle_reservations(result.reservations, actual_tokens=30, actual_cost=None)

    # The reserved key balanced to actuals; the divergent key untouched.
    assert redis._data[reserved_key] == 30
    assert redis._data[divergent_key] == 0


async def test_compensate_reservations_gives_back_reservation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Error path: compensate decrements the exact reserved key back to zero."""
    redis = FakeRedis()
    monkeypatch.setattr("src.gateway.quota.get_redis", lambda: redis)
    enforcer = QuotaEnforcer()
    quota = _token_quota(limit="100000")

    result = await enforcer.check_and_increment(100, None, 10, [quota])
    reserved_key = result.reservations[0].key
    assert redis._data[reserved_key] == 1

    await enforcer.compensate_reservations(result.reservations)

    assert redis._data[reserved_key] == 0
