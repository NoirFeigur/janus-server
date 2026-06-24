"""Hard rate limiting via Redis (P2).

Implements three admission controls:
1. RPM — sliding window (sorted set: score=timestamp, member=request_id).
2. TPM — token bucket (hash: tokens + last_ts; refills tokens/min).
3. Concurrent — streaming semaphore (sorted set: score=started_ms, member=request_id).

All checks run BEFORE quota reservation.  RPM is never refunded; TPM reserves
estimated tokens upfront and settles the signed difference with actuals (refund
when under-estimated, extra deduction when over-estimated); concurrent is
released in stream finally.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, cast

from redis.exceptions import RedisError

from src.core.logging import get_logger
from src.core.redis import get_redis

_log = get_logger(__name__)

# Conservative upfront TPM reservation per request.  Settled against actual
# token usage at request finalization (see finalize._settle_tpm).
ESTIMATED_TOKENS_PER_REQUEST = 100


@dataclass(frozen=True, slots=True)
class RateLimitCheckResult:
    """Result of rate limit check — either allowed or denied with details."""

    allowed: bool
    rpm_remaining: int | None = None
    tpm_remaining: int | None = None
    concurrent_remaining: int | None = None
    retry_after_seconds: int | None = None
    denied_reason: str | None = None


# ---------------------------------------------------------------------------
# Lua scripts (atomicity)
# ---------------------------------------------------------------------------

# RPM sliding window check: ZADD if within limit, else reject.
_LUA_RPM_CHECK = """
local key = KEYS[1]
local now_ms = tonumber(ARGV[1])
local window_ms = tonumber(ARGV[2])
local limit = tonumber(ARGV[3])
local member = ARGV[4]

-- Remove expired entries
redis.call('ZREMRANGEBYSCORE', key, '-inf', now_ms - window_ms)
local count = redis.call('ZCARD', key)

if count >= limit then
    return {0, count, limit}
end

redis.call('ZADD', key, now_ms, member)
redis.call('PEXPIRE', key, window_ms + 1000)
return {1, count + 1, limit}
"""

# TPM token bucket check: reserve tokens if available.
_LUA_TPM_CHECK = """
local key = KEYS[1]
local now_ms = tonumber(ARGV[1])
local limit = tonumber(ARGV[2])
local request_tokens = tonumber(ARGV[3])
local refill_rate = tonumber(ARGV[4])

local data = redis.call('HMGET', key, 'tokens', 'ts')
local tokens = tonumber(data[1]) or limit
local last_ts = tonumber(data[2]) or now_ms

-- Refill tokens based on elapsed time
local elapsed_ms = now_ms - last_ts
local refill = math.floor(elapsed_ms * refill_rate / 60000)
tokens = math.min(limit, tokens + refill)

if tokens < request_tokens then
    return {0, tokens, limit}
end

tokens = tokens - request_tokens
redis.call('HSET', key, 'tokens', tokens, 'ts', now_ms)
redis.call('PEXPIRE', key, 120000)
return {1, tokens, limit}
"""

# TPM settle: apply a signed delta (estimated - actual) to the bucket.
# Positive delta refunds unused reservation (capped at limit); negative delta
# deducts over-consumption beyond the upfront estimate (may drive the bucket
# into debt that recovers via refill). Never touches the refill timestamp.
_LUA_TPM_SETTLE = """
local key = KEYS[1]
local delta = tonumber(ARGV[1])
local limit = tonumber(ARGV[2])

if redis.call('EXISTS', key) == 0 then
    return 0
end

local tokens = tonumber(redis.call('HGET', key, 'tokens')) or 0
tokens = tokens + delta
if tokens > limit then
    tokens = limit
end
redis.call('HSET', key, 'tokens', tokens)
redis.call('PEXPIRE', key, 120000)
return 1
"""

# Concurrent semaphore: acquire slot.
_LUA_CONCURRENT_CHECK = """
local key = KEYS[1]
local now_ms = tonumber(ARGV[1])
local limit = tonumber(ARGV[2])
local member = ARGV[3]
local timeout_ms = tonumber(ARGV[4])

-- Remove stale entries (streams that never released)
redis.call('ZREMRANGEBYSCORE', key, '-inf', now_ms - timeout_ms)
local count = redis.call('ZCARD', key)

if count >= limit then
    return {0, count, limit}
end

