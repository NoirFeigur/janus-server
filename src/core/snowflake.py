"""Snowflake ID generator (data-model §0.2).

64-bit layout: 1 sign bit (always 0) | 41-bit ms timestamp | 10-bit worker-id | 12-bit sequence.

The worker-id is leased from Redis per replica in production (fail-fast if it
cannot be obtained — §0.2). For local single-process development it defaults to
0. ``set_worker_id()`` is called once at startup after a lease is acquired.

Thread-safe: ``next_id()`` may be invoked from ``asyncio.to_thread`` or multiple
worker threads. Clock rollback is handled by spinning until the clock catches up
(never silently reuses a timestamp).
"""

from __future__ import annotations

import threading
import time

# Custom epoch: 2025-01-01T00:00:00Z in ms. Keeps the 41-bit timestamp field
# from overflowing until ~2094 and yields smaller, time-ordered ids.
_EPOCH_MS = 1735689600000

_WORKER_ID_BITS = 10
_SEQUENCE_BITS = 12

_MAX_WORKER_ID = (1 << _WORKER_ID_BITS) - 1  # 1023
_MAX_SEQUENCE = (1 << _SEQUENCE_BITS) - 1  # 4095

_WORKER_ID_SHIFT = _SEQUENCE_BITS  # 12
_TIMESTAMP_SHIFT = _SEQUENCE_BITS + _WORKER_ID_BITS  # 22

_lock = threading.Lock()
_worker_id = 0
_last_ms = -1
_sequence = 0


def set_worker_id(worker_id: int) -> None:
    """Pin the worker-id for this process. Call once at startup."""
    if not 0 <= worker_id <= _MAX_WORKER_ID:
        raise ValueError(f"worker_id must be in 0..{_MAX_WORKER_ID}, got {worker_id}")
    global _worker_id
    with _lock:
        _worker_id = worker_id


def _now_ms() -> int:
    return int(time.time() * 1000)


def next_id() -> int:
    """Generate a new snowflake id. Thread-safe."""
    global _last_ms, _sequence
    with _lock:
        now = _now_ms()
        if now < _last_ms:
            # Clock moved backwards — wait until it catches up rather than
            # risk a duplicate id.
            while now < _last_ms:
                now = _now_ms()
        if now == _last_ms:
            _sequence = (_sequence + 1) & _MAX_SEQUENCE
            if _sequence == 0:
                # Sequence exhausted this ms — spin to the next ms.
                while now <= _last_ms:
                    now = _now_ms()
        else:
            _sequence = 0
        _last_ms = now
        return (
            ((now - _EPOCH_MS) << _TIMESTAMP_SHIFT)
            | (_worker_id << _WORKER_ID_SHIFT)
            | _sequence
        )
