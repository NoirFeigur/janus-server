from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.db.models.gateway_observability import GatewayRequestLog
from src.db.models.usage import UsageRecord
from src.gateway.events import (
    LOG_DLQ_KEY,
    LOG_INFLIGHT_KEY,
    LOG_QUEUE_KEY,
    USAGE_DLQ_KEY,
    USAGE_INFLIGHT_KEY,
    USAGE_QUEUE_KEY,
    enqueue_log_event,
    enqueue_usage_event,
)
from src.gateway.observability import flush_gateway_logs
from src.gateway.usage_batch import flush_usage_records
from tests._async_redis_double import AsyncRedisDouble

pytestmark = pytest.mark.asyncio


def _usage_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "request_id": "usage-req-1",
        "user_id": 100,
        "api_key_id": None,
        "logical_model_id": 10,
        "channel_id": 20,
        "upstream_model": "gpt-4o",
        "prompt_tokens": 11,
        "completion_tokens": 7,
        "total_tokens": 18,
        "cost": "0.000123",
        "status": "success",
        "latency_ms": 42,
        "downgraded_features": None,
    }
    payload.update(overrides)
    return payload


def _log_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "request_id": "log-req-1",
        "user_id": 100,
        "api_key_id": None,
        "logical_model_id": 10,
        "model": "gpt-4o",
        "channel_id": 20,
        "upstream_model": "gpt-4o",
        "provider": "openai",
        "status_code": 200,
        "error_code": None,
        "error_body": None,
        "latency_ms": 42,
        "ttft_ms": None,
        "tokens_in": 11,
        "tokens_out": 7,
        "stream": False,
        "cache_hit": False,
        "retry_count": 0,
        "fallback_used": False,
    }
    payload.update(overrides)
    return payload


