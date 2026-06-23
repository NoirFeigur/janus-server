"""Unit tests for src/gateway/finalize.py — unified gateway request finalizer."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from src.enums import UsageStatus
from src.gateway.context import GatewayRequestContext
from src.gateway.events import USAGE_QUEUE_KEY, LOG_QUEUE_KEY
from src.gateway.finalize import finalize_gateway_request
from tests._async_redis_double import AsyncRedisDouble


@pytest.mark.asyncio
async def test_finalize_enqueues_usage_event(fake_redis: AsyncRedisDouble) -> None:
    ctx = GatewayRequestContext(
        user_id=1,
        logical_model_id=10,
        logical_model_name="gpt-4",
        channel_id=5,
        upstream_model="gpt-4-0125",
        provider="openai",
        prompt_tokens=50,
        completion_tokens=25,
        total_tokens=75,
    )

    await finalize_gateway_request(ctx)

    assert await fake_redis.llen(USAGE_QUEUE_KEY) == 1
    raw = await fake_redis.lpop(USAGE_QUEUE_KEY)
    assert raw is not None
    payload = json.loads(raw)
    assert payload["user_id"] == 1
    assert payload["logical_model_id"] == 10
    assert payload["prompt_tokens"] == 50
    assert payload["completion_tokens"] == 25


@pytest.mark.asyncio
async def test_finalize_enqueues_log_event(fake_redis: AsyncRedisDouble) -> None:
    ctx = GatewayRequestContext(
        user_id=2,
        request_id="req-abc",
        logical_model_name="claude-3",
        provider="anthropic",
        http_status_code=200,
        stream=True,
    )

    await finalize_gateway_request(ctx)

    assert await fake_redis.llen(LOG_QUEUE_KEY) == 1
    raw = await fake_redis.lpop(LOG_QUEUE_KEY)
    assert raw is not None
    payload = json.loads(raw)
    assert payload["request_id"] == "req-abc"
    assert payload["provider"] == "anthropic"
    assert payload["stream"] is True


@pytest.mark.asyncio
async def test_finalize_computes_latency(fake_redis: AsyncRedisDouble) -> None:
    ctx = GatewayRequestContext(user_id=1)
    # latency_ms should be computed during finalize
    assert ctx.latency_ms is None
    await finalize_gateway_request(ctx)
    assert ctx.latency_ms is not None
    assert ctx.latency_ms >= 0


@pytest.mark.asyncio
async def test_finalize_error_context(fake_redis: AsyncRedisDouble) -> None:
    ctx = GatewayRequestContext(
        user_id=3,
        status=UsageStatus.error.value,
        http_status_code=502,
        error_code="upstream_error",
        error_body="Bad Gateway",
    )

    await finalize_gateway_request(ctx)

    raw = await fake_redis.lpop(LOG_QUEUE_KEY)
    assert raw is not None
    payload = json.loads(raw)
    assert payload["status_code"] == 502
    assert payload["error_code"] == "upstream_error"
    assert payload["error_body"] == "Bad Gateway"


@pytest.mark.asyncio
async def test_finalize_quota_settlement_skipped_without_service(
    fake_redis: AsyncRedisDouble,
) -> None:
    """Quota settlement skipped when service/model not provided (no error)."""
    ctx = GatewayRequestContext(user_id=1, quota_reserved=True)
    # Should not raise
    await finalize_gateway_request(ctx)
    assert ctx.quota_settled is False


@pytest.mark.asyncio
async def test_finalize_individual_step_failure_does_not_block_others(
    fake_redis: AsyncRedisDouble,
) -> None:
    """If usage enqueue fails, log enqueue should still succeed."""
    ctx = GatewayRequestContext(user_id=1, logical_model_name="gpt-4")

    with patch(
        "src.gateway.finalize.enqueue_usage_event",
        new_callable=AsyncMock,
        side_effect=RuntimeError("boom"),
    ):
        await finalize_gateway_request(ctx)

    # Log event should still be enqueued despite usage failure
    assert await fake_redis.llen(LOG_QUEUE_KEY) == 1
