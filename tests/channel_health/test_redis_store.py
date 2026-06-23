"""Unit tests for src/channel_health/redis_store.py — sliding window + degraded set."""

from __future__ import annotations

import pytest

from src.channel_health.redis_store import (
    acquire_probe_lock,
    add_to_degraded,
    clear_channel_state,
    get_channel_state,
    get_degraded_channel_ids,
    get_error_rate,
    is_degraded,
    record_request_outcome,
    release_probe_lock,
    remove_from_degraded,
    update_channel_state,
)
from tests._async_redis_double import AsyncRedisDouble


# ---------------------------------------------------------------------------
# Health recording
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_record_success(fake_redis: AsyncRedisDouble) -> None:
    await record_request_outcome(1, success=True, window_seconds=300)
    total, errors, rate = await get_error_rate(1, window_seconds=300)
    assert total == 1
    assert errors == 0
    assert rate == 0.0


@pytest.mark.asyncio
async def test_record_error(fake_redis: AsyncRedisDouble) -> None:
    await record_request_outcome(1, success=False, error_class="timeout", window_seconds=300)
    total, errors, rate = await get_error_rate(1, window_seconds=300)
    assert total == 1
    assert errors == 1
    assert rate == 1.0


@pytest.mark.asyncio
async def test_error_rate_mixed(fake_redis: AsyncRedisDouble) -> None:
    for _ in range(7):
        await record_request_outcome(2, success=True, window_seconds=300)
    for _ in range(3):
        await record_request_outcome(2, success=False, window_seconds=300)

    total, errors, rate = await get_error_rate(2, window_seconds=300)
    assert total == 10
    assert errors == 3
    assert abs(rate - 0.3) < 0.01


# ---------------------------------------------------------------------------
# Degraded set
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_to_degraded_first(fake_redis: AsyncRedisDouble) -> None:
    result = await add_to_degraded(100)
    assert result is True


@pytest.mark.asyncio
async def test_add_to_degraded_duplicate(fake_redis: AsyncRedisDouble) -> None:
    await add_to_degraded(100)
    result = await add_to_degraded(100)
    assert result is False


@pytest.mark.asyncio
async def test_remove_from_degraded(fake_redis: AsyncRedisDouble) -> None:
    await add_to_degraded(200)
    assert await is_degraded(200) is True
    await remove_from_degraded(200)
    assert await is_degraded(200) is False


@pytest.mark.asyncio
async def test_get_degraded_channel_ids(fake_redis: AsyncRedisDouble) -> None:
    await add_to_degraded(10)
    await add_to_degraded(20)
    await add_to_degraded(30)
    ids = await get_degraded_channel_ids()
    assert ids == {10, 20, 30}


@pytest.mark.asyncio
async def test_is_degraded_false(fake_redis: AsyncRedisDouble) -> None:
    assert await is_degraded(999) is False


# ---------------------------------------------------------------------------
# Channel state hash
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_and_get_channel_state(fake_redis: AsyncRedisDouble) -> None:
    await update_channel_state(5, status="degraded", error_rate="0.6", probe_failures="2")
    state = await get_channel_state(5)
    assert state["status"] == "degraded"
    assert state["error_rate"] == "0.6"
    assert state["probe_failures"] == "2"


@pytest.mark.asyncio
async def test_get_channel_state_empty(fake_redis: AsyncRedisDouble) -> None:
    state = await get_channel_state(999)
    assert state == {}


@pytest.mark.asyncio
async def test_clear_channel_state(fake_redis: AsyncRedisDouble) -> None:
    await update_channel_state(7, status="degraded")
    await clear_channel_state(7)
    state = await get_channel_state(7)
    assert state == {}


# ---------------------------------------------------------------------------
# Probe lock
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_acquire_probe_lock(fake_redis: AsyncRedisDouble) -> None:
    assert await acquire_probe_lock(1, ttl_seconds=60) is True
    # Second acquire should fail (lock held)
    assert await acquire_probe_lock(1, ttl_seconds=60) is False


@pytest.mark.asyncio
async def test_release_probe_lock(fake_redis: AsyncRedisDouble) -> None:
    await acquire_probe_lock(2, ttl_seconds=60)
    await release_probe_lock(2)
    # Should be able to acquire again
    assert await acquire_probe_lock(2, ttl_seconds=60) is True
