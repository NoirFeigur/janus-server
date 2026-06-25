"""Hard rate limiting via Redis (P2).

Implements three admission controls:
1. RPM — sliding window (sorted set: score=timestamp, member=request_id).
2. TPM — token bucket (hash: tokens + last_ts; refills tokens/min).
3. Concurrent — streaming semaphore (sorted set: score=started_ms, member=request_id).

All checks run BEFORE quota reservation.  RPM is not refunded on normal
completion (it counts attempts over a sliding window); it IS returned on the
rollback path when a later gate rejects a request that already passed RPM.  TPM
reserves estimated tokens upfront and settles the signed difference with actuals
(refund when under-estimated, extra deduction when over-estimated); concurrent
is released in stream finally.
"""

from __future__ import annotations

import time
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

from redis.exceptions import RedisError

from src.core.logging import get_logger
from src.core.redis import get_redis

_log = get_logger(__name__)

# Conservative upfront TPM reservation per request.  Settled against actual
# token usage at request finalization (see finalize._settle_tpm).  Used as a
# minimum floor when no message-derived estimate is available.
ESTIMATED_TOKENS_PER_REQUEST = 100

# Approximate characters-per-token ratio. ~4 chars/token is the standard
# heuristic across OpenAI / Anthropic / Gemini tokenizers for English; CJK runs
# closer to 1 char/token but the *floor* this drives is fine to under-estimate
# since settlement reconciles to actuals.
_CHARS_PER_TOKEN = 4
# Hard ceiling on the upfront estimate so a single oversized request cannot
# drain the entire bucket via reservation. The signed-delta settlement still
# bills the actual overage at finalize time.
_MAX_UPFRONT_ESTIMATE = 4000

# TPM bucket TTL. MUST outlive the longest possible request so settlement still
# finds the bucket. A stream may run up to ``_STREAM_MAX_DURATION_SECONDS`` (30
# min, mirrored in router._STREAM_MAX_DURATION_SECONDS) before settle_tpm runs;
# at the old 2-min TTL the bucket expired mid-stream, so _LUA_TPM_SETTLE hit
# ``EXISTS == 0`` and silently dropped every over-estimate deduction — letting a
# heavy long-stream caller bypass TPM entirely. 30 min + 1 min slack covers the
# cap plus finalize latency.
_TPM_BUCKET_TTL_MS = 1_860_000


def estimate_request_tokens(messages: object) -> int:
    """Estimate prompt tokens for an upfront TPM reservation.

    Falls back to :data:`ESTIMATED_TOKENS_PER_REQUEST` for inputs we cannot
    inspect, but otherwise returns a length-derived floor so a 5k-prompt request
    against a 30k-TPM bucket cannot bypass the limit by reserving only 100
    tokens upfront and relying on settle-time reconciliation. The estimate is
    capped so a malicious payload cannot starve the bucket.

    Accepts the several input shapes the gateway endpoints carry:

    - chat ``messages``: ``list[{"content": str | list[{"text": str}]}]``
    - embeddings ``input``: ``str`` or ``list[str]``
    - responses ``input``: ``str`` or ``list`` of strings/dicts
    """
    total_chars = _count_input_chars(messages)
    if total_chars <= 0:
        return ESTIMATED_TOKENS_PER_REQUEST
    estimate = max(ESTIMATED_TOKENS_PER_REQUEST, total_chars // _CHARS_PER_TOKEN)
    return min(estimate, _MAX_UPFRONT_ESTIMATE)


def _count_input_chars(value: object) -> int:
    """Best-effort character count across chat/embeddings/responses input shapes."""
    if isinstance(value, str):
        return len(value)
    if not isinstance(value, list):
        return 0
    total_chars = 0
    for item in value:
        if isinstance(item, str):
            total_chars += len(item)
        elif isinstance(item, dict):
            content = item.get("content")
            if content is None:
                # Responses API items may carry text under "text" directly.
                text = item.get("text")
                if isinstance(text, str):
                    total_chars += len(text)
            elif isinstance(content, str):
                total_chars += len(content)
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, dict):
                        text = part.get("text")
                        if isinstance(text, str):
                            total_chars += len(text)
    return total_chars


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
    -- Oldest in-window entry's score (ms). It frees a slot once it ages out of
    -- the window, so the precise retry-after is (oldest + window - now), not a
    -- flat 60s. ZRANGE WITHSCORES returns {member, score}; -1 on empty.
    local oldest = redis.call('ZRANGE', key, 0, 0, 'WITHSCORES')
    local oldest_score = -1
    if oldest[2] ~= nil then
        oldest_score = tonumber(oldest[2])
    end
    return {0, count, limit, oldest_score}
end

redis.call('ZADD', key, now_ms, member)
redis.call('PEXPIRE', key, window_ms + 1000)
return {1, count + 1, limit, -1}
"""

# TPM token bucket check: reserve tokens if available.
_LUA_TPM_CHECK = """
local key = KEYS[1]
local now_ms = tonumber(ARGV[1])
local limit = tonumber(ARGV[2])
local request_tokens = tonumber(ARGV[3])
local refill_rate = tonumber(ARGV[4])
local ttl_ms = tonumber(ARGV[5])

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
redis.call('PEXPIRE', key, ttl_ms)
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
local ttl_ms = tonumber(ARGV[3])

if redis.call('EXISTS', key) == 0 then
    return 0
