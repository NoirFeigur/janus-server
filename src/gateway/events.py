"""Durable Redis-backed event queue for gateway events (usage + logs).

Events are pushed to a Redis list (RPUSH), claimed by batch workers into an
inflight list, and acknowledged only after the DB write succeeds.  Failed
claims are retried a bounded number of times before they are moved to a
dead-letter list.  This replaces the fire-and-forget ``asyncio.create_task``
pattern with durable enqueuing.

Design:
- Hot path awaits only one Redis RPUSH (sub-millisecond, non-blocking).
- Batch worker (ARQ cron) claims N items, writes to DB in bulk, then ACKs.
- If Redis is unreachable during enqueue, log the event and drop gracefully
  (fail-open: never block the gateway response for internal accounting).
"""

from __future__ import annotations

import json
import time
from collections import deque
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

from src.core.logging import get_logger
from src.core.redis import get_redis

_log = get_logger(__name__)

# Redis key constants
USAGE_QUEUE_KEY = "gw:event:usage:pending"
LOG_QUEUE_KEY = "gw:event:log:pending"
USAGE_INFLIGHT_KEY = "gw:event:usage:inflight"
LOG_INFLIGHT_KEY = "gw:event:log:inflight"
USAGE_DLQ_KEY = "gw:event:usage:dlq"
LOG_DLQ_KEY = "gw:event:log:dlq"
# Billing-failure DLQ (Oracle #9). A billing-critical finalize step (quota
# settle, TPM settle, usage enqueue) that RAISES must not vanish into a
# suppress(Exception): the lost settlement is silent revenue/limit drift. Each
# failure is recorded here (durably, via the outbox-backed enqueue) so it is
# observable and replayable, while the finalize step itself stays non-fatal so a
# single billing hiccup never aborts the response or its sibling steps.
BILLING_DLQ_KEY = "gw:event:billing:dlq"

# Batch tuning
DEFAULT_BATCH_SIZE = 100
MAX_RETRY_ATTEMPTS = 3
DEFAULT_INFLIGHT_STALE_SECONDS = 300

# Process-local outbox (Oracle #8). When a Redis enqueue fails (transient outage),
# the event is parked here instead of being silently dropped — losing a usage
# event loses billing/revenue. On the next *successful* enqueue (Redis recovered)
# the backlog is replayed FIFO ahead of the new event, so accounting catches up.
# Bounded per queue so a prolonged outage cannot exhaust process memory: once the
# cap is hit the OLDEST buffered events are evicted (a deque maxlen does this),
# trading the least-recent accounting data for liveness. This is a last-resort
# net for short blips, NOT a durable store — sustained Redis loss still needs ops
# intervention, surfaced via the eviction warning.
_OUTBOX_MAX_PER_QUEUE = 10_000
_outboxes: dict[str, deque[str]] = {}


def _outbox_for(queue_key: str) -> deque[str]:
    box = _outboxes.get(queue_key)
    if box is None:
        box = deque(maxlen=_OUTBOX_MAX_PER_QUEUE)
        _outboxes[queue_key] = box
    return box


def _outbox_size(queue_key: str) -> int:
    """Number of events currently parked in the local outbox for ``queue_key``."""
    box = _outboxes.get(queue_key)
    return len(box) if box is not None else 0


def reset_outbox() -> None:
    """Clear all process-local outboxes (test isolation / explicit reset)."""
    _outboxes.clear()

_ATTEMPT_KEY = "_attempt"
_PAYLOAD_KEY = "payload"

_LUA_CLAIM_EVENT = """
-- CLAIM_EVENT_QUEUE
local payload = redis.call("LPOP", KEYS[1])
if not payload then
    return nil
end
local envelope = cjson.encode({
    payload = payload,
    claimed_at_ms = ARGV[1]
})
redis.call("RPUSH", KEYS[2], envelope)
return envelope
"""

# Atomic stale-claim recovery (Oracle #13). The old Python version did a
# non-atomic LRANGE snapshot then per-envelope RPUSH+LREM: two concurrent
# recover passes (e.g. two ARQ workers) both snapshot the same stale envelope
# and BOTH requeue it, double-processing the event (double billing / duplicate
# log row). Doing the whole scan-requeue-remove inside one Lua script makes it a
# single atomic Redis operation — the second pass sees the envelope already gone.
# Returns {recovered_count, dlq_count}. ``LREM 1`` removes the FIRST matching
# envelope; the per-claim ``claimed_at_ms`` makes envelopes effectively unique.
_LUA_RECOVER_STALE = """
-- RECOVER_STALE_CLAIMS
local inflight_key = KEYS[1]
local queue_key = KEYS[2]
local dlq_key = KEYS[3]
local cutoff_ms = tonumber(ARGV[1])

local envelopes = redis.call("LRANGE", inflight_key, 0, -1)
local recovered = 0
local dlqd = 0

for _, envelope in ipairs(envelopes) do
    local ok, decoded = pcall(cjson.decode, envelope)
    if not ok or type(decoded) ~= "table" or decoded.payload == nil then
        -- Undecodable / malformed envelope: dead-letter it.
        redis.call("RPUSH", dlq_key, envelope)
        redis.call("LREM", inflight_key, 1, envelope)
        dlqd = dlqd + 1
    else
        local claimed_at = tonumber(decoded.claimed_at_ms)
        if claimed_at == nil then
            redis.call("RPUSH", dlq_key, envelope)
            redis.call("LREM", inflight_key, 1, envelope)
            dlqd = dlqd + 1
        elseif claimed_at <= cutoff_ms then
            redis.call("RPUSH", queue_key, decoded.payload)
            redis.call("LREM", inflight_key, 1, envelope)
            recovered = recovered + 1
        end
    end
end

return {recovered, dlqd}
"""


