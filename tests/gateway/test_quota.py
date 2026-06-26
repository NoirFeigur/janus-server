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


def _cost_quota(*, limit: str, enforce: bool = True) -> Quota:
    return Quota(
        id=1,
        scope=QuotaScope.user.value,
        scope_id=100,
        logical_model_id=10,
        period=QuotaPeriod.daily.value,
        metric=QuotaMetric.cost.value,
        limit_value=Decimal(limit),
        enforce=enforce,
    )


async def test_check_and_increment_passes_under_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = FakeRedis()
    monkeypatch.setattr("src.gateway.quota.get_redis", lambda: redis)

    result = await QuotaEnforcer().check_and_increment(100, None, 10, [_quota(limit="2")])

    assert result.passed is True
    assert result.warnings == []
    period = QuotaEnforcer._period_key("daily")
    assert redis._data[f"quota:u:100:10:{period}:requests"] == 1


async def test_check_and_increment_raises_when_hard_quota_exceeded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = FakeRedis()
    monkeypatch.setattr("src.gateway.quota.get_redis", lambda: redis)
    quota = _quota(limit="1")
    await QuotaEnforcer().check_and_increment(100, None, 10, [quota])

    with pytest.raises(QuotaLimitExceeded) as exc_info:
        await QuotaEnforcer().check_and_increment(100, None, 10, [quota])

    assert exc_info.value.exceeded.quota is quota
    assert exc_info.value.exceeded.current == Decimal("2")


async def test_check_and_increment_warns_when_soft_quota_exceeded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = FakeRedis()
    monkeypatch.setattr("src.gateway.quota.get_redis", lambda: redis)
    quota = _quota(limit="1", enforce=False)
    await QuotaEnforcer().check_and_increment(100, None, 10, [quota])

    result = await QuotaEnforcer().check_and_increment(100, None, 10, [quota])

    assert len(result.warnings) == 1
    assert result.warnings[0].current == Decimal("2")
    assert result.warnings[0].limit == Decimal("1")


