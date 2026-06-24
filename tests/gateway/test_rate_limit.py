"""Unit tests for src/gateway/rate_limit.py — Lua RPM/TPM/concurrent scripts."""

from __future__ import annotations

import pytest

from src.gateway.rate_limit import (
    check_rate_limits,
    release_concurrent,
    settle_tpm,
)
from tests._async_redis_double import AsyncRedisDouble


def _make_rule(
    *,
    rule_id: int = 1,
    subject_type: str = "user",
    subject_id: int = 10,
    logical_model_id: int = 1,
    rpm_limit: int | None = None,
    tpm_limit: int | None = None,
    max_concurrent: int | None = None,
) -> dict:
    return {
        "id": rule_id,
        "subject_type": subject_type,
        "subject_id": subject_id,
        "logical_model_id": logical_model_id,
        "rpm_limit": rpm_limit,
        "tpm_limit": tpm_limit,
        "max_concurrent": max_concurrent,
    }


# ---------------------------------------------------------------------------
# RPM
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rpm_allowed_within_limit(fake_redis: AsyncRedisDouble) -> None:
    rule = _make_rule(rpm_limit=5)
    result = await check_rate_limits(request_id="req-1", member="m-1", rules=[rule])
    assert result.allowed is True
    assert result.rpm_remaining is not None
    assert result.rpm_remaining >= 0


@pytest.mark.asyncio
async def test_rpm_denied_at_limit(fake_redis: AsyncRedisDouble) -> None:
    rule = _make_rule(rpm_limit=2)
    # Fill up to limit (distinct members, as the gateway would generate)
    await check_rate_limits(request_id="req-1", member="m-1", rules=[rule])
    await check_rate_limits(request_id="req-2", member="m-2", rules=[rule])
    # Third should be denied
    result = await check_rate_limits(request_id="req-3", member="m-3", rules=[rule])
    assert result.allowed is False
    assert result.denied_reason == "rpm_exceeded"
    assert result.retry_after_seconds == 60


@pytest.mark.asyncio
async def test_rpm_repeated_member_does_not_bypass(fake_redis: AsyncRedisDouble) -> None:
    """Regression for bug B: a fixed member (e.g. a replayed client x-request-id)
    must NOT collapse the sliding-window count. The gateway supplies a fresh
    server-side member per request, so distinct requests always consume distinct
    slots regardless of any client-controlled id.
    """
    rule = _make_rule(rpm_limit=2)
    # Three real requests, each with its own server-side member, while a hostile
    # client replays the SAME request_id every time.
    await check_rate_limits(request_id="same", member="m-1", rules=[rule])
    await check_rate_limits(request_id="same", member="m-2", rules=[rule])
    result = await check_rate_limits(request_id="same", member="m-3", rules=[rule])
    # The window correctly counts 3 distinct members → 3rd is denied. Under the
    # old request_id-as-member logic, ZADD would overwrite a single member and
    # ZCARD would stay at 1, letting this through.
    assert result.allowed is False
    assert result.denied_reason == "rpm_exceeded"


# ---------------------------------------------------------------------------
# TPM
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tpm_allowed_within_limit(fake_redis: AsyncRedisDouble) -> None:
    rule = _make_rule(tpm_limit=1000)
    result = await check_rate_limits(
        request_id="req-1", member="m-1", rules=[rule], estimated_tokens=100
    )
    assert result.allowed is True
    assert result.tpm_remaining is not None


@pytest.mark.asyncio
async def test_tpm_denied_when_exhausted(fake_redis: AsyncRedisDouble) -> None:
    rule = _make_rule(tpm_limit=100)
    # First request takes all tokens
    result1 = await check_rate_limits(
        request_id="req-1", member="m-1", rules=[rule], estimated_tokens=100
    )
    assert result1.allowed is True
    # Second request should be denied (no tokens left)
    result2 = await check_rate_limits(
        request_id="req-2", member="m-2", rules=[rule], estimated_tokens=50
    )
    assert result2.allowed is False
    assert result2.denied_reason == "tpm_exceeded"


@pytest.mark.asyncio
async def test_tpm_settle_refunds_unused(fake_redis: AsyncRedisDouble) -> None:
    rule = _make_rule(tpm_limit=200)
    # Reserve 150 tokens upfront
    await check_rate_limits(
        request_id="req-1", member="m-1", rules=[rule], estimated_tokens=150
    )
    # Actual usage was only 50 → refund the 100-token surplus (positive delta)
    await settle_tpm("req-1", [rule], delta_tokens=100)
    # Now should have 150 tokens available — allow 140
    result = await check_rate_limits(
        request_id="req-2", member="m-2", rules=[rule], estimated_tokens=140
    )
    assert result.allowed is True


