"""Tests for the snowflake id generator (src/core/snowflake.py)."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from src.core import snowflake


@pytest.fixture(autouse=True)
def reset_snowflake_state() -> Iterator[None]:
    """Restore module globals after each test (the module is process-global)."""
    saved = (snowflake._worker_id, snowflake._last_ms, snowflake._sequence)
    yield
    snowflake._worker_id, snowflake._last_ms, snowflake._sequence = saved


def test_next_id_is_positive_64bit() -> None:
    value = snowflake.next_id()
    assert value > 0
    assert value < (1 << 63)  # fits in a signed BigInteger


def test_next_id_unique_across_many_calls() -> None:
    ids = [snowflake.next_id() for _ in range(10_000)]
    assert len(set(ids)) == len(ids)  # no collisions


def test_next_id_monotonically_increasing() -> None:
    ids = [snowflake.next_id() for _ in range(1_000)]
    assert ids == sorted(ids)  # time-ordered, strictly increasing within a process


def test_worker_id_is_encoded_in_the_id() -> None:
    snowflake.set_worker_id(42)
    value = snowflake.next_id()
    extracted = (value >> snowflake._WORKER_ID_SHIFT) & snowflake._MAX_WORKER_ID
    assert extracted == 42


def test_set_worker_id_rejects_out_of_range() -> None:
    with pytest.raises(ValueError):
        snowflake.set_worker_id(-1)
    with pytest.raises(ValueError):
        snowflake.set_worker_id(snowflake._MAX_WORKER_ID + 1)


def test_set_worker_id_accepts_boundaries() -> None:
    snowflake.set_worker_id(0)
    snowflake.set_worker_id(snowflake._MAX_WORKER_ID)  # must not raise


def test_sequence_rollover_advances_to_next_ms(monkeypatch: pytest.MonkeyPatch) -> None:
    """When the sequence is exhausted within one ms, the clock must advance."""
    clock = {"ms": 1_000_000_000_000}

    def fake_now_ms() -> int:
        return clock["ms"]

    monkeypatch.setattr(snowflake, "_now_ms", fake_now_ms)
    snowflake._last_ms = -1
    snowflake._sequence = 0

    # Exhaust the 12-bit sequence within the same frozen millisecond...
    first_batch = [snowflake.next_id() for _ in range(snowflake._MAX_SEQUENCE + 1)]
    assert len(set(first_batch)) == len(first_batch)

    # ...the next call would overflow the sequence, so the generator must spin
    # until the clock moves forward. Advance it on the next read.
    original = clock["ms"]

    calls = {"n": 0}

    def advancing_now_ms() -> int:
        calls["n"] += 1
        # First read returns the same ms (forces the spin), then time moves on.
        return original if calls["n"] < 2 else original + 1

    monkeypatch.setattr(snowflake, "_now_ms", advancing_now_ms)
    next_value = snowflake.next_id()
    extracted_ms = (next_value >> snowflake._TIMESTAMP_SHIFT) + snowflake._EPOCH_MS
    assert extracted_ms == original + 1


def test_clock_rollback_spins_until_caught_up(monkeypatch: pytest.MonkeyPatch) -> None:
    """If the wall clock moves backwards, next_id spins until it catches up
    (never silently reuses a timestamp → never risks a duplicate id)."""
    base = 2_000_000_000_000
    snowflake._last_ms = base
    snowflake._sequence = 0

    # First read is *behind* _last_ms (rollback), then it catches up.
    readings = iter([base - 5, base - 2, base])

    def rolling_now_ms() -> int:
        try:
            return next(readings)
        except StopIteration:
            return base

    monkeypatch.setattr(snowflake, "_now_ms", rolling_now_ms)
    value = snowflake.next_id()
    # Caught up to base (== _last_ms) → same-ms path → sequence advances to 1.
    extracted_ms = (value >> snowflake._TIMESTAMP_SHIFT) + snowflake._EPOCH_MS
    assert extracted_ms == base
    assert value & snowflake._MAX_SEQUENCE == 1


def test_small_clock_rollback_within_threshold_waits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rollback at the threshold boundary is waited out, not raised.

    A drift exactly equal to ``_MAX_ROLLBACK_WAIT_MS`` is the largest jump we
    still absorb by waiting; it must catch up and mint an id, never raise. The
    real ``time.sleep(0.001)`` in the wait loop is harmless here (one short nap).
    """
    base = 3_000_000_000_000
    snowflake._last_ms = base
    snowflake._sequence = 0
    rollback = snowflake._MAX_ROLLBACK_WAIT_MS  # exactly at the boundary

    # Behind by the full threshold once, then the clock catches up to base.
    readings = iter([base - rollback, base])

    def rolling_now_ms() -> int:
        try:
            return next(readings)
        except StopIteration:
            return base

    monkeypatch.setattr(snowflake, "_now_ms", rolling_now_ms)
    value = snowflake.next_id()
    extracted_ms = (value >> snowflake._TIMESTAMP_SHIFT) + snowflake._EPOCH_MS
    assert extracted_ms == base  # caught up, minted under the recovered clock


def test_large_clock_rollback_fails_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    """A rollback larger than the threshold raises instead of hanging.

    A multi-second backwards jump (manual clock set / big NTP correction) cannot
    be waited out without stalling every caller behind ``_lock`` for the full
    rollback. ``next_id`` must raise ``ClockRollbackError`` immediately so the
    misconfiguration surfaces rather than freezing id minting.
    """
    base = 4_000_000_000_000
    snowflake._last_ms = base
    snowflake._sequence = 0
    # One ms past the threshold → fail-fast territory.
    rolled_back = base - (snowflake._MAX_ROLLBACK_WAIT_MS + 1)

    monkeypatch.setattr(snowflake, "_now_ms", lambda: rolled_back)
    with pytest.raises(snowflake.ClockRollbackError):
        snowflake.next_id()
    # _last_ms must be untouched — we refused to mint, not advanced the clock.
    assert snowflake._last_ms == base