def _patch_unit_of_work(
    monkeypatch: pytest.MonkeyPatch,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from src.db.session import unit_of_work as real_unit_of_work

    @asynccontextmanager
    async def test_unit_of_work() -> AsyncIterator[AsyncSession]:
        async with real_unit_of_work(session_factory) as session:
            yield session

    monkeypatch.setattr("src.db.session.unit_of_work", test_unit_of_work)


async def test_flush_usage_records_acks_only_after_db_success(
    monkeypatch: pytest.MonkeyPatch,
    fake_redis: AsyncRedisDouble,
    gateway_session: AsyncSession,
    sqlite_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    _patch_unit_of_work(monkeypatch, sqlite_session_factory)
    await enqueue_usage_event(_usage_payload())

    written = await flush_usage_records({})

    assert written == 1
    assert await fake_redis.llen(USAGE_QUEUE_KEY) == 0
    assert await fake_redis.llen(USAGE_INFLIGHT_KEY) == 0
    row = await gateway_session.scalar(
        select(UsageRecord).where(UsageRecord.request_id == "usage-req-1")
    )
    assert row is not None
    assert row.total_tokens == 18


async def test_flush_usage_records_requeues_on_db_failure(
    monkeypatch: pytest.MonkeyPatch,
    fake_redis: AsyncRedisDouble,
) -> None:
    async def fail_bulk_insert(_records: list[dict[str, Any]]) -> int:
        raise RuntimeError("db down")

    monkeypatch.setattr("src.gateway.usage_batch._bulk_insert_usage", fail_bulk_insert)
    await enqueue_usage_event(_usage_payload())

    written = await flush_usage_records({})

    assert written == 0
    assert await fake_redis.llen(USAGE_INFLIGHT_KEY) == 0
    assert await fake_redis.llen(USAGE_DLQ_KEY) == 0
    raw = await fake_redis.lpop(USAGE_QUEUE_KEY)
    assert raw is not None
    assert json.loads(raw)["_attempt"] == 1


async def test_flush_usage_records_dlqs_after_max_retries(
    monkeypatch: pytest.MonkeyPatch,
    fake_redis: AsyncRedisDouble,
) -> None:
    async def fail_bulk_insert(_records: list[dict[str, Any]]) -> int:
        raise RuntimeError("db down")

    monkeypatch.setattr("src.gateway.usage_batch._bulk_insert_usage", fail_bulk_insert)
    await enqueue_usage_event(_usage_payload(_attempt=2))

    written = await flush_usage_records({})

    assert written == 0
    assert await fake_redis.llen(USAGE_QUEUE_KEY) == 0
    assert await fake_redis.llen(USAGE_INFLIGHT_KEY) == 0
    raw = await fake_redis.lpop(USAGE_DLQ_KEY)
    assert raw is not None
    assert json.loads(raw)["_attempt"] == 3


async def test_flush_usage_records_skips_duplicate_request_id_and_acks(
    monkeypatch: pytest.MonkeyPatch,
    fake_redis: AsyncRedisDouble,
    gateway_session: AsyncSession,
    sqlite_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    _patch_unit_of_work(monkeypatch, sqlite_session_factory)
    gateway_session.add(UsageRecord(**_usage_payload(request_id="dup-usage")))
    await gateway_session.commit()
    await enqueue_usage_event(_usage_payload(request_id="dup-usage"))

    written = await flush_usage_records({})

    assert written == 0
    assert await fake_redis.llen(USAGE_QUEUE_KEY) == 0
    assert await fake_redis.llen(USAGE_INFLIGHT_KEY) == 0
    count = await gateway_session.scalar(
        select(func.count()).select_from(UsageRecord).where(UsageRecord.request_id == "dup-usage")
    )
    assert count == 1


async def test_flush_usage_records_deduplicates_within_batch(
    monkeypatch: pytest.MonkeyPatch,
    fake_redis: AsyncRedisDouble,
    gateway_session: AsyncSession,
    sqlite_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    _patch_unit_of_work(monkeypatch, sqlite_session_factory)
    await enqueue_usage_event(_usage_payload(request_id="same-batch"))
    await enqueue_usage_event(_usage_payload(request_id="same-batch", total_tokens=99))

    written = await flush_usage_records({})

    assert written == 1
    assert await fake_redis.llen(USAGE_INFLIGHT_KEY) == 0
    count = await gateway_session.scalar(
        select(func.count()).select_from(UsageRecord).where(UsageRecord.request_id == "same-batch")
    )
    assert count == 1


async def test_usage_record_request_id_unique_constraint(
    gateway_session: AsyncSession,
) -> None:
    """Two usage rows with the same non-null request_id must be rejected by the
    DB unique index — the durable guard against double-billing (Oracle #1)."""
    gateway_session.add(UsageRecord(**_usage_payload(request_id="dup-unique")))
    await gateway_session.commit()

    gateway_session.add(UsageRecord(**_usage_payload(request_id="dup-unique")))
    with pytest.raises(IntegrityError):
        await gateway_session.commit()
    await gateway_session.rollback()


async def test_usage_record_request_id_allows_multiple_nulls(
    gateway_session: AsyncSession,
) -> None:
    """The unique index must still allow multiple NULL request_ids (NULLs are
    distinct), so rows without a correlation id are never blocked."""
    gateway_session.add(UsageRecord(**_usage_payload(request_id=None)))
    gateway_session.add(UsageRecord(**_usage_payload(request_id=None)))
    await gateway_session.commit()

    count = await gateway_session.scalar(
        select(func.count())
        .select_from(UsageRecord)
        .where(UsageRecord.request_id.is_(None))
    )
    assert count == 2


async def test_flush_usage_records_idempotent_across_processes(
    monkeypatch: pytest.MonkeyPatch,
    fake_redis: AsyncRedisDouble,
    gateway_session: AsyncSession,
    sqlite_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A second worker/recovery flush of an already-written request_id must be a
    no-op at the DB (ON CONFLICT DO NOTHING), not a duplicate insert nor a crash
    — the cross-process race guard (Oracle #1)."""
    _patch_unit_of_work(monkeypatch, sqlite_session_factory)
    gateway_session.add(UsageRecord(**_usage_payload(request_id="race-req")))
    await gateway_session.commit()

    await enqueue_usage_event(_usage_payload(request_id="race-req", total_tokens=999))
    written = await flush_usage_records({})

    assert written == 0
    assert await fake_redis.llen(USAGE_QUEUE_KEY) == 0
    assert await fake_redis.llen(USAGE_INFLIGHT_KEY) == 0
    rows = (
        await gateway_session.scalars(
            select(UsageRecord).where(UsageRecord.request_id == "race-req")
        )
    ).all()
    assert len(rows) == 1
    assert rows[0].total_tokens == 18  # original row untouched, not overwritten


async def test_flush_gateway_logs_acks_after_db_success(
    monkeypatch: pytest.MonkeyPatch,
    fake_redis: AsyncRedisDouble,
    gateway_session: AsyncSession,
    sqlite_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    _patch_unit_of_work(monkeypatch, sqlite_session_factory)
    await enqueue_log_event(_log_payload())

    written = await flush_gateway_logs({})

    assert written == 1
    assert await fake_redis.llen(LOG_QUEUE_KEY) == 0
    assert await fake_redis.llen(LOG_INFLIGHT_KEY) == 0
    row = await gateway_session.scalar(
        select(GatewayRequestLog).where(GatewayRequestLog.request_id == "log-req-1")
    )
    assert row is not None
    assert row.tokens_in == 11


async def test_flush_gateway_logs_requeues_on_db_failure(
    monkeypatch: pytest.MonkeyPatch,
    fake_redis: AsyncRedisDouble,
) -> None:
    async def fail_bulk_insert(_records: list[dict[str, Any]]) -> int:
        raise RuntimeError("db down")

    monkeypatch.setattr("src.gateway.observability._bulk_insert_logs", fail_bulk_insert)
    await enqueue_log_event(_log_payload())

    written = await flush_gateway_logs({})

    assert written == 0
    assert await fake_redis.llen(LOG_INFLIGHT_KEY) == 0
    assert await fake_redis.llen(LOG_DLQ_KEY) == 0
    raw = await fake_redis.lpop(LOG_QUEUE_KEY)
    assert raw is not None
    assert json.loads(raw)["_attempt"] == 1


async def test_flush_gateway_logs_deduplicates_within_batch(
    monkeypatch: pytest.MonkeyPatch,
    fake_redis: AsyncRedisDouble,
    gateway_session: AsyncSession,
    sqlite_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    _patch_unit_of_work(monkeypatch, sqlite_session_factory)
    await enqueue_log_event(_log_payload(request_id="same-log-batch"))
    await enqueue_log_event(_log_payload(request_id="same-log-batch", tokens_in=99))

    written = await flush_gateway_logs({})

    assert written == 1
    assert await fake_redis.llen(LOG_INFLIGHT_KEY) == 0
    count = await gateway_session.scalar(
        select(func.count())
        .select_from(GatewayRequestLog)
        .where(GatewayRequestLog.request_id == "same-log-batch")
    )
    assert count == 1


async def test_gateway_request_log_request_id_unique_constraint(
    gateway_session: AsyncSession,
) -> None:
    """Two log rows with the same request_id must be rejected by the DB unique
    index — the durable guard against a duplicate flush crashing on a non-atomic
    SELECT-then-INSERT race (Oracle #14)."""
    gateway_session.add(GatewayRequestLog(**_log_payload(request_id="dup-log-unique")))
    await gateway_session.commit()

    gateway_session.add(GatewayRequestLog(**_log_payload(request_id="dup-log-unique")))
    with pytest.raises(IntegrityError):
        await gateway_session.commit()
    await gateway_session.rollback()


async def test_flush_gateway_logs_idempotent_across_processes(
    monkeypatch: pytest.MonkeyPatch,
    fake_redis: AsyncRedisDouble,
    gateway_session: AsyncSession,
    sqlite_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A second worker/recovery flush of an already-written request_id must be a
    no-op at the DB (ON CONFLICT DO NOTHING), not a crash from the unique
    constraint nor a duplicate insert — the cross-process race guard (Oracle
    #14)."""
    _patch_unit_of_work(monkeypatch, sqlite_session_factory)
    gateway_session.add(GatewayRequestLog(**_log_payload(request_id="race-log")))
    await gateway_session.commit()

    await enqueue_log_event(_log_payload(request_id="race-log", tokens_in=999))
    written = await flush_gateway_logs({})

    assert written == 0
    assert await fake_redis.llen(LOG_QUEUE_KEY) == 0
    assert await fake_redis.llen(LOG_INFLIGHT_KEY) == 0
    rows = (
        await gateway_session.scalars(
            select(GatewayRequestLog).where(GatewayRequestLog.request_id == "race-log")
        )
    ).all()
    assert len(rows) == 1
    assert rows[0].tokens_in == 11  # original row untouched, not overwritten
