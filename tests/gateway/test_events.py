"""Unit tests for src/gateway/events.py — durable Redis event queue."""

from __future__ import annotations

import json

import pytest

from src.gateway.events import (
    LOG_DLQ_KEY,
    LOG_INFLIGHT_KEY,
    LOG_QUEUE_KEY,
    USAGE_DLQ_KEY,
    USAGE_INFLIGHT_KEY,
    USAGE_QUEUE_KEY,
    ack_claimed,
    claim_batch,
    enqueue_event,
    enqueue_log_event,
    enqueue_usage_event,
    flush_dlq,
    get_queue_length,
    peek_dlq,
    recover_stale_claims,
    retry_or_dlq_claimed,
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
async def test_claim_batch_moves_items_to_inflight(fake_redis: AsyncRedisDouble) -> None:
    for i in range(5):
        await enqueue_event(USAGE_QUEUE_KEY, {"seq": i})

    items = await claim_batch(USAGE_QUEUE_KEY, USAGE_INFLIGHT_KEY, batch_size=3)
    assert len(items) == 3
    assert json.loads(items[0].raw)["seq"] == 0
    assert json.loads(items[2].raw)["seq"] == 2
    assert await fake_redis.llen(USAGE_QUEUE_KEY) == 2
    assert await fake_redis.llen(USAGE_INFLIGHT_KEY) == 3

    acked = await ack_claimed(USAGE_INFLIGHT_KEY, items[:2])
    assert acked == 2
    assert await fake_redis.llen(USAGE_INFLIGHT_KEY) == 1


@pytest.mark.asyncio
async def test_claim_batch_empty_queue(fake_redis: AsyncRedisDouble) -> None:
    items = await claim_batch(USAGE_QUEUE_KEY, USAGE_INFLIGHT_KEY, batch_size=10)
    assert items == []


@pytest.mark.asyncio
async def test_send_to_dlq(fake_redis: AsyncRedisDouble) -> None:
    failed = ['{"bad": true}', '{"also_bad": true}']
    await send_to_dlq(USAGE_DLQ_KEY, failed)
    assert await fake_redis.llen(USAGE_DLQ_KEY) == 2


@pytest.mark.asyncio
async def test_retry_claimed_requeues_then_dlqs(fake_redis: AsyncRedisDouble) -> None:
    await enqueue_event(USAGE_QUEUE_KEY, {"request_id": "req-1"})
    items = await claim_batch(USAGE_QUEUE_KEY, USAGE_INFLIGHT_KEY, batch_size=1)

    await retry_or_dlq_claimed(
        queue_key=USAGE_QUEUE_KEY,
        inflight_key=USAGE_INFLIGHT_KEY,
        dlq_key=USAGE_DLQ_KEY,
        items=items,
        max_attempts=2,
    )

    assert await fake_redis.llen(USAGE_INFLIGHT_KEY) == 0
    assert await fake_redis.llen(USAGE_QUEUE_KEY) == 1
    retried = await fake_redis.lpop(USAGE_QUEUE_KEY)
    assert retried is not None
    assert json.loads(retried)["_attempt"] == 1
    await fake_redis.rpush(USAGE_INFLIGHT_KEY, items[0].envelope)
    await retry_or_dlq_claimed(
        queue_key=USAGE_QUEUE_KEY,
        inflight_key=USAGE_INFLIGHT_KEY,
        dlq_key=USAGE_DLQ_KEY,
        items=[type(items[0])(raw=retried, envelope=items[0].envelope)],
        max_attempts=2,
    )

    assert await fake_redis.llen(USAGE_QUEUE_KEY) == 0
    assert await fake_redis.llen(USAGE_DLQ_KEY) == 1
    dlq = await fake_redis.lpop(USAGE_DLQ_KEY)
    assert dlq is not None
    assert json.loads(dlq)["_attempt"] == 2


@pytest.mark.asyncio
async def test_recover_stale_claims_requeues_inflight(
    fake_redis: AsyncRedisDouble,
) -> None:
    payload = json.dumps({"request_id": "stale"})
    envelope = json.dumps({"payload": payload, "claimed_at_ms": 1})
    await fake_redis.rpush(LOG_INFLIGHT_KEY, envelope)

    recovered = await recover_stale_claims(
        queue_key=LOG_QUEUE_KEY,
        inflight_key=LOG_INFLIGHT_KEY,
        dlq_key=LOG_DLQ_KEY,
        stale_after_seconds=1,
    )

    assert recovered == 1
    assert await fake_redis.llen(LOG_INFLIGHT_KEY) == 0
    raw = await fake_redis.lpop(LOG_QUEUE_KEY)
    assert raw == payload


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


# ---------------------------------------------------------------------------
# #8: local outbox fallback — a billing event must not be silently dropped
# when Redis is momentarily unreachable during enqueue.
# ---------------------------------------------------------------------------


class _FailingRedis:
    """A Redis double whose rpush always raises (simulates Redis down)."""

    def __init__(self) -> None:
        self.rpush_calls = 0

    async def rpush(self, key: str, *values: str) -> int:
        self.rpush_calls += 1
        raise ConnectionError("redis unreachable")


@pytest.mark.asyncio
async def test_enqueue_buffers_to_outbox_when_redis_down(
    fake_redis: AsyncRedisDouble, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Oracle #8: a usage event must survive a transient Redis outage.

    The old enqueue logged + dropped the event on any Redis error, silently
    losing billing data (lost revenue). It must instead buffer the event in a
    bounded process-local outbox so it can be replayed once Redis recovers."""
    from src.gateway.events import _outbox_size, reset_outbox

    reset_outbox()
    failing = _FailingRedis()
    monkeypatch.setattr("src.gateway.events.get_redis", lambda: failing)

    result = await enqueue_usage_event({"user_id": 7, "tokens": 100})

    # Enqueue reports failure (caller already responded), but the event is NOT
    # lost — it is parked in the outbox for replay.
    assert result is False
    assert failing.rpush_calls >= 1
    assert _outbox_size(USAGE_QUEUE_KEY) == 1


@pytest.mark.asyncio
async def test_outbox_flushed_to_redis_on_next_successful_enqueue(
    fake_redis: AsyncRedisDouble, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Once Redis recovers, the buffered event is replayed ahead of new ones."""
    from src.gateway.events import _outbox_size, reset_outbox

    reset_outbox()
    failing = _FailingRedis()
    monkeypatch.setattr("src.gateway.events.get_redis", lambda: failing)
    await enqueue_usage_event({"user_id": 7, "seq": 1})
    assert _outbox_size(USAGE_QUEUE_KEY) == 1

    # Redis recovers — the autouse fake_redis is the live client again.
    monkeypatch.setattr("src.gateway.events.get_redis", lambda: fake_redis)
    result = await enqueue_usage_event({"user_id": 7, "seq": 2})

    assert result is True
    assert _outbox_size(USAGE_QUEUE_KEY) == 0
    # Both the replayed (seq=1) and the new (seq=2) event landed in Redis.
    assert await fake_redis.llen(USAGE_QUEUE_KEY) == 2
    first = json.loads(await fake_redis.lpop(USAGE_QUEUE_KEY))
    assert first["seq"] == 1


@pytest.mark.asyncio
async def test_outbox_is_bounded(
    fake_redis: AsyncRedisDouble, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The outbox is bounded so a prolonged outage cannot exhaust memory —
    oldest buffered events are dropped once the cap is reached."""
    from src.gateway.events import _OUTBOX_MAX_PER_QUEUE, _outbox_size, reset_outbox

    reset_outbox()
    failing = _FailingRedis()
    monkeypatch.setattr("src.gateway.events.get_redis", lambda: failing)

    for i in range(_OUTBOX_MAX_PER_QUEUE + 50):
        await enqueue_usage_event({"seq": i})

    assert _outbox_size(USAGE_QUEUE_KEY) == _OUTBOX_MAX_PER_QUEUE


# ---------------------------------------------------------------------------
# #13: recover_stale_claims must be atomic. Two concurrent recover passes (e.g.
# two ARQ workers) must not BOTH requeue the same stale envelope — that
# double-processes the event (double billing / duplicate log row).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_recover_does_not_double_requeue(
    fake_redis: AsyncRedisDouble, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Oracle #13: a single stale claim recovered by two concurrent passes must
    land on the pending queue exactly once.

    The old implementation did a non-atomic LRANGE snapshot followed by
    per-envelope RPUSH+LREM. Two passes that both snapshot the same envelope
    before either removes it each requeue it — duplicate re-enqueue. To force
    that interleaving deterministically, ``lrange`` is made to yield control."""
    import asyncio

    payload = json.dumps({"request_id": "stale-1"})
    envelope = json.dumps({"payload": payload, "claimed_at_ms": 1})
    await fake_redis.rpush(LOG_INFLIGHT_KEY, envelope)

    original_lrange = fake_redis.lrange

    async def _yielding_lrange(key: str, start: int, stop: int) -> list[str]:
        # Snapshot FIRST, then yield, so a second concurrent recover pass takes
        # the SAME snapshot before either requeues — the exact window the old
        # non-atomic code raced in (both see the envelope, both requeue it).
        result = await original_lrange(key, start, stop)
        await asyncio.sleep(0)
        return result

    monkeypatch.setattr(fake_redis, "lrange", _yielding_lrange)

    async def _recover() -> int:
        return await recover_stale_claims(
            queue_key=LOG_QUEUE_KEY,
            inflight_key=LOG_INFLIGHT_KEY,
            dlq_key=LOG_DLQ_KEY,
            stale_after_seconds=1,
        )

    results = await asyncio.gather(_recover(), _recover())

    # The stale claim is requeued exactly once across both passes.
    assert await fake_redis.llen(LOG_QUEUE_KEY) == 1
    assert sum(results) == 1
    assert await fake_redis.llen(LOG_INFLIGHT_KEY) == 0