@pytest.mark.asyncio
async def test_tpm_settle_deducts_overage(fake_redis: AsyncRedisDouble) -> None:
    """A negative delta (actual > estimate) deducts the overage so TPM limits on
    real tokens, not request count. Regression for the refund-only bug."""
    rule = _make_rule(tpm_limit=300)
    # Reserve the flat 100-token estimate
    await check_rate_limits(
        request_id="req-1", member="m-1", rules=[rule], estimated_tokens=100
    )
    # Actual usage was 250 → must deduct the extra 150 (negative delta).
    # Bucket: 300 - 100 (reserve) - 150 (settle) = 50 remaining.
    await settle_tpm("req-1", [rule], delta_tokens=100 - 250)
    # Only 50 tokens left, so a 100-token request must be denied. Under the old
    # refund-only logic the bucket would still hold 200 and this would pass.
    result = await check_rate_limits(
        request_id="req-2", member="m-2", rules=[rule], estimated_tokens=100
    )
    assert result.allowed is False
    assert result.denied_reason == "tpm_exceeded"


@pytest.mark.asyncio
async def test_tpm_settle_zero_delta_noop(fake_redis: AsyncRedisDouble) -> None:
    rule = _make_rule(tpm_limit=200)
    await check_rate_limits(
        request_id="req-1", member="m-1", rules=[rule], estimated_tokens=100
    )
    await settle_tpm("req-1", [rule], delta_tokens=0)
    # Bucket unchanged at 100 remaining; a 100-token request still fits exactly.
    result = await check_rate_limits(
        request_id="req-2", member="m-2", rules=[rule], estimated_tokens=100
    )
    assert result.allowed is True


# ---------------------------------------------------------------------------
# Concurrent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_allowed(fake_redis: AsyncRedisDouble) -> None:
    rule = _make_rule(max_concurrent=3)
    result = await check_rate_limits(
        request_id="req-1", member="m-1", rules=[rule], is_stream=True
    )
    assert result.allowed is True
    assert result.concurrent_remaining is not None


@pytest.mark.asyncio
async def test_concurrent_denied_at_limit(fake_redis: AsyncRedisDouble) -> None:
    rule = _make_rule(max_concurrent=2)
    await check_rate_limits(request_id="req-1", member="m-1", rules=[rule], is_stream=True)
    await check_rate_limits(request_id="req-2", member="m-2", rules=[rule], is_stream=True)
    result = await check_rate_limits(
        request_id="req-3", member="m-3", rules=[rule], is_stream=True
    )
    assert result.allowed is False
    assert result.denied_reason == "concurrent_exceeded"


@pytest.mark.asyncio
async def test_concurrent_release(fake_redis: AsyncRedisDouble) -> None:
    rule = _make_rule(max_concurrent=2)
    await check_rate_limits(request_id="req-1", member="m-1", rules=[rule], is_stream=True)
    await check_rate_limits(request_id="req-2", member="m-2", rules=[rule], is_stream=True)
    # Release one slot by its server-side member
    await release_concurrent("m-1", [rule])
    # Now should allow
    result = await check_rate_limits(
        request_id="req-3", member="m-3", rules=[rule], is_stream=True
    )
    assert result.allowed is True


@pytest.mark.asyncio
async def test_concurrent_repeated_member_does_not_bypass(
    fake_redis: AsyncRedisDouble,
) -> None:
    """Regression for bug B: a replayed client id must not defeat the concurrent
    semaphore. Distinct server-side members fill distinct slots."""
    rule = _make_rule(max_concurrent=2)
    await check_rate_limits(request_id="same", member="m-1", rules=[rule], is_stream=True)
    await check_rate_limits(request_id="same", member="m-2", rules=[rule], is_stream=True)
    result = await check_rate_limits(
        request_id="same", member="m-3", rules=[rule], is_stream=True
    )
    assert result.allowed is False
    assert result.denied_reason == "concurrent_exceeded"


@pytest.mark.asyncio
async def test_concurrent_not_checked_for_non_stream(fake_redis: AsyncRedisDouble) -> None:
    rule = _make_rule(max_concurrent=1)
    # Non-streaming doesn't trigger concurrent check
    result = await check_rate_limits(
        request_id="req-1", member="m-1", rules=[rule], is_stream=False
    )
    assert result.allowed is True
    assert result.concurrent_remaining is None


# ---------------------------------------------------------------------------
# Multiple rules
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_multiple_rules_first_blocks(fake_redis: AsyncRedisDouble) -> None:
    rule1 = _make_rule(rule_id=1, rpm_limit=1)
    rule2 = _make_rule(rule_id=2, rpm_limit=100)
    # First request fills rule1
    await check_rate_limits(request_id="req-1", member="m-1", rules=[rule1, rule2])
    # Second blocked by rule1 even though rule2 has capacity
    result = await check_rate_limits(request_id="req-2", member="m-2", rules=[rule1, rule2])
    assert result.allowed is False


# ---------------------------------------------------------------------------
# Fail-open
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_rules_allowed(fake_redis: AsyncRedisDouble) -> None:
    result = await check_rate_limits(request_id="req-1", member="m-1", rules=[])
    assert result.allowed is True
