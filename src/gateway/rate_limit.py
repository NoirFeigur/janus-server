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

# Absolute upper bound on the client-declared output cap (``max_tokens`` /
# ``max_completion_tokens`` / ``maxOutputTokens`` / ``max_output_tokens``). The
# raw JSON integer is unbounded (Python ints have no width limit), so a single
# request could declare a multi-billion output ceiling. That ceiling is added to
# the prompt estimate in :func:`estimate_request_tokens` and then reserved
# against the shared department/global quota AND the TPM bucket BEFORE the
# upstream call — a single crafted request could exhaust the entire shared quota
# counter for everyone. Clamp to a value comfortably above any real model's
# output window (current frontier reasoning models top out near ~128k output) so
# legitimate large generations are untouched while the abuse vector is closed.
# Settlement at finalize still reconciles to actual usage either way.
_MAX_COMPLETION_CEILING = 200_000

# TPM bucket TTL. MUST outlive the longest possible request so settlement still
# finds the bucket. A stream may run up to ``_STREAM_MAX_DURATION_SECONDS`` (30
# min, mirrored in router._STREAM_MAX_DURATION_SECONDS) before settle_tpm runs;
# at the old 2-min TTL the bucket expired mid-stream, so _LUA_TPM_SETTLE hit
# ``EXISTS == 0`` and silently dropped every over-estimate deduction — letting a
# heavy long-stream caller bypass TPM entirely. 30 min + 1 min slack covers the
# cap plus finalize latency.
_TPM_BUCKET_TTL_MS = 1_860_000

# Concurrent-semaphore stale-eviction timeout (Oracle #10). MUST strictly outlive
# the longest legitimate stream so an ACTIVE stream is never mistaken for an
# abandoned slot. The old value equalled the 30-min max stream duration
# (``_STREAM_MAX_DURATION_SECONDS`` in router), so a stream still running at the
# cap was evicted as "stale" and its slot handed to a new request — silently
# admitting beyond ``max_concurrent``. 35 min = 30-min cap + 5-min finalize/settle
# slack: long enough that a live stream keeps its slot, short enough that a truly
# crashed stream's slot is still reclaimed within a few minutes of the cap.
_CONCURRENT_STALE_TIMEOUT_SECONDS = 2100

# Process-local emergency rate limiter (Oracle #8). When Redis is unreachable the
# Redis-backed admission controls cannot run. The original code failed fully open
# (``allowed=True`` for EVERY request) — a Redis outage turned the gateway into a
# zero-protection door, exposing upstream providers to unbounded load at the very
# moment the system is already degraded. Instead each replica applies a
# conservative, PROCESS-LOCAL fixed-window cap per subject so abusers are still
# throttled during the outage.
#
# This is explicitly best-effort and degraded: it is per-process, NOT
# cluster-wide, so the effective cluster cap is roughly ``cap × replica_count``.
# That is an acceptable, bounded over-admission compared to unlimited. The window
# is a coarse fixed 60s bucket — cheap and lock-free under asyncio's single
# thread (the event loop serialises the check+commit, so no race). State is
# bounded by distinct-subject cardinality (~user count): stale windows are reset
# on next access, never accumulating per-time-bucket entries.
_EMERGENCY_WINDOW_SECONDS = 60
# Degraded cap for a subject whose rule has no rpm_limit (token/concurrent-only):
# Redis down must never mean unlimited even for these.
_EMERGENCY_DEFAULT_MAX_PER_WINDOW = 60
# subject_key -> (window_index, count_in_window)
_emergency_windows: dict[str, tuple[int, int]] = {}


def reset_emergency_limiter() -> None:
    """Clear process-local emergency limiter state (test isolation / reset)."""
    _emergency_windows.clear()


