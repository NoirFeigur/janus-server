"""Durable Redis-backed event queue for gateway events (usage + logs).

Events are pushed to a Redis list (RPUSH), claimed by batch workers (LPOP N),
and moved to a dead-letter list after max retries.  This replaces the
fire-and-forget ``asyncio.create_task`` pattern with durable enqueuing.

Design:
- Hot path awaits only one Redis RPUSH (sub-millisecond, non-blocking).
- Batch worker (ARQ cron) pops N items, writes to DB in bulk, retries on failure.
- If Redis is unreachable during enqueue, log the event and drop gracefully
  (fail-open: never block the gateway response for internal accounting).
"""

from __future__ import annotations

import json
from contextlib import suppress
from typing import Any

from src.core.logging import get_logger
from src.core.redis import get_redis

_log = get_logger(__name__)

# Redis key constants
USAGE_QUEUE_KEY = "gw:event:usage:pending"
LOG_QUEUE_KEY = "gw:event:log:pending"
USAGE_DLQ_KEY = "gw:event:usage:dlq"
LOG_DLQ_KEY = "gw:event:log:dlq"

# Batch tuning
DEFAULT_BATCH_SIZE = 100
MAX_RETRY_ATTEMPTS = 3


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


async def pop_batch(queue_key: str, batch_size: int = DEFAULT_BATCH_SIZE) -> list[str]:
    """Pop up to ``batch_size`` items from the queue atomically.

    Returns raw JSON strings.  Empty list if queue is empty or Redis is down.
    """
    try:
        redis = get_redis()
        items: list[str] = []
        for _ in range(batch_size):
            item = await redis.lpop(queue_key)
            if item is None:
                break
            items.append(item)  # type: ignore[arg-type]
        return items
    except Exception:
        _log.exception("gateway.event_queue.pop_batch_failed", queue_key=queue_key)
        return []


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
