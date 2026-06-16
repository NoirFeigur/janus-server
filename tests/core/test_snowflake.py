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
