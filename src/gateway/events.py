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

# Batch tuning
DEFAULT_BATCH_SIZE = 100
MAX_RETRY_ATTEMPTS = 3
DEFAULT_INFLIGHT_STALE_SECONDS = 300

_ATTEMPT_KEY = "_attempt"
_CLAIMED_AT_KEY = "claimed_at_ms"
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


@dataclass(frozen=True, slots=True)
class ClaimedEvent:
    """A queue item claimed into the inflight list."""

    raw: str
    envelope: str


async def enqueue_event(queue_key: str, payload: dict[str, Any]) -> bool:
    """Push a JSON-serialized event to the specified Redis queue.

    Returns True on success, False on failure (fail-open: never raises).
    """
    try:
        redis = get_redis()
        await redis.rpush(queue_key, json.dumps(payload, default=str))
        return True
    except Exception:
        _log.warning(
            "gateway.event_queue.enqueue_failed",
            queue_key=queue_key,
            payload_keys=list(payload.keys()),
        )
        return False


async def enqueue_usage_event(payload: dict[str, Any]) -> bool:
    """Enqueue a usage recording event (called from gateway finalizer)."""
    return await enqueue_event(USAGE_QUEUE_KEY, payload)


async def enqueue_log_event(payload: dict[str, Any]) -> bool:
    """Enqueue a gateway request log event (called from gateway finalizer)."""
    return await enqueue_event(LOG_QUEUE_KEY, payload)


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
    """Requeue claims that have been inflight longer than ``stale_after_seconds``."""
    recovered = 0
    cutoff_ms = _now_ms() - stale_after_seconds * 1000
    try:
        redis = get_redis()
        envelopes = await redis.lrange(inflight_key, 0, -1)
        for raw_envelope in envelopes:
            envelope = str(raw_envelope)
            claimed = _decode_claimed(envelope)
            if claimed is None:
                await redis.rpush(dlq_key, envelope)
                await redis.lrem(inflight_key, 1, envelope)
                continue
            claimed_at = _claimed_at_ms(envelope)
            if claimed_at is None:
                await redis.rpush(dlq_key, envelope)
                await redis.lrem(inflight_key, 1, envelope)
                continue
            if claimed_at > cutoff_ms:
                continue
            await redis.rpush(queue_key, claimed.raw)
            await redis.lrem(inflight_key, 1, envelope)
            recovered += 1
    except Exception:
        _log.exception(
            "gateway.event_queue.recover_stale_failed",
            queue_key=queue_key,
            inflight_key=inflight_key,
        )
    return recovered


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


def _claimed_at_ms(envelope: str) -> int | None:
    with suppress(json.JSONDecodeError, TypeError, ValueError):
        data = json.loads(envelope)
        return int(data.get(_CLAIMED_AT_KEY))
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
