"""Batch writer for gateway request log events (P1 observability).

Same pattern as usage_batch: ARQ cron claims Redis log events into inflight,
bulk-inserts GatewayRequestLog rows, and ACKs only after DB commit. Failed
writes are retried before DLQ.
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select

from src.core.logging import get_logger
from src.gateway.events import (
    DEFAULT_BATCH_SIZE,
    LOG_DLQ_KEY,
    LOG_INFLIGHT_KEY,
    LOG_QUEUE_KEY,
    ClaimedEvent,
    ack_claimed,
    claim_batch,
    dlq_claimed,
    recover_stale_claims,
    retry_or_dlq_claimed,
)

_log = get_logger(__name__)


async def flush_gateway_logs(ctx: dict[str, Any]) -> int:
    """ARQ task: claim log events from Redis queue and bulk-insert to DB.

    Returns the number of records successfully written.
    """
    await recover_stale_claims(
        queue_key=LOG_QUEUE_KEY,
        inflight_key=LOG_INFLIGHT_KEY,
        dlq_key=LOG_DLQ_KEY,
    )
    claimed = await claim_batch(LOG_QUEUE_KEY, LOG_INFLIGHT_KEY, DEFAULT_BATCH_SIZE)
    if not claimed:
        return 0

    records: list[dict[str, Any]] = []
    valid_claims: list[ClaimedEvent] = []
    unparseable: list[ClaimedEvent] = []
    for item in claimed:
        try:
            record = json.loads(item.raw)
            if not isinstance(record, dict):
                raise TypeError("log event must be a JSON object")
            records.append(record)
            valid_claims.append(item)
        except (json.JSONDecodeError, TypeError):
            unparseable.append(item)

    if unparseable:
        await dlq_claimed(LOG_INFLIGHT_KEY, LOG_DLQ_KEY, unparseable)
        _log.warning("gateway.log_batch.unparseable", count=len(unparseable))

    if not records:
        return 0

    try:
        written = await _bulk_insert_logs(records)
    except Exception:
        _log.exception("gateway.log_batch.db_write_failed", count=len(records))
        await retry_or_dlq_claimed(
            queue_key=LOG_QUEUE_KEY,
            inflight_key=LOG_INFLIGHT_KEY,
            dlq_key=LOG_DLQ_KEY,
            items=valid_claims,
        )
        return 0

    await ack_claimed(LOG_INFLIGHT_KEY, valid_claims)
    return written


async def _bulk_insert_logs(records: list[dict[str, Any]]) -> int:
    """Bulk-insert gateway request log records."""
    from src.db.models.gateway_observability import GatewayRequestLog
    from src.db.session import unit_of_work

    async with unit_of_work() as session:
        request_ids = [str(r["request_id"]) for r in records if r.get("request_id")]
        existing_request_ids: set[str] = set()
        if request_ids:
            result = await session.execute(
                select(GatewayRequestLog.request_id).where(
                    GatewayRequestLog.request_id.in_(request_ids)
                )
            )
            existing_request_ids = {str(value) for value in result.scalars().all()}

        seen_request_ids: set[str] = set()
        new_records: list[dict[str, Any]] = []
        for record in records:
            request_id = record.get("request_id")
            if not request_id:
                new_records.append(record)
                continue
            request_id = str(request_id)
            if request_id in existing_request_ids or request_id in seen_request_ids:
                continue
            seen_request_ids.add(request_id)
            new_records.append(record)
        orm_records = [_to_log_record(r) for r in new_records]
        session.add_all(orm_records)

    skipped = len(records) - len(new_records)
    if skipped:
        _log.info(
            "gateway.log_batch.duplicates_skipped",
            count=skipped,
        )
    _log.debug("gateway.log_batch.flushed", count=len(new_records))
    return len(new_records)


def _to_log_record(data: dict[str, Any]) -> Any:
    """Convert a queue payload dict to a GatewayRequestLog ORM instance."""
    from src.db.models.gateway_observability import GatewayRequestLog

    return GatewayRequestLog(
        request_id=data["request_id"],
        user_id=data.get("user_id"),
        api_key_id=data.get("api_key_id"),
        logical_model_id=data.get("logical_model_id"),
        model=data.get("model"),
        channel_id=data.get("channel_id"),
        upstream_model=data.get("upstream_model"),
        provider=data.get("provider"),
        status_code=data.get("status_code", 200),
        error_code=data.get("error_code"),
        error_body=data.get("error_body"),
        latency_ms=data.get("latency_ms"),
        ttft_ms=data.get("ttft_ms"),
        tokens_in=data.get("tokens_in", 0),
        tokens_out=data.get("tokens_out", 0),
        stream=data.get("stream", False),
        cache_hit=data.get("cache_hit", False),
        retry_count=data.get("retry_count", 0),
        fallback_used=data.get("fallback_used", False),
    )