def _emergency_admit(rules: list[dict[str, Any]], now_ms: int) -> bool:
    """Best-effort process-local fixed-window admission, used ONLY while Redis is
    down (the fail-open path of :func:`check_rate_limits`).

    Returns True only if the request fits under EVERY applicable subject's
    degraded window budget; False if any subject has exhausted it. The per-subject
    cap is the rule's ``rpm_limit`` when set (so degraded behaviour tracks the
    configured intent), else :data:`_EMERGENCY_DEFAULT_MAX_PER_WINDOW`.

    Headroom is checked for all subjects BEFORE any increment so a multi-rule
    request is admitted atomically (all-or-nothing) and a denial does not leave a
    partial increment that would mis-charge an unrelated subject's window.
    """
    if not rules:
        # No rules → no subject identity to throttle against; nothing to cap.
        return True
    window_index = (now_ms // 1000) // _EMERGENCY_WINDOW_SECONDS
    subjects: list[tuple[str, int]] = []
    for rule in rules:
        subject = _subject_key(rule)
        rpm_limit = rule.get("rpm_limit")
        cap = int(rpm_limit) if rpm_limit is not None else _EMERGENCY_DEFAULT_MAX_PER_WINDOW
        subjects.append((subject, cap))

    def _current_count(subject: str) -> int:
        win, count = _emergency_windows.get(subject, (window_index, 0))
        return count if win == window_index else 0

    # Phase 1: check headroom for ALL subjects.
    for subject, cap in subjects:
        if _current_count(subject) >= cap:
            return False
    # Phase 2: commit increments (event loop serialises this with phase 1).
    for subject, _cap in subjects:
        _emergency_windows[subject] = (window_index, _current_count(subject) + 1)
    return True


def estimate_request_tokens(messages: object, max_completion: int = 0) -> int:
    """Estimate total tokens for an upfront TPM reservation (prompt + output cap).

    Returns ``prompt_estimate + max_completion`` so the reservation covers BOTH
    the input AND the tokens the model may generate (Oracle #3). Reserving only
    the prompt let a small-prompt request with a large ``max_tokens`` slip a big
    generation past a tight TPM bucket — TPM then effectively limited input size,
    not throughput. Settlement at finalize reconciles to actuals either way.

    The prompt portion falls back to :data:`ESTIMATED_TOKENS_PER_REQUEST` for
    inputs we cannot inspect, and is capped at :data:`_MAX_UPFRONT_ESTIMATE` so a
    malicious payload cannot starve the bucket. ``max_completion`` is the
    client-declared output ceiling (already self-limited by the caller's own
    token budget) and is added on top of the capped prompt estimate; it is
    floored at 0 and clamped to :data:`_MAX_COMPLETION_CEILING` HERE — this is
    the single convergence point every endpoint funnels through, so the clamp
    closes the unbounded-reservation vector even on paths that pass a raw
    ``max_tokens`` without going through :func:`coerce_max_completion` (e.g. the
    Anthropic native ``/v1/messages`` route).

    Accepts the several input shapes the gateway endpoints carry:

    - chat ``messages``: ``list[{"content": str | list[{"text": str}]}]``
    - embeddings ``input``: ``str`` or ``list[str]``
    - responses ``input``: ``str`` or ``list`` of strings/dicts
    """
    total_chars = _count_input_chars(messages)
    if total_chars <= 0:
        prompt_estimate = ESTIMATED_TOKENS_PER_REQUEST
    else:
        prompt_estimate = max(
            ESTIMATED_TOKENS_PER_REQUEST, total_chars // _CHARS_PER_TOKEN
        )
        prompt_estimate = min(prompt_estimate, _MAX_UPFRONT_ESTIMATE)
    return prompt_estimate + _clamp_completion(max_completion)


def coerce_max_completion(value: object) -> int:
    """Normalize a provider-declared output-token cap into a non-negative int.

    Providers carry the output ceiling in differently-typed, optional fields
    (Gemini ``generationConfig["maxOutputTokens"]``, OpenAI ``max_tokens`` /
    ``max_completion_tokens``, Responses ``max_output_tokens``). The value may be
    absent (``None``), a bare int, or a stringified int from a loosely-typed
    client. Anything we cannot read as a positive int contributes nothing to the
    reservation rather than raising on the hot path.
    """
    if isinstance(value, bool):  # bool is an int subclass — never a token count
        return 0
    if isinstance(value, int):
        return _clamp_completion(value)
    if isinstance(value, str):
        with suppress(ValueError):
            return _clamp_completion(int(value.strip()))
    return 0


def _clamp_completion(value: int) -> int:
    """Floor at 0, ceil at :data:`_MAX_COMPLETION_CEILING`.

    The ceiling closes the unbounded-reservation abuse vector (a raw JSON int has
    no width limit, so a crafted ``max_tokens`` could reserve an arbitrarily large
    slice of the shared quota/TPM counters before the upstream call).
    """
    return max(0, min(value, _MAX_COMPLETION_CEILING))


def _count_input_chars(value: object) -> int:
    """Best-effort character count across chat/embeddings/responses/Gemini shapes."""
    if isinstance(value, str):
        return len(value)
    if not isinstance(value, list):
        return 0
    total_chars = 0
    for item in value:
        if isinstance(item, str):
            total_chars += len(item)
        elif isinstance(item, dict):
            total_chars += _count_item_chars(item)
    return total_chars


def _count_item_chars(item: dict[str, Any]) -> int:
    """Count text chars in one input item across all provider message shapes.

    Handled shapes:
    - Gemini content: ``{"role": ..., "parts": [{"text": str}]}`` — the ``parts``
      array MUST be inspected, otherwise a multi-thousand-char Gemini prompt
      counts as 0 and the TPM upfront estimate collapses to the default floor,
      letting a large prompt bypass a tight TPM bucket on its first request.
    - chat content: ``{"content": str | list[{"text": str}]}``
    - responses item: ``{"text": str}`` (text carried directly).
    """
    parts = item.get("parts")
    if isinstance(parts, list):
        return _count_parts_chars(parts)
    content = item.get("content")
    if content is None:
        # Responses API items may carry text under "text" directly.
        text = item.get("text")
        return len(text) if isinstance(text, str) else 0
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        return _count_parts_chars(content)
    return 0


def _count_parts_chars(parts: list[Any]) -> int:
    """Sum ``text`` chars across a list of content/parts blocks."""
    total = 0
    for part in parts:
        if isinstance(part, dict):
            text = part.get("text")
            if isinstance(text, str):
                total += len(text)
    return total


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
#
# Debt-aware TTL (Oracle #4): when the settle drives the balance negative, the
# bucket must outlive the time refill needs to repay that debt — otherwise the
# key expires mid-recovery, the next request reads a fresh full bucket, and the
# heavy caller's over-consumption is silently forgiven (TPM bypass). Recovery
# time is ``abs(debt) / refill_rate`` minutes; we set the TTL to that window plus
# the base TTL as slack, and never shrink below the base TTL.
_LUA_TPM_SETTLE = """
local key = KEYS[1]
local delta = tonumber(ARGV[1])
local limit = tonumber(ARGV[2])
local ttl_ms = tonumber(ARGV[3])
local refill_rate = tonumber(ARGV[4])

if redis.call('EXISTS', key) == 0 then
    return 0
end

local tokens = tonumber(redis.call('HGET', key, 'tokens')) or 0
tokens = tokens + delta
if tokens > limit then
    tokens = limit
end
redis.call('HSET', key, 'tokens', tokens)

local effective_ttl = ttl_ms
if tokens < 0 and refill_rate and refill_rate > 0 then
    local recovery_ms = math.ceil((-tokens) * 60000 / refill_rate)
    local debt_ttl = recovery_ms + ttl_ms
    if debt_ttl > effective_ttl then
        effective_ttl = debt_ttl
    end
end
redis.call('PEXPIRE', key, effective_ttl)
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

    rpm_acquired: list[dict[str, Any]] = []
    tpm_acquired: list[dict[str, Any]] = []
    concurrent_acquired = False

    async def _rollback() -> None:
        # Release every gate this request actually acquired, in any order — a
        # later gate's denial means the request never runs, so its RPM window
        # entry, TPM reservation and concurrent slot must ALL be returned.
        # Omitting RPM (the original bug) leaked the sliding-window slot: a
        # never-admitted request kept throttling the subject for a full minute.
        #
        # This rollback is best-effort: a Redis failure mid-rollback must NOT
        # escape, or it would unwind into the enclosing ``except RedisError`` and
        # flip a deny (rpm/tpm/concurrent exceeded) into an emergency-admit —
        # i.e. a rate-limit BYPASS exactly under load. ``release_rpm`` /
        # ``release_concurrent`` already suppress internally; ``settle_tpm`` no
        # longer does (it must surface to the billing DLQ on the finalize path),
        # so it is suppressed HERE, on this admission-control rollback path only.
        if rpm_acquired:
            await release_rpm(member, rpm_acquired)
        if tpm_acquired:
            with suppress(RedisError):
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
                rpm_acquired.append(rule)

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
                timeout_ms = _CONCURRENT_STALE_TIMEOUT_SECONDS * 1000
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
        # Redis is down — the Redis-backed admission controls cannot run. Rather
        # than failing fully open (the original bug: allowed=True for EVERY
        # request, zero protection), apply a degraded PROCESS-LOCAL emergency cap
        # per subject (Oracle #8). This is a controlled fail-open: legitimate
        # traffic still flows, but an abuser cannot use the outage to flood
        # upstreams unbounded. Surfaced as a health-alert warning so ops can react.
        admitted = _emergency_admit(rules, now_ms)
        _log.warning(
            "rate_limit.redis_unavailable",
            request_id=request_id,
            degraded_admitted=admitted,
            health_alert="rate_limit_degraded",
        )
        if admitted:
            return RateLimitCheckResult(allowed=True)
        return RateLimitCheckResult(
            allowed=False,
            retry_after_seconds=_EMERGENCY_WINDOW_SECONDS,
            denied_reason="degraded_rate_limited",
        )

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

    Redis failures are NOT swallowed here: settlement is billing-critical (it
    reconciles the TPM bucket against real token usage), so a failure must reach
    :func:`finalize._settle_tpm`, which records it to the billing DLQ for later
    replay. The best-effort rollback callers (``check_rate_limits._rollback``,
    ``router`` / ``endpoints_v1._rollback_rate_limit_reservations``) each wrap
    their ``settle_tpm`` call in their own ``suppress(Exception)``, so surfacing
    the error here does not break their fire-and-forget semantics.
    """
    if delta_tokens == 0:
        return
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
                str(tpm_limit),
            )


def _subject_key(rule: dict[str, Any]) -> str:
    """Build a subject key for Redis from a rate limit rule."""
    subject_type = rule.get("subject_type", "global")
    subject_id = rule.get("subject_id") or 0
    model_id = rule.get("logical_model_id") or 0
    return f"{subject_type}:{subject_id}:m:{model_id}"
