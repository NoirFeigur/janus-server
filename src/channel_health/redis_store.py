"""Redis-backed sliding window health counters and degraded set.

Each channel has per-minute buckets tracking total/error counts.  The degraded
set (Redis SET) holds channel IDs currently excluded from Router construction.
"""

from __future__ import annotations

import time
from contextlib import suppress
from typing import Any

from redis.exceptions import RedisError

from src.core.logging import get_logger
from src.core.redis import get_redis

_log = get_logger(__name__)

# Redis key patterns
_BUCKET_PREFIX = "ch:health:"  # ch:health:{channel_id}:{minute_bucket}
_DEGRADED_SET_KEY = "ch:health:degraded"
_STATE_PREFIX = "ch:health:state:"  # ch:health:state:{channel_id}
_PROBE_LOCK_PREFIX = "ch:health:probe:lock:"  # ch:health:probe:lock:{channel_id}


def _bucket_key(channel_id: int, minute_bucket: int) -> str:
    return f"{_BUCKET_PREFIX}{channel_id}:{minute_bucket}"


def _state_key(channel_id: int) -> str:
    return f"{_STATE_PREFIX}{channel_id}"


def _probe_lock_key(channel_id: int) -> str:
    return f"{_PROBE_LOCK_PREFIX}{channel_id}"


def _current_minute_bucket() -> int:
    return int(time.time() // 60)


# ---------------------------------------------------------------------------
# Health recording
# ---------------------------------------------------------------------------


async def record_request_outcome(
    channel_id: int,
    *,
    success: bool,
    error_class: str | None = None,
    window_seconds: int = 300,
) -> None:
    """Record a request outcome for the given channel.

    Increments the appropriate counter in the current minute bucket.
    Fail-open: Redis errors are silently logged (never blocks gateway response).
    """
    with suppress(Exception):
        redis = get_redis()
        bucket = _current_minute_bucket()
        key = _bucket_key(channel_id, bucket)
        ttl = window_seconds + 120  # Keep bucket slightly beyond the window

        pipe = redis.pipeline(transaction=False)
        pipe.hincrby(key, "total", 1)
        if not success:
            pipe.hincrby(key, "error", 1)
            if error_class:
                pipe.hincrby(key, error_class, 1)
        pipe.expire(key, ttl)
        await pipe.execute()


async def get_error_rate(channel_id: int, window_seconds: int = 300) -> tuple[int, int, float]:
    """Get (total, errors, error_rate) for a channel over the sliding window.

    Returns (0, 0, 0.0) on Redis failure (fail-open).
    """
    try:
        redis = get_redis()
        current_bucket = _current_minute_bucket()
        num_buckets = (window_seconds // 60) + 1

        total = 0
        errors = 0
        pipe = redis.pipeline(transaction=False)
        for i in range(num_buckets):
            key = _bucket_key(channel_id, current_bucket - i)
            pipe.hget(key, "total")
            pipe.hget(key, "error")

        results = await pipe.execute()
        for j in range(0, len(results), 2):
            t = results[j]
            e = results[j + 1]
            if t is not None:
                total += int(t)
            if e is not None:
                errors += int(e)

        rate = (errors / total) if total > 0 else 0.0
        return total, errors, rate
    except (RedisError, ValueError):
        return 0, 0, 0.0


# ---------------------------------------------------------------------------
# Degraded set management
# ---------------------------------------------------------------------------


async def add_to_degraded(channel_id: int) -> bool:
    """Add channel to the degraded set.  Returns True if this was the first adder (winner)."""
    try:
        redis = get_redis()
        added = await redis.sadd(_DEGRADED_SET_KEY, str(channel_id))
        return bool(added)
    except RedisError:
        return False


async def remove_from_degraded(channel_id: int) -> None:
    """Remove channel from the degraded set (on recovery)."""
    with suppress(RedisError):
        redis = get_redis()
        await redis.srem(_DEGRADED_SET_KEY, str(channel_id))


async def is_degraded(channel_id: int) -> bool:
    """Check if a channel is currently degraded."""
    try:
        redis = get_redis()
        return bool(await redis.sismember(_DEGRADED_SET_KEY, str(channel_id)))
    except RedisError:
        return False


async def get_degraded_channel_ids() -> set[int]:
    """Get all currently degraded channel IDs.  Returns empty set on Redis failure."""
    try:
        redis = get_redis()
        members = await redis.smembers(_DEGRADED_SET_KEY)
        return {int(m) for m in members}
    except (RedisError, ValueError):
        return set()


# ---------------------------------------------------------------------------
# Channel state hash (probe tracking)
# ---------------------------------------------------------------------------


async def get_channel_state(channel_id: int) -> dict[str, Any]:
    """Get the health state hash for a channel."""
    try:
        redis = get_redis()
        data = await redis.hgetall(_state_key(channel_id))
        return dict(data) if data else {}
    except RedisError:
        return {}


async def update_channel_state(channel_id: int, **fields: Any) -> None:
    """Update fields in the channel state hash."""
    with suppress(RedisError):
        redis = get_redis()
        str_fields = {k: str(v) for k, v in fields.items()}
        await redis.hset(_state_key(channel_id), mapping=str_fields)


async def clear_channel_state(channel_id: int) -> None:
    """Clear all health state and buckets for a channel (on manual enable or recovery)."""
    with suppress(RedisError):
        redis = get_redis()
        # Delete state hash
        await redis.delete(_state_key(channel_id))
        # Clear recent buckets
        current_bucket = _current_minute_bucket()
        keys = [_bucket_key(channel_id, current_bucket - i) for i in range(10)]
        if keys:
            await redis.delete(*keys)


# ---------------------------------------------------------------------------
# Probe lock (prevent duplicate probes)
# ---------------------------------------------------------------------------


async def acquire_probe_lock(channel_id: int, ttl_seconds: int = 60) -> bool:
    """Try to acquire probe lock for a channel. Returns True if acquired."""
    try:
        redis = get_redis()
        result = await redis.set(
            _probe_lock_key(channel_id), "1", ex=ttl_seconds, nx=True
        )
        return result is not None
    except RedisError:
        return False


async def release_probe_lock(channel_id: int) -> None:
    """Release probe lock (on probe completion)."""
    with suppress(RedisError):
        redis = get_redis()
        await redis.delete(_probe_lock_key(channel_id))