redis.call('ZADD', key, now_ms, member)
redis.call('PEXPIRE', key, timeout_ms + 10000)
return {1, count + 1, limit}
"""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def check_rate_limits(
    *,
    request_id: str,
    member: str,
    rules: list[dict[str, Any]],
    estimated_tokens: int = 100,
    is_stream: bool = False,
) -> RateLimitCheckResult:
    """Check all applicable rate limit rules for a request.

    Args:
        request_id: Request identifier, for logging/correlation only.
        member: Server-side unguessable sorted-set member for the RPM sliding
            window and the concurrent semaphore. MUST NOT be derived from any
            client-controllable value (e.g. the ``x-request-id`` header): a
            fixed/repeated member collapses ``ZCARD`` to 1, letting a caller
            replay the same id to defeat RPM and concurrency limits entirely.
            Generate it per request (e.g. ``uuid4().hex``) and pass the *same*
            value to :func:`release_concurrent`.
        rules: List of rate limit rule dicts (from DB/cache).
        estimated_tokens: Conservative token estimate for TPM reservation.
        is_stream: Whether this is a streaming request (for concurrent check).

    Returns:
        RateLimitCheckResult with allowed=True or denied details.
    """
    now_ms = int(time.time() * 1000)
    rpm_remaining: int | None = None
    tpm_remaining: int | None = None
    concurrent_remaining: int | None = None

    try:
        redis = get_redis()

        for rule in rules:
            rule_id = rule.get("id", 0)
            subject_key = _subject_key(rule)

            # RPM check
            rpm_limit = rule.get("rpm_limit")
            if rpm_limit is not None:
                key = f"rl:rpm:{rule_id}:{subject_key}"
                result = await redis.eval(  # type: ignore[union-attr]
                    _LUA_RPM_CHECK, 1, key, now_ms, 60000, rpm_limit, member
                )
                allowed, current, limit = int(result[0]), int(result[1]), int(result[2])
                rpm_remaining = max(0, limit - current)
                if not allowed:
                    return RateLimitCheckResult(
                        allowed=False,
                        rpm_remaining=0,
                        retry_after_seconds=60,
                        denied_reason="rpm_exceeded",
                    )

            # TPM check
            tpm_limit = rule.get("tpm_limit")
            if tpm_limit is not None and estimated_tokens > 0:
                key = f"rl:tpm:{rule_id}:{subject_key}"
                refill_rate = tpm_limit  # tokens per minute
                result = await redis.eval(  # type: ignore[union-attr]
                    _LUA_TPM_CHECK, 1, key, now_ms, tpm_limit, estimated_tokens, refill_rate
                )
                allowed, current, limit = int(result[0]), int(result[1]), int(result[2])
                tpm_remaining = current if allowed else 0
                if not allowed:
                    return RateLimitCheckResult(
                        allowed=False,
                        tpm_remaining=0,
                        retry_after_seconds=10,
                        denied_reason="tpm_exceeded",
                    )

            # Concurrent check (streaming only)
            max_concurrent = rule.get("max_concurrent")
            if max_concurrent is not None and is_stream:
                key = f"rl:conc:{rule_id}:{subject_key}"
                timeout_ms = 1800 * 1000  # 30 min max stream
                result = await redis.eval(  # type: ignore[union-attr]
                    _LUA_CONCURRENT_CHECK, 1, key, now_ms, max_concurrent,
                    member, timeout_ms
                )
                allowed, current, limit = int(result[0]), int(result[1]), int(result[2])
                concurrent_remaining = max(0, limit - current)
                if not allowed:
                    return RateLimitCheckResult(
                        allowed=False,
                        concurrent_remaining=0,
                        retry_after_seconds=5,
                        denied_reason="concurrent_exceeded",
                    )

    except RedisError:
        # Fail-open: if Redis is down, allow the request through
        _log.warning("rate_limit.redis_unavailable", request_id=request_id)
        return RateLimitCheckResult(allowed=True)

    return RateLimitCheckResult(
        allowed=True,
        rpm_remaining=rpm_remaining,
        tpm_remaining=tpm_remaining,
        concurrent_remaining=concurrent_remaining,
    )


async def release_concurrent(member: str, rules: list[dict[str, Any]]) -> None:
    """Release the concurrent semaphore slot acquired at check time.

    ``member`` MUST be the same server-side token passed to
    :func:`check_rate_limits`. Releasing by a client-controllable id would let a
    caller free *other* requests' slots (or fail to free their own).
    """
    with suppress(RedisError):
        redis = get_redis()
        for rule in rules:
            max_concurrent = rule.get("max_concurrent")
            if max_concurrent is not None:
                rule_id = rule.get("id", 0)
                subject_key = _subject_key(rule)
                key = f"rl:conc:{rule_id}:{subject_key}"
                await redis.zrem(key, member)


async def settle_tpm(request_id: str, rules: list[dict[str, Any]], delta_tokens: int) -> None:
    """Reconcile the upfront TPM reservation against actual usage.

    ``delta_tokens`` is ``ESTIMATED_TOKENS_PER_REQUEST - actual_tokens``:

    - **Positive** (actual < estimate): refund the unused reservation, capped at
      the bucket limit so the bucket never exceeds capacity.
    - **Negative** (actual > estimate): deduct the over-consumption beyond the
      upfront estimate so the bucket reflects *real* token usage. Without this,
      TPM would only ever bill the flat estimate per request — i.e. limiting on
      request count, not tokens. The bucket may go into debt and recover via the
      normal time-based refill, which correctly throttles heavy callers.

    A zero delta is a no-op.
    """
    if delta_tokens == 0:
        return
    with suppress(RedisError):
        redis = get_redis()
        for rule in rules:
            tpm_limit = rule.get("tpm_limit")
            if tpm_limit is not None:
                rule_id = rule.get("id", 0)
                subject_key = _subject_key(rule)
                key = f"rl:tpm:{rule_id}:{subject_key}"
                await cast(
                    "Awaitable[Any]",
                    redis.eval(_LUA_TPM_SETTLE, 1, key, str(delta_tokens), str(tpm_limit)),
                )


def _subject_key(rule: dict[str, Any]) -> str:
    """Build a subject key for Redis from a rate limit rule."""
    subject_type = rule.get("subject_type", "global")
    subject_id = rule.get("subject_id") or 0
    model_id = rule.get("logical_model_id") or 0
    return f"{subject_type}:{subject_id}:m:{model_id}"
