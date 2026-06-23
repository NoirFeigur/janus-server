"""Unit tests for src/channel_health/service.py — auto-degrade and recovery."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from src.channel_health.redis_store import (
    add_to_degraded,
    get_channel_state,
    get_degraded_channel_ids,
    is_degraded,
    record_request_outcome,
)
from src.channel_health.service import ChannelHealthService
from tests._async_redis_double import AsyncRedisDouble


@pytest.fixture
def health_service() -> ChannelHealthService:
    """ChannelHealthService with low thresholds for testing."""
    svc = ChannelHealthService.__new__(ChannelHealthService)
    svc.window_seconds = 300
    svc.min_requests = 5
    svc.error_threshold = 0.5
    svc.max_probe_failures = 3
    svc.probe_interval_seconds = 60
    return svc


# ---------------------------------------------------------------------------
# Auto-degrade
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_record_and_evaluate_success_no_degrade(
    fake_redis: AsyncRedisDouble, health_service: ChannelHealthService
) -> None:
    """Success outcomes never trigger degradation."""
    await health_service.record_and_evaluate(1, success=True)
    assert await is_degraded(1) is False


@pytest.mark.asyncio
async def test_record_and_evaluate_below_min_requests(
    fake_redis: AsyncRedisDouble, health_service: ChannelHealthService
) -> None:
    """Below min_requests threshold — don't degrade even with 100% errors."""
    for _ in range(4):  # min_requests=5, only 4
        await health_service.record_and_evaluate(2, success=False, error_class="timeout")
    assert await is_degraded(2) is False


@pytest.mark.asyncio
async def test_record_and_evaluate_triggers_degrade(
    fake_redis: AsyncRedisDouble, health_service: ChannelHealthService
) -> None:
    """When error threshold is breached with enough requests, channel is degraded."""
    # 5 errors out of 5 = 100% error rate (above 50% threshold)
    for _ in range(5):
        await health_service.record_and_evaluate(3, success=False, error_class="502")

    assert await is_degraded(3) is True
    state = await get_channel_state(3)
    assert state.get("status") == "degraded"


@pytest.mark.asyncio
async def test_record_and_evaluate_below_threshold(
    fake_redis: AsyncRedisDouble, health_service: ChannelHealthService
) -> None:
    """Error rate below threshold — don't degrade."""
    # 8 success + 2 errors = 20% error rate (below 50%)
    for _ in range(8):
        await health_service.record_and_evaluate(4, success=True)
    for _ in range(2):
        await health_service.record_and_evaluate(4, success=False)
    assert await is_degraded(4) is False


# ---------------------------------------------------------------------------
# Recovery
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_record_probe_success_recovers(
    fake_redis: AsyncRedisDouble, health_service: ChannelHealthService
) -> None:
    """Successful probe removes channel from degraded set."""
    await add_to_degraded(10)
    assert await is_degraded(10) is True

    await health_service.record_probe_success(10)
    assert await is_degraded(10) is False
    # State should be cleared
    state = await get_channel_state(10)
    assert state == {}


# ---------------------------------------------------------------------------
# Probe failure escalation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_record_probe_failure_increments(
    fake_redis: AsyncRedisDouble, health_service: ChannelHealthService
) -> None:
    """Probe failure increments counter in state."""
    should_disable = await health_service.record_probe_failure(20, current_failures=0)
    assert should_disable is False
    state = await get_channel_state(20)
    assert state["probe_failures"] == "1"


@pytest.mark.asyncio
async def test_record_probe_failure_reaches_hard_disable(
    fake_redis: AsyncRedisDouble, health_service: ChannelHealthService
) -> None:
    """After max_probe_failures, returns True (hard-disable signal)."""
    # max_probe_failures=3, current_failures=2 → new_count=3 → should_disable
    should_disable = await health_service.record_probe_failure(30, current_failures=2)
    assert should_disable is True
