"""Snowflake ID generator (data-model §0.2).

64-bit layout: 1 sign bit (always 0) | 41-bit ms timestamp | 10-bit worker-id | 12-bit sequence.

The worker-id is leased from Redis per replica in production (fail-fast if it
cannot be obtained — §0.2). For local single-process development it defaults to
0. ``set_worker_id()`` is called once at startup after a lease is acquired.

Thread-safe: ``next_id()`` may be invoked from ``asyncio.to_thread`` or multiple
worker threads.

Clock rollback is **tiered** (never silently reuses a timestamp, but also never
hangs the whole generator on a large jump):

- **Small rollback** (``≤ _MAX_ROLLBACK_WAIT_MS``, e.g. a sub-second NTP nudge):
  briefly sleep and re-read the clock until it catches back up to ``_last_ms``.
  Bounded by the threshold, so the wait can never exceed it.
- **Large rollback** (``> _MAX_ROLLBACK_WAIT_MS``, e.g. a manual clock set or a
  multi-second NTP correction): raise :class:`ClockRollbackError` immediately
  rather than block the generator (and every caller behind ``_lock``) for the
  full rollback duration. A large backwards jump signals a misconfigured host;
  failing fast surfaces it instead of stalling id minting for seconds/minutes.
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

# Largest backwards clock jump we will absorb by waiting. A drift up to this many
# ms is treated as a transient NTP nudge (bounded busy-wait to catch up); beyond
# it we fail fast rather than stall every id caller behind ``_lock``.
_MAX_ROLLBACK_WAIT_MS = 1000

_lock = threading.Lock()
_worker_id = 0
_last_ms = -1
_sequence = 0


class ClockRollbackError(RuntimeError):
    """Raised when the wall clock jumps backwards more than ``_MAX_ROLLBACK_WAIT_MS``.

    A large backwards jump (manual clock set, big NTP correction) cannot be safely
    waited out without hanging the generator, and minting under a rolled-back
    timestamp would risk duplicate ids. Surfacing the misconfiguration is the only
    safe option.
    """


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
    """Generate a new snowflake id. Thread-safe.

    Raises :class:`ClockRollbackError` if the wall clock has jumped backwards by
    more than ``_MAX_ROLLBACK_WAIT_MS`` (a small rollback is waited out instead).
    """
    global _last_ms, _sequence
    with _lock:
        now = _now_ms()
        if now < _last_ms:
            rollback_ms = _last_ms - now
            if rollback_ms > _MAX_ROLLBACK_WAIT_MS:
                # Too large to wait out — failing fast beats hanging every caller
                # behind ``_lock`` for the full rollback (and never minting under
                # a rolled-back timestamp).
                raise ClockRollbackError(
                    f"clock moved backwards {rollback_ms}ms "
                    f"(> {_MAX_ROLLBACK_WAIT_MS}ms); refusing to mint"
                )
            # Small rollback (transient NTP nudge): wait — bounded by the
            # threshold — until the clock catches back up to _last_ms. Sleep
            # instead of busy-spinning so we do not peg a CPU while holding the
            # lock; 1ms granularity matches the timestamp resolution.
            while now < _last_ms:
                time.sleep(0.001)
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