@dataclass(frozen=True, slots=True)
class ClaimedEvent:
    """A queue item claimed into the inflight list."""

    raw: str
    envelope: str


async def enqueue_event(queue_key: str, payload: dict[str, Any]) -> bool:
    """Push a JSON-serialized event to the specified Redis queue.

    Returns True when the event reached Redis, False when it was parked in the
    process-local outbox (Oracle #8) because Redis was unreachable. Never raises:
    the hot path must not block on internal accounting.

    On a successful push, any backlog accumulated in the outbox during a prior
    outage is replayed FIFO *before* this event, so accounting order is
    preserved as closely as possible once Redis recovers.
    """
    serialized = json.dumps(payload, default=str)
    try:
        redis = get_redis()
        await _flush_outbox(redis, queue_key)
        await redis.rpush(queue_key, serialized)
        return True
    except Exception:
        box = _outbox_for(queue_key)
        evicting = len(box) >= _OUTBOX_MAX_PER_QUEUE
        box.append(serialized)
        _log.warning(
            "gateway.event_queue.enqueue_buffered",
            queue_key=queue_key,
            payload_keys=list(payload.keys()),
            outbox_size=len(box),
            outbox_evicting=evicting,
        )
        return False


async def _flush_outbox(redis: Any, queue_key: str) -> None:
    """Replay any outbox backlog for ``queue_key`` to Redis, oldest first.

    Called at the top of a successful enqueue. If a replay push fails the event
    is put back at the FRONT of the outbox and the flush stops, so ordering is
    preserved and the still-failing backlog is retried on the next attempt.
    """
    box = _outboxes.get(queue_key)
    if not box:
        return
    while box:
        item = box.popleft()
        try:
            await redis.rpush(queue_key, item)
        except Exception:
            box.appendleft(item)
            raise


async def enqueue_usage_event(payload: dict[str, Any]) -> bool:
    """Enqueue a usage recording event (called from gateway finalizer)."""
    return await enqueue_event(USAGE_QUEUE_KEY, payload)


async def enqueue_log_event(payload: dict[str, Any]) -> bool:
    """Enqueue a gateway request log event (called from gateway finalizer)."""
    return await enqueue_event(LOG_QUEUE_KEY, payload)


async def record_billing_failure(
    *, kind: str, request_id: str | None, error: str, detail: dict[str, Any] | None = None
) -> None:
    """Record a billing-critical finalize-step failure to the billing DLQ (Oracle #9).

    A raised quota/TPM settle or usage enqueue would otherwise be swallowed by a
    blanket ``suppress(Exception)``, silently dropping revenue/limit accounting.
    This logs at ERROR (the always-available backstop alert) and durably enqueues
    a structured record to :data:`BILLING_DLQ_KEY` for inspection and replay. It
    is itself non-fatal: the enqueue is outbox-backed and never raises, so
    recording a billing failure cannot in turn abort the finalize step.
    """
    _log.error(
        "gateway.billing.step_failed",
        kind=kind,
        request_id=request_id,
        error=error,
    )
    record = {
        "kind": kind,
        "request_id": request_id,
        "error": error,
        "detail": detail or {},
        "recorded_at_ms": _now_ms(),
    }
    await enqueue_event(BILLING_DLQ_KEY, record)