async def test_cost_quota_exceeded_reports_current_in_points_not_micro_units(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F: a cost quota stores micro-units (×1e6) in Redis, but the 429 `current`
    must be reported in cost POINTS so it is comparable with `limit_value`.

    Limit is 10 points (10_000_000 micro-units). We pre-seed the counter to the
    limit, so the next reservation (+1 micro-unit) trips the hard quota. The raw
    Redis value is 10_000_001 micro-units, which must surface as 10.000001 points
    — NOT the raw micro-unit integer."""
    redis = FakeRedis()
    monkeypatch.setattr("src.gateway.quota.get_redis", lambda: redis)
    quota = _cost_quota(limit="10")
    enforcer = QuotaEnforcer()
    # Seed the counter right at the limit (10 points = 10_000_000 micro-units).
    redis._data[enforcer._key_for_quota(100, None, 10, quota)] = 10_000_000

    with pytest.raises(QuotaLimitExceeded) as exc_info:
        await enforcer.check_and_increment(100, None, 10, [quota])

    # +1 micro-unit → 10_000_001 raw, reported as 10.000001 points (not 10000001).
    assert exc_info.value.exceeded.current == Decimal("10.000001")


async def test_cost_quota_soft_warning_current_in_points(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F: the soft-limit warning `current` is also in points (micro-units / 1e6)."""
    redis = FakeRedis()
    monkeypatch.setattr("src.gateway.quota.get_redis", lambda: redis)
    quota = _cost_quota(limit="5", enforce=False)
    enforcer = QuotaEnforcer()
    # Seed above the 5-point limit: 6_000_000 micro-units = 6 points.
    redis._data[enforcer._key_for_quota(100, None, 10, quota)] = 6_000_000

    result = await enforcer.check_and_increment(100, None, 10, [quota])

    assert len(result.warnings) == 1
    # 6_000_001 micro-units → 6.000001 points; limit stays 5 points.
    assert result.warnings[0].current == Decimal("6.000001")
    assert result.warnings[0].limit == Decimal("5")


async def test_token_reservation_bounds_concurrent_overshoot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Oracle #2: token quota must reserve the ESTIMATED amount up front, not +1.

    With a 1000-token limit and a 400-token estimate, only two requests fit
    (400 + 400 = 800 ≤ 1000); the third (would be 1200) must be rejected at
    pre-flight. The old +1 reservation let unbounded concurrent requests all
    pass pre-flight and only overshoot at settle time.
    """
    redis = FakeRedis()
    monkeypatch.setattr("src.gateway.quota.get_redis", lambda: redis)
    enforcer = QuotaEnforcer()
    quota = _token_quota(limit="1000")

    r1 = await enforcer.check_and_increment(
        100, None, 10, [quota], estimated_tokens=400
    )
    r2 = await enforcer.check_and_increment(
        100, None, 10, [quota], estimated_tokens=400
    )
    assert r1.passed and r2.passed
    assert r1.reservations[0].reserved == 400

    with pytest.raises(QuotaLimitExceeded):
        await enforcer.check_and_increment(
            100, None, 10, [quota], estimated_tokens=400
        )


async def test_cost_reservation_bounds_concurrent_overshoot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Oracle #2: cost quota reserves the estimated cost (micro-units), not +1."""
    redis = FakeRedis()
    monkeypatch.setattr("src.gateway.quota.get_redis", lambda: redis)
    enforcer = QuotaEnforcer()
    quota = _cost_quota(limit="10")  # 10 points = 10_000_000 micro-units

    # Each request estimated at 4 points → two fit (8), third (12) rejected.
    r1 = await enforcer.check_and_increment(
        100, None, 10, [quota], estimated_cost=Decimal("4")
    )
    assert r1.passed
    assert r1.reservations[0].reserved == 4_000_000
    await enforcer.check_and_increment(
        100, None, 10, [quota], estimated_cost=Decimal("4")
    )

    with pytest.raises(QuotaLimitExceeded):
        await enforcer.check_and_increment(
            100, None, 10, [quota], estimated_cost=Decimal("4")
        )


async def test_settle_reservations_balances_against_estimated_reserve(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Settle adjusts by (actual - reserved), not (actual - 1)."""
    redis = FakeRedis()
    monkeypatch.setattr("src.gateway.quota.get_redis", lambda: redis)
    enforcer = QuotaEnforcer()
    quota = _token_quota(limit="100000")

    result = await enforcer.check_and_increment(
        100, None, 10, [quota], estimated_tokens=400
    )
    reserved_key = result.reservations[0].key
    assert redis._data[reserved_key] == 400  # reserved estimate, not +1

    # Actual was 250: adjustment = 250 - 400 = -150 → counter settles to 250.
    await enforcer.settle_reservations(
        result.reservations, actual_tokens=250, actual_cost=None
    )
    assert redis._data[reserved_key] == 250


async def test_compensate_reservations_gives_back_full_estimate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Error path: compensate returns the full reserved estimate, not just +1."""
    redis = FakeRedis()
    monkeypatch.setattr("src.gateway.quota.get_redis", lambda: redis)
    enforcer = QuotaEnforcer()
    quota = _token_quota(limit="100000")

    result = await enforcer.check_and_increment(
        100, None, 10, [quota], estimated_tokens=400
    )
    reserved_key = result.reservations[0].key
    assert redis._data[reserved_key] == 400

    await enforcer.compensate_reservations(result.reservations)
    assert redis._data[reserved_key] == 0


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


async def test_ttl_outlives_longest_request_near_period_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Oracle #6: a reservation key TTL must outlive the longest possible
    request, not just the time to period-end.

    At 23:59:59 a daily key would otherwise get TTL ~1s. A 10-minute stream then
    settles via INCRBY on the (now expired) key, which Redis recreates with NO
    TTL — an immortal orphan that poisons the next period's count. The TTL must
    therefore include a grace window covering the max stream duration + settle
    latency so the key is still alive (with its TTL intact) when settle runs."""
    from datetime import UTC, datetime, timedelta

    class _FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz: object = None) -> datetime:  # type: ignore[override]
            # Last second of a day → naive time-to-midnight is ~1s.
            return datetime(2026, 6, 26, 23, 59, 59, tzinfo=UTC)

    monkeypatch.setattr("src.gateway.quota.datetime", _FrozenDatetime)

    daily_ttl = QuotaEnforcer._ttl_seconds(QuotaPeriod.daily.value)
    monthly_ttl = QuotaEnforcer._ttl_seconds(QuotaPeriod.monthly.value)
    assert daily_ttl is not None
    assert monthly_ttl is not None
    # Must cover the grace window (max stream duration + settle latency), so a
    # long request that started just before midnight still finds its key alive.
    from src.gateway.quota import _QUOTA_TTL_GRACE_SECONDS

    assert daily_ttl >= _QUOTA_TTL_GRACE_SECONDS
    assert monthly_ttl >= _QUOTA_TTL_GRACE_SECONDS
    # Sanity: still bounded — period remainder (~1s) + grace, not a year.
    assert daily_ttl <= _QUOTA_TTL_GRACE_SECONDS + 60
    _ = timedelta  # keep import used if pruned


async def test_ttl_total_period_has_no_expiry() -> None:
    """The 'total' period never expires — no grace window applies."""
    assert QuotaEnforcer._ttl_seconds(QuotaPeriod.total.value) is None


# ---------------------------------------------------------------------------
# Concurrency / double-spend / underflow invariants
# ---------------------------------------------------------------------------


async def test_multi_quota_later_exceed_rolls_back_earlier_increment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A later quota's hard breach must roll back EVERY earlier increment in the
    same check — no partial reservation leak (double-spend adjacent).

    ``check_and_increment`` increments each quota counter in order, and if any
    enforced counter trips its limit it DECRBYs every counter already
    incremented in this call (quota.py fallback L339-342 / the Lua mirror). If
    that rollback were incomplete, the first quota would keep a reservation for a
    request that was rejected and never ran — a permanent leak that throttles the
    subject and, on a cost/token counter, silently overcounts usage. This locks
    the all-or-nothing reservation contract across multiple quotas.
    """
    redis = FakeRedis()
    monkeypatch.setattr("src.gateway.quota.get_redis", lambda: redis)
    enforcer = QuotaEnforcer()

    # First quota (requests) has ample headroom; second (tokens) trips at +400.
    requests_quota = _quota(limit="100")  # requests metric, won't trip on +1
    token_quota = _token_quota(limit="1")  # tokens metric, +400 reserve >> 1
    requests_key = enforcer._key_for_quota(100, None, 10, requests_quota)
    token_key = enforcer._key_for_quota(100, None, 10, token_quota)

    with pytest.raises(QuotaLimitExceeded) as exc_info:
        await enforcer.check_and_increment(
            100, None, 10, [requests_quota, token_quota], estimated_tokens=400
        )

    # The breach is attributed to the token quota (the second rule).
    assert exc_info.value.exceeded.quota is token_quota
    # BOTH counters rolled back to their pre-call value — no leaked reservation.
    assert redis._data.get(requests_key, 0) == 0
    assert redis._data.get(token_key, 0) == 0


async def test_concurrent_reserve_then_settle_aggregates_without_double_spend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two in-flight requests on the SAME quota counter reserve, then settle to
    actuals — the final count equals the sum of real usage, never negative.

    Both requests hit the same resolved key (same user/model/period/metric), so
    this is the concurrent double-spend / underflow surface: each reserves its
    estimate up front (counter holds the aggregate in-flight reservation), then
    each settles by ``actual - reserved`` against that exact key. The end state
    must be exactly ``actual_1 + actual_2`` (no reservation double-counted, no
    over-refund driving the counter below zero).
    """
    redis = FakeRedis()
    monkeypatch.setattr("src.gateway.quota.get_redis", lambda: redis)
    enforcer = QuotaEnforcer()
    quota = _token_quota(limit="100000")

    # Both requests reserve 400 up front (interleaved in-flight).
    r1 = await enforcer.check_and_increment(
        100, None, 10, [quota], estimated_tokens=400
    )
    r2 = await enforcer.check_and_increment(
        100, None, 10, [quota], estimated_tokens=400
    )
    key = r1.reservations[0].key
    assert r2.reservations[0].key == key  # same counter — the contended case
    assert redis._data[key] == 800  # aggregate reservation, both in flight

    # Settle interleaved: r1 used 250, r2 used 600.
    await enforcer.settle_reservations(r1.reservations, actual_tokens=250, actual_cost=None)
    assert redis._data[key] >= 0  # never underflows mid-settlement
    await enforcer.settle_reservations(r2.reservations, actual_tokens=600, actual_cost=None)

    # Final counter is exactly the sum of actuals — no double-spend, no underflow.
    assert redis._data[key] == 850
