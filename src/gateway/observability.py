"""Batch writer for gateway request log events (P1 observability).

Same pattern as usage_batch: ARQ cron claims Redis log events into inflight,
bulk-inserts GatewayRequestLog rows, and ACKs only after DB commit. Failed
writes are retried before DLQ.
"""

from __future__ import annotations

import json
from typing import Any

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
    """Bulk-insert gateway request log records, idempotent on ``request_id``.

    Deduplicates within the batch (so a single INSERT never lists the same
    ``request_id`` twice), then relies on the DB unique index +
    ``ON CONFLICT DO NOTHING`` to drop any row already written by another worker
    or a stale-claim recovery. A duplicate flush is therefore a safe no-op, not
    a crash from the unique constraint (Oracle #14). Returns the number of rows
    actually inserted.
    """
    from src.db.bulk import insert_ignore_conflicts
    from src.db.models.gateway_observability import GatewayRequestLog
    from src.db.session import unit_of_work

    seen_request_ids: set[str] = set()
    rows: list[dict[str, Any]] = []
    for record in records:
        request_id = record.get("request_id")
        if request_id:
            request_id = str(request_id)
            if request_id in seen_request_ids:
                continue
            seen_request_ids.add(request_id)
        rows.append(_to_log_row(record))

    async with unit_of_work() as session:
        written = await insert_ignore_conflicts(
            session, GatewayRequestLog, rows, index_elements=["request_id"]
        )

    skipped = len(records) - written
    if skipped:
        _log.info("gateway.log_batch.duplicates_skipped", count=skipped)
    _log.debug("gateway.log_batch.flushed", count=written)
    return written


def _to_log_row(data: dict[str, Any]) -> dict[str, Any]:
    """Convert a queue payload dict to a GatewayRequestLog insert-params dict.

    All rows carry an identical key set (so they batch as one executemany) with
    an explicit snowflake ``id``; ``created_at`` falls to the DB server_default.
    """
    from src.core.snowflake import next_id

    return {
        "id": next_id(),
        "request_id": data.get("request_id"),
        "user_id": data.get("user_id"),
        "api_key_id": data.get("api_key_id"),
        "logical_model_id": data.get("logical_model_id"),
        "model": data.get("model"),
        "channel_id": data.get("channel_id"),
        "upstream_model": data.get("upstream_model"),
        "provider": data.get("provider"),
        "status_code": data.get("status_code", 200),
        "error_code": data.get("error_code"),
        "error_body": data.get("error_body"),
        "latency_ms": data.get("latency_ms"),
        "ttft_ms": data.get("ttft_ms"),
        "tokens_in": data.get("tokens_in", 0),
        "tokens_out": data.get("tokens_out", 0),
        "stream": data.get("stream", False),
        "cache_hit": data.get("cache_hit", False),
        "retry_count": data.get("retry_count", 0),
        "fallback_used": data.get("fallback_used", False),
    }