async def claim_batch(
    queue_key: str,
    inflight_key: str,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> list[ClaimedEvent]:
    """Atomically claim up to ``batch_size`` pending items into inflight.

    Items remain in Redis until :func:`ack_claimed` removes their inflight
    envelopes.  If the worker crashes after claim but before DB commit, a later
    :func:`recover_stale_claims` call can put the payload back on the pending
    list.
    """
    try:
        redis = get_redis()
        claimed: list[ClaimedEvent] = []
        for _ in range(batch_size):
            envelope = await redis.eval(
                _LUA_CLAIM_EVENT,
                2,
                queue_key,
                inflight_key,
                str(_now_ms()),
            )
            if envelope is None:
                break
            decoded = _decode_claimed(str(envelope))
            if decoded is not None:
                claimed.append(decoded)
        return claimed
    except Exception:
        _log.exception("gateway.event_queue.claim_batch_failed", queue_key=queue_key)
        return []


async def ack_claimed(inflight_key: str, items: list[ClaimedEvent]) -> int:
    """ACK claimed items by removing their inflight envelopes."""
    if not items:
        return 0
    acked = 0
    try:
        redis = get_redis()
        for item in items:
            acked += int(await redis.lrem(inflight_key, 1, item.envelope))
    except Exception:
        _log.exception("gateway.event_queue.ack_failed", inflight_key=inflight_key)
    return acked


async def retry_or_dlq_claimed(
    *,
    queue_key: str,
    inflight_key: str,
    dlq_key: str,
    items: list[ClaimedEvent],
    max_attempts: int = MAX_RETRY_ATTEMPTS,
) -> None:
    """Retry claimed items, moving them to DLQ after ``max_attempts`` failures."""
    if not items:
        return
    try:
        redis = get_redis()
        for item in items:
            payload, attempts = _increment_attempt(item.raw)
            if attempts >= max_attempts:
                await redis.rpush(dlq_key, payload)
            else:
                await redis.rpush(queue_key, payload)
            await redis.lrem(inflight_key, 1, item.envelope)
    except Exception:
        _log.exception(
            "gateway.event_queue.retry_or_dlq_failed",
            queue_key=queue_key,
            inflight_key=inflight_key,
            dlq_key=dlq_key,
        )


async def dlq_claimed(inflight_key: str, dlq_key: str, items: list[ClaimedEvent]) -> None:
    """Move invalid claimed items to DLQ and remove them from inflight."""
    if not items:
        return
    try:
        redis = get_redis()
        for item in items:
            await redis.rpush(dlq_key, item.raw)
            await redis.lrem(inflight_key, 1, item.envelope)
    except Exception:
        _log.exception(
            "gateway.event_queue.dlq_claimed_failed",
            inflight_key=inflight_key,
            dlq_key=dlq_key,
        )


async def recover_stale_claims(
    *,
    queue_key: str,
    inflight_key: str,
    dlq_key: str,
    stale_after_seconds: int = DEFAULT_INFLIGHT_STALE_SECONDS,
) -> int:
    """Requeue claims that have been inflight longer than ``stale_after_seconds``.

    Atomic (Oracle #13): the entire scan-requeue-remove runs inside a single Lua
    script so two concurrent recover passes (e.g. two ARQ workers) cannot both
    requeue the same stale envelope. Malformed / undecodable envelopes are
    dead-lettered. Returns the number of envelopes requeued to ``queue_key``.
    """
    cutoff_ms = _now_ms() - stale_after_seconds * 1000
    try:
        redis = get_redis()
        result = await redis.eval(
            _LUA_RECOVER_STALE,
            3,
            inflight_key,
            queue_key,
            dlq_key,
            str(cutoff_ms),
        )
        # Script returns {recovered_count, dlq_count}.
        if isinstance(result, (list, tuple)) and result:
            return int(result[0])
        return 0
    except Exception:
        _log.exception(
            "gateway.event_queue.recover_stale_failed",
            queue_key=queue_key,
            inflight_key=inflight_key,
        )
        return 0


async def send_to_dlq(dlq_key: str, items: list[str]) -> None:
    """Move failed items to the dead-letter queue for manual inspection."""
    with suppress(Exception):
        redis = get_redis()
        if items:
            await redis.rpush(dlq_key, *items)


async def get_queue_length(queue_key: str) -> int:
    """Return current queue length (for admin health views)."""
    with suppress(Exception):
        redis = get_redis()
        length = await redis.llen(queue_key)
        return int(length)
    return 0


async def peek_dlq(dlq_key: str, count: int = 20) -> list[dict[str, Any]]:
    """Peek at dead-letter items without removing them (admin view)."""
    try:
        redis = get_redis()
        raw_items = await redis.lrange(dlq_key, 0, count - 1)
        results: list[dict[str, Any]] = []
        for raw in raw_items:
            with suppress(json.JSONDecodeError):
                results.append(json.loads(raw))
        return results
    except Exception:
        return []


async def flush_dlq(dlq_key: str) -> int:
    """Remove and return count of dead-letter items (admin action)."""
    try:
        redis = get_redis()
        length = await redis.llen(dlq_key)
        await redis.delete(dlq_key)
        return int(length)
    except Exception:
        return 0


def _now_ms() -> int:
    return int(time.time() * 1000)


def _decode_claimed(envelope: str) -> ClaimedEvent | None:
    with suppress(json.JSONDecodeError, TypeError):
        data = json.loads(envelope)
        payload = data.get(_PAYLOAD_KEY)
        if isinstance(payload, str):
            return ClaimedEvent(raw=payload, envelope=envelope)
    return None


def _increment_attempt(raw: str) -> tuple[str, int]:
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        payload = {"raw": raw}
    if not isinstance(payload, dict):
        payload = {"raw": payload}
    attempts = int(payload.get(_ATTEMPT_KEY, 0) or 0) + 1
    payload[_ATTEMPT_KEY] = attempts
    return json.dumps(payload, default=str), attempts