end

local tokens = tonumber(redis.call('HGET', key, 'tokens')) or 0
tokens = tokens + delta
if tokens > limit then
    tokens = limit
end
redis.call('HSET', key, 'tokens', tokens)
redis.call('PEXPIRE', key, ttl_ms)
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

    tpm_acquired: list[dict[str, Any]] = []
    concurrent_acquired = False

    async def _rollback() -> None:
        if tpm_acquired:
            await settle_tpm(request_id, tpm_acquired, estimated_tokens)
        if concurrent_acquired:
            await release_concurrent(member, rules)

    try:
        redis = get_redis()

        for rule in rules:
            rule_id = rule.get("id", 0)
            subject_key = _subject_key(rule)

            # RPM check
            rpm_limit = rule.get("rpm_limit")
            if rpm_limit is not None:
                key = f"rl:rpm:{rule_id}:{subject_key}"
                result = await redis.eval(
                    _LUA_RPM_CHECK, 1, key, now_ms, 60000, rpm_limit, member
                )
                allowed, current, limit = int(result[0]), int(result[1]), int(result[2])
                rpm_remaining = max(0, limit - current)
                if not allowed:
                    # Precise retry-after: the oldest in-window request frees a
                    # slot once it ages out. result[3] is its score (ms); fall
                    # back to the full window when unavailable.
                    oldest_score = int(result[3]) if len(result) > 3 else -1
                    if oldest_score >= 0:
                        retry_after = max(1, (oldest_score + 60000 - now_ms + 999) // 1000)
                    else:
                        retry_after = 60
                    await _rollback()
                    return RateLimitCheckResult(
                        allowed=False,
                        rpm_remaining=0,
                        retry_after_seconds=retry_after,
                        denied_reason="rpm_exceeded",
                    )

            # TPM check
            tpm_limit = rule.get("tpm_limit")
            if tpm_limit is not None and estimated_tokens > 0:
                key = f"rl:tpm:{rule_id}:{subject_key}"
                tpm_burst_limit = rule.get("tpm_burst_limit") or tpm_limit
                refill_rate = tpm_limit  # tokens per minute
                result = await redis.eval(
                    _LUA_TPM_CHECK,
                    1,
                    key,
                    now_ms,
                    tpm_burst_limit,
                    estimated_tokens,
                    refill_rate,
                    _TPM_BUCKET_TTL_MS,
                )
                allowed, current, limit = int(result[0]), int(result[1]), int(result[2])
                tpm_remaining = current if allowed else 0
                if not allowed:
                    await _rollback()
                    return RateLimitCheckResult(
                        allowed=False,
                        tpm_remaining=0,
                        retry_after_seconds=10,
                        denied_reason="tpm_exceeded",
                    )
                tpm_acquired.append(rule)

            # Concurrent check (streaming only)
            max_concurrent = rule.get("max_concurrent")
            if max_concurrent is not None and is_stream:
                key = f"rl:conc:{rule_id}:{subject_key}"
                timeout_ms = 1800 * 1000  # 30 min max stream
                result = await redis.eval(
                    _LUA_CONCURRENT_CHECK, 1, key, now_ms, max_concurrent,
                    member, timeout_ms
                )
                allowed, current, limit = int(result[0]), int(result[1]), int(result[2])
                concurrent_remaining = max(0, limit - current)
                if not allowed:
                    await _rollback()
                    return RateLimitCheckResult(
                        allowed=False,
                        concurrent_remaining=0,
                        retry_after_seconds=5,
                        denied_reason="concurrent_exceeded",
                    )
                concurrent_acquired = True

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


async def release_rpm(member: str, rules: list[dict[str, Any]]) -> None:
    """Remove this request's RPM sliding-window entry.

    Used only on the rollback path: when a *later* admission gate (quota) rejects
    a request that already passed RPM, the request never executes, so its RPM
    slot should be returned to keep the rollback symmetric with TPM/concurrent.
    ``member`` MUST be the same server-side token passed to
    :func:`check_rate_limits` — removing by a client-controllable id would let a
    caller evict *other* requests' RPM entries.

    Note: this is NOT called on normal completion. A request that actually ran
    keeps consuming its RPM slot for the full window (RPM counts attempts).
    """
    with suppress(RedisError):
        redis = get_redis()
        for rule in rules:
            if rule.get("rpm_limit") is not None:
                rule_id = rule.get("id", 0)
                subject_key = _subject_key(rule)
                key = f"rl:rpm:{rule_id}:{subject_key}"
                await redis.zrem(key, member)


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
                tpm_burst_limit = rule.get("tpm_burst_limit") or tpm_limit
                rule_id = rule.get("id", 0)
                subject_key = _subject_key(rule)
                key = f"rl:tpm:{rule_id}:{subject_key}"
                await redis.eval(
                    _LUA_TPM_SETTLE,
                    1,
                    key,
                    str(delta_tokens),
                    str(tpm_burst_limit),
                    str(_TPM_BUCKET_TTL_MS),
                )


def _subject_key(rule: dict[str, Any]) -> str:
    """Build a subject key for Redis from a rate limit rule."""
    subject_type = rule.get("subject_type", "global")
    subject_id = rule.get("subject_id") or 0
    model_id = rule.get("logical_model_id") or 0
    return f"{subject_type}:{subject_id}:m:{model_id}"
