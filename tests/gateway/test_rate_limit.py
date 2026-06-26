"""Unit tests for src/gateway/rate_limit.py — Lua RPM/TPM/concurrent scripts."""

from __future__ import annotations

import time

import pytest
from redis.exceptions import RedisError

from src.gateway.rate_limit import (
    _MAX_COMPLETION_CEILING,
    ESTIMATED_TOKENS_PER_REQUEST,
    check_rate_limits,
    coerce_max_completion,
    estimate_request_tokens,
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
    tpm_burst_limit: int | None = None,
    max_concurrent: int | None = None,
) -> dict:
    return {
        "id": rule_id,
        "subject_type": subject_type,
        "subject_id": subject_id,
        "logical_model_id": logical_model_id,
        "rpm_limit": rpm_limit,
        "tpm_limit": tpm_limit,
        "tpm_burst_limit": tpm_burst_limit,
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
async def test_tpm_burst_limit_used_as_bucket_capacity(fake_redis: AsyncRedisDouble) -> None:
    rule = _make_rule(tpm_limit=100, tpm_burst_limit=250)
    first = await check_rate_limits(
        request_id="req-1", member="m-1", rules=[rule], estimated_tokens=200
    )
    assert first.allowed is True

    second = await check_rate_limits(
        request_id="req-2", member="m-2", rules=[rule], estimated_tokens=60
    )
    assert second.allowed is False
    assert second.denied_reason == "tpm_exceeded"


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


@pytest.mark.asyncio
async def test_tpm_settle_debt_ttl_outlives_refill_recovery(
    fake_redis: AsyncRedisDouble,
) -> None:
    """Oracle #4: when settle drives the bucket into deep debt, the key TTL must
    cover the time refill needs to clear that debt. A 1000 tok/min refill against
    a ~39k-token debt takes ~39 min to recover — far longer than the fixed
    ~31 min bucket TTL. Under the old fixed TTL the debt key expired before refill
    repaid it, so the next request saw a fresh full bucket and the heavy caller's
    overage was silently forgiven (TPM bypass)."""
    rule = _make_rule(tpm_limit=1000)
    key = "rl:tpm:1:user:10:m:1"
    # Reserve the flat estimate → bucket at 900.
    await check_rate_limits(
        request_id="req-1", member="m-1", rules=[rule], estimated_tokens=100
    )
    # Actual usage was ~40k tokens → delta = 100 - 40000. Bucket: 900 - 39900 =
    # -39000. Recovery at 1000/min ≈ 39 min, which exceeds the base TTL.
    await settle_tpm("req-1", [rule], delta_tokens=100 - 40000)
    ttl_seconds = await fake_redis.ttl(key)
    # Base TTL is ~1860s; a debt that needs ~39 min to refill demands a much
    # longer TTL. Assert it comfortably exceeds the base so the debt survives.
    assert ttl_seconds > 1860 + 2000


@pytest.mark.asyncio
async def test_tpm_settle_non_negative_keeps_base_ttl(
    fake_redis: AsyncRedisDouble,
) -> None:
    """A settle that leaves the bucket non-negative does not inflate the TTL —
    only debt extends it, so idle healthy buckets still expire on the base TTL."""
    rule = _make_rule(tpm_limit=1000)
    key = "rl:tpm:1:user:10:m:1"
    await check_rate_limits(
        request_id="req-1", member="m-1", rules=[rule], estimated_tokens=100
    )
    # Small overage keeps the bucket positive (900 - 50 = 850).
    await settle_tpm("req-1", [rule], delta_tokens=100 - 150)
    ttl_seconds = await fake_redis.ttl(key)
    # Stays at the base TTL (~1860s); allow a small ceiling for clock slack.
    assert ttl_seconds <= 1860 + 5


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


@pytest.mark.asyncio
async def test_later_rule_denial_rolls_back_prior_tpm_reservation(
    fake_redis: AsyncRedisDouble,
) -> None:
    rule1 = _make_rule(rule_id=1, tpm_limit=300)
    rule2 = _make_rule(rule_id=2, rpm_limit=1)
    await check_rate_limits(request_id="req-fill", member="m-fill", rules=[rule2])

    denied = await check_rate_limits(
        request_id="req-denied",
        member="m-denied",
        rules=[rule1, rule2],
        estimated_tokens=100,
    )
    assert denied.allowed is False

    allowed = await check_rate_limits(
        request_id="req-after",
        member="m-after",
        rules=[rule1],
        estimated_tokens=300,
    )
    assert allowed.allowed is True


@pytest.mark.asyncio
async def test_later_rule_denial_rolls_back_prior_concurrent_slot(
    fake_redis: AsyncRedisDouble,
) -> None:
    rule1 = _make_rule(rule_id=1, max_concurrent=1)
    rule2 = _make_rule(rule_id=2, rpm_limit=1)
    await check_rate_limits(request_id="req-fill", member="m-fill", rules=[rule2])

    denied = await check_rate_limits(
        request_id="req-denied",
        member="m-denied",
        rules=[rule1, rule2],
        is_stream=True,
    )
    assert denied.allowed is False

    allowed = await check_rate_limits(
        request_id="req-after",
        member="m-after",
        rules=[rule1],
        is_stream=True,
    )
    assert allowed.allowed is True


@pytest.mark.asyncio
async def test_later_gate_denial_rolls_back_acquired_rpm(
    fake_redis: AsyncRedisDouble,
) -> None:
    """Oracle #5: when RPM is acquired but a later gate on the SAME request
    rejects (TPM here), the rollback MUST release the RPM window entry too.

    The old ``_rollback`` only reverted TPM + concurrent, so the RPM slot leaked:
    a request that never ran still consumed RPM quota for the full sliding
    window, and repeated denials eventually locked the subject out of requests
    that were never admitted."""
    # rpm_limit=1 so a single leaked entry exhausts the window; tpm tiny so TPM
    # denies only AFTER RPM has already been acquired in the same call.
    rule = _make_rule(rule_id=1, rpm_limit=1, tpm_limit=50)
    denied = await check_rate_limits(
        request_id="req-denied", member="m-1", rules=[rule], estimated_tokens=100
    )
    assert denied.allowed is False
    assert denied.denied_reason == "tpm_exceeded"

    # RPM must have been rolled back: a fresh member (TPM skipped via
    # estimated_tokens=0) should still find the single RPM slot free.
    allowed = await check_rate_limits(
        request_id="req-after", member="m-2", rules=[rule], estimated_tokens=0
    )
    assert allowed.allowed is True


# ---------------------------------------------------------------------------
# Fail-open
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_rules_allowed(fake_redis: AsyncRedisDouble) -> None:
    result = await check_rate_limits(request_id="req-1", member="m-1", rules=[])
    assert result.allowed is True


# ---------------------------------------------------------------------------
# Upfront TPM estimate — cross-provider input shapes
# ---------------------------------------------------------------------------


def test_estimate_tokens_openai_chat_content() -> None:
    messages = [{"role": "user", "content": "x" * 8000}]
    # 8000 chars / 4 chars-per-token = 2000, well above the default floor.
    assert estimate_request_tokens(messages) == 2000


def test_estimate_tokens_anthropic_content_blocks() -> None:
    messages = [{"role": "user", "content": [{"type": "text", "text": "z" * 8000}]}]
    assert estimate_request_tokens(messages) == 2000


def test_estimate_tokens_gemini_parts_not_collapsed_to_floor() -> None:
    # Gemini carries text under contents[].parts[].text. A regression here made a
    # multi-thousand-char Gemini prompt estimate the default 100-token floor,
    # letting a large prompt bypass a tight TPM bucket on its first request.
    contents = [{"role": "user", "parts": [{"text": "g" * 8000}]}]
    assert estimate_request_tokens(contents) == 2000


def test_estimate_tokens_gemini_multi_parts() -> None:
    contents = [{"role": "user", "parts": [{"text": "a" * 4000}, {"text": "b" * 4000}]}]
    assert estimate_request_tokens(contents) == 2000


def test_estimate_tokens_empty_input_falls_back_to_floor() -> None:
    assert estimate_request_tokens([]) == ESTIMATED_TOKENS_PER_REQUEST
    assert estimate_request_tokens([{"role": "user", "parts": []}]) == ESTIMATED_TOKENS_PER_REQUEST


def test_estimate_tokens_includes_max_completion() -> None:
    """Oracle #3: TPM reservation must cover the declared output cap, not just
    the prompt. Otherwise a request with a small prompt and a huge max_tokens
    reserves only the prompt estimate and the generated output bypasses TPM."""
    messages = [{"role": "user", "content": "x" * 8000}]  # 2000 prompt estimate
    assert estimate_request_tokens(messages, max_completion=500) == 2500


def test_estimate_tokens_max_completion_added_to_floor() -> None:
    """Even when the prompt collapses to the floor, the output cap is reserved."""
    assert (
        estimate_request_tokens([], max_completion=1000)
        == ESTIMATED_TOKENS_PER_REQUEST + 1000
    )


def test_estimate_tokens_prompt_capped_but_completion_added_on_top() -> None:
    """The prompt estimate is still capped, but the client-declared output cap is
    added on top (it is self-limited by the caller's own token budget)."""
    messages = [{"role": "user", "content": "x" * 40000}]  # 10000 → capped 4000
    assert estimate_request_tokens(messages, max_completion=2000) == 4000 + 2000


def test_estimate_tokens_negative_max_completion_ignored() -> None:
    messages = [{"role": "user", "content": "x" * 8000}]
    assert estimate_request_tokens(messages, max_completion=-5) == 2000


# ---------------------------------------------------------------------------
# B4: coerce_max_completion clamps the client-declared output cap. A raw JSON int
# has no width limit, so an unbounded max_tokens would reserve an arbitrarily
# large slice of the shared quota/TPM counters before the upstream call.
# ---------------------------------------------------------------------------


def test_coerce_max_completion_clamps_oversized_int() -> None:
    """A multi-billion output cap is clamped to the absolute ceiling so it cannot
    drain the shared department/global quota counter on reservation."""
    assert coerce_max_completion(10_000_000_000) == _MAX_COMPLETION_CEILING


def test_coerce_max_completion_clamps_oversized_str() -> None:
    """The same clamp applies to a stringified int from a loosely-typed client."""
    assert coerce_max_completion("99999999999") == _MAX_COMPLETION_CEILING


def test_coerce_max_completion_passes_through_legitimate_value() -> None:
    """A realistic output cap below the ceiling is preserved unchanged."""
    assert coerce_max_completion(8192) == 8192


def test_coerce_max_completion_at_ceiling_unchanged() -> None:
    """Exactly the ceiling is kept (boundary is inclusive)."""
    assert coerce_max_completion(_MAX_COMPLETION_CEILING) == _MAX_COMPLETION_CEILING


# ---------------------------------------------------------------------------
# B3: settle_tpm must NOT swallow Redis errors. Settlement is billing-critical
# (it reconciles the TPM bucket against real usage), so a failure has to reach
# finalize._settle_tpm, which records it to the billing DLQ for later replay.
# The best-effort rollback callers wrap their own settle_tpm call in suppress.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_settle_tpm_propagates_redis_error(fake_redis: AsyncRedisDouble) -> None:
    """A RedisError during settlement propagates to the caller instead of being
    silently swallowed — finalize._settle_tpm needs it to fire the billing DLQ."""
    rule = _make_rule(tpm_limit=200)

    async def _boom(*_args: object, **_kwargs: object) -> list[int]:
        raise RedisError("redis down")

    fake_redis.eval = _boom  # type: ignore[method-assign]
    with pytest.raises(RedisError):
        await settle_tpm("req-1", [rule], delta_tokens=50)


# ---------------------------------------------------------------------------
# #8 subpart B: fail-open must NOT be unconditional allow. When Redis is down a
# process-local emergency limiter caps each subject so a Redis outage cannot turn
# the gateway into a zero-protection open door.
# ---------------------------------------------------------------------------


class _RedisDownDouble:
    """A Redis double whose every scripted op raises (simulates Redis down)."""

    async def eval(self, *args: object, **kwargs: object) -> object:
        raise RedisError("redis unreachable")


@pytest.mark.asyncio
async def test_fail_open_applies_process_level_emergency_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Oracle #8: when Redis is down, the old code returned allowed=True for
    EVERY request — zero protection. It must instead admit only up to a
    conservative per-subject, per-window emergency cap and deny the rest."""
    from src.gateway.rate_limit import reset_emergency_limiter

    reset_emergency_limiter()
    monkeypatch.setattr(
        "src.gateway.rate_limit.get_redis", lambda: _RedisDownDouble()
    )
    # A small configured rpm caps the emergency window too.
    rule = _make_rule(rule_id=1, subject_id=42, rpm_limit=3)

    admitted = 0
    last = None
    for i in range(5):
        last = await check_rate_limits(
            request_id=f"req-{i}", member=f"m-{i}", rules=[rule]
        )
        if last.allowed:
            admitted += 1

    # Exactly the cap is admitted; the overflow is denied (not allowed=True).
    assert admitted == 3
    assert last is not None
    assert last.allowed is False
    assert last.denied_reason == "degraded_rate_limited"
    assert last.retry_after_seconds is not None and last.retry_after_seconds > 0


@pytest.mark.asyncio
async def test_fail_open_emergency_cap_is_per_subject(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Distinct subjects have independent emergency windows — one subject
    exhausting its degraded budget must not deny a different subject."""
    from src.gateway.rate_limit import reset_emergency_limiter

    reset_emergency_limiter()
    monkeypatch.setattr(
        "src.gateway.rate_limit.get_redis", lambda: _RedisDownDouble()
    )
    rule_a = _make_rule(rule_id=1, subject_id=1, rpm_limit=2)
    rule_b = _make_rule(rule_id=1, subject_id=2, rpm_limit=2)

    # Exhaust subject A.
    await check_rate_limits(request_id="a1", member="m-a1", rules=[rule_a])
    await check_rate_limits(request_id="a2", member="m-a2", rules=[rule_a])
    denied_a = await check_rate_limits(request_id="a3", member="m-a3", rules=[rule_a])
    assert denied_a.allowed is False

    # Subject B is untouched.
    allowed_b = await check_rate_limits(request_id="b1", member="m-b1", rules=[rule_b])
    assert allowed_b.allowed is True


@pytest.mark.asyncio
async def test_fail_open_with_no_rpm_rule_uses_default_emergency_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rule without an rpm_limit still gets a default degraded cap — Redis down
    must never mean unlimited, even for token/concurrent-only rules."""
    from src.gateway.rate_limit import (
        _EMERGENCY_DEFAULT_MAX_PER_WINDOW,
        reset_emergency_limiter,
    )

    reset_emergency_limiter()
    monkeypatch.setattr(
        "src.gateway.rate_limit.get_redis", lambda: _RedisDownDouble()
    )
    rule = _make_rule(rule_id=1, subject_id=99, tpm_limit=1000)  # no rpm_limit

    admitted = 0
    last = None
    for i in range(_EMERGENCY_DEFAULT_MAX_PER_WINDOW + 5):
        last = await check_rate_limits(
            request_id=f"r-{i}", member=f"m-{i}", rules=[rule]
        )
        if last.allowed:
            admitted += 1

    assert admitted == _EMERGENCY_DEFAULT_MAX_PER_WINDOW
    assert last is not None and last.allowed is False


# ---------------------------------------------------------------------------
# #10: the concurrent-semaphore stale timeout must OUTLIVE the longest possible
# stream. When the stale timeout equals the max stream duration, a stream still
# running at the cap is evicted as "stale", freeing its slot for over-admission
# beyond max_concurrent.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_active_stream_not_evicted_before_max_duration(
    fake_redis: AsyncRedisDouble,
) -> None:
    """Oracle #10: an active stream at age ~1900s (past the old 1800s timeout but
    within the legitimate max-duration + finalize slack) must keep its slot. With
    the old 1800s timeout it was wrongly evicted, letting a second stream slip in
    over a max_concurrent=1 cap."""
    rule = _make_rule(rule_id=1, subject_id=7, max_concurrent=1)
    key = "rl:conc:1:user:7:m:1"
    now_ms = int(time.time() * 1000)
    # An active stream that started ~1900s ago — older than the old 1800s stale
    # timeout, but still legitimately running (max duration 1800s + finalize slack).
    await fake_redis.zadd(key, {"active-stream": now_ms - 1_900_000})

    result = await check_rate_limits(
        request_id="req-second",
        member="m-second",
        rules=[rule],
        is_stream=True,
    )

    assert result.allowed is False
    assert result.denied_reason == "concurrent_exceeded"


@pytest.mark.asyncio
async def test_concurrent_truly_stale_stream_is_evicted(
    fake_redis: AsyncRedisDouble,
) -> None:
    """A genuinely dead stream (older than the stale timeout) is still reclaimed —
    the fix raises the threshold, it does not disable stale eviction."""
    rule = _make_rule(rule_id=1, subject_id=8, max_concurrent=1)
    key = "rl:conc:1:user:8:m:1"
    now_ms = int(time.time() * 1000)
    # Older than the 2100s stale timeout → genuinely abandoned, must be reclaimed.
    await fake_redis.zadd(key, {"dead-stream": now_ms - 2_200_000})

    result = await check_rate_limits(
        request_id="req-fresh",
        member="m-fresh",
        rules=[rule],
        is_stream=True,
    )

    assert result.allowed is True
