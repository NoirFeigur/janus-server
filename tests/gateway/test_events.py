"""Unit tests for src/gateway/events.py — durable Redis event queue."""

from __future__ import annotations

import json

import pytest

from src.gateway.events import (
    DEFAULT_BATCH_SIZE,
    LOG_DLQ_KEY,
    LOG_QUEUE_KEY,
    USAGE_DLQ_KEY,
    USAGE_QUEUE_KEY,
    enqueue_event,
    enqueue_log_event,
    enqueue_usage_event,
    flush_dlq,
    get_queue_length,
    peek_dlq,
    pop_batch,
    send_to_dlq,
)
from tests._async_redis_double import AsyncRedisDouble


@pytest.mark.asyncio
async def test_enqueue_event_success(fake_redis: AsyncRedisDouble) -> None:
    payload = {"user_id": 1, "model": "gpt-4"}
    result = await enqueue_event(USAGE_QUEUE_KEY, payload)
    assert result is True
    assert await fake_redis.llen(USAGE_QUEUE_KEY) == 1


@pytest.mark.asyncio
async def test_enqueue_usage_event(fake_redis: AsyncRedisDouble) -> None:
    payload = {"user_id": 42, "tokens": 100}
    assert await enqueue_usage_event(payload) is True
    raw = await fake_redis.lpop(USAGE_QUEUE_KEY)
    assert raw is not None
    assert json.loads(raw)["user_id"] == 42


@pytest.mark.asyncio
async def test_enqueue_log_event(fake_redis: AsyncRedisDouble) -> None:
    payload = {"request_id": "abc123", "status_code": 200}
    assert await enqueue_log_event(payload) is True
    raw = await fake_redis.lpop(LOG_QUEUE_KEY)
    assert raw is not None
    assert json.loads(raw)["request_id"] == "abc123"


@pytest.mark.asyncio
async def test_pop_batch_returns_items_in_order(fake_redis: AsyncRedisDouble) -> None:
    for i in range(5):
        await enqueue_event(USAGE_QUEUE_KEY, {"seq": i})

    items = await pop_batch(USAGE_QUEUE_KEY, batch_size=3)
    assert len(items) == 3
    assert json.loads(items[0])["seq"] == 0
    assert json.loads(items[2])["seq"] == 2

    # 2 remaining
    remaining = await pop_batch(USAGE_QUEUE_KEY, batch_size=10)
    assert len(remaining) == 2


@pytest.mark.asyncio
async def test_pop_batch_empty_queue(fake_redis: AsyncRedisDouble) -> None:
    items = await pop_batch(USAGE_QUEUE_KEY, batch_size=10)
    assert items == []


@pytest.mark.asyncio
async def test_send_to_dlq(fake_redis: AsyncRedisDouble) -> None:
    failed = ['{"bad": true}', '{"also_bad": true}']
    await send_to_dlq(USAGE_DLQ_KEY, failed)
    assert await fake_redis.llen(USAGE_DLQ_KEY) == 2


@pytest.mark.asyncio
async def test_get_queue_length(fake_redis: AsyncRedisDouble) -> None:
    assert await get_queue_length(USAGE_QUEUE_KEY) == 0
    await enqueue_event(USAGE_QUEUE_KEY, {"x": 1})
    await enqueue_event(USAGE_QUEUE_KEY, {"x": 2})
    assert await get_queue_length(USAGE_QUEUE_KEY) == 2


@pytest.mark.asyncio
async def test_peek_dlq(fake_redis: AsyncRedisDouble) -> None:
    items = [json.dumps({"err": i}) for i in range(3)]
    await send_to_dlq(LOG_DLQ_KEY, items)

    peeked = await peek_dlq(LOG_DLQ_KEY, count=2)
    assert len(peeked) == 2
    assert peeked[0]["err"] == 0
    # Original items still in DLQ
    assert await fake_redis.llen(LOG_DLQ_KEY) == 3


@pytest.mark.asyncio
async def test_flush_dlq(fake_redis: AsyncRedisDouble) -> None:
    items = [json.dumps({"x": i}) for i in range(4)]
    await send_to_dlq(USAGE_DLQ_KEY, items)

    count = await flush_dlq(USAGE_DLQ_KEY)
    assert count == 4
    assert await fake_redis.llen(USAGE_DLQ_KEY) == 0
