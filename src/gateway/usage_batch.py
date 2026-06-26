"""Durable batch writer for usage events (P3).

Runs as an ARQ cron job (every 5 seconds).  Claims pending usage events into an
inflight Redis list, bulk-inserts them into ``usage_record``, and ACKs only after
the DB transaction succeeds.  Failed writes are requeued with bounded retry
metadata and moved to DLQ after max retries.
"""

from __future__ import annotations

import json
from contextlib import suppress
from decimal import Decimal
from typing import Any

from src.core.logging import get_logger
from src.gateway.events import (
    DEFAULT_BATCH_SIZE,
    USAGE_DLQ_KEY,
    USAGE_INFLIGHT_KEY,
    USAGE_QUEUE_KEY,
    ClaimedEvent,
    ack_claimed,
    claim_batch,
    dlq_claimed,
    recover_stale_claims,
    retry_or_dlq_claimed,
)

_log = get_logger(__name__)


async def flush_usage_records(ctx: dict[str, Any]) -> int:
    """ARQ task: claim usage events from Redis queue and bulk-insert to DB.

    Returns the number of records successfully written.
    """
    await recover_stale_claims(
        queue_key=USAGE_QUEUE_KEY,
        inflight_key=USAGE_INFLIGHT_KEY,
        dlq_key=USAGE_DLQ_KEY,
    )
    claimed = await claim_batch(USAGE_QUEUE_KEY, USAGE_INFLIGHT_KEY, DEFAULT_BATCH_SIZE)
    if not claimed:
        return 0

    records: list[dict[str, Any]] = []
    valid_claims: list[ClaimedEvent] = []
    unparseable: list[ClaimedEvent] = []
    for item in claimed:
        try:
            record = json.loads(item.raw)
            if not isinstance(record, dict):
                raise TypeError("usage event must be a JSON object")
            records.append(record)
            valid_claims.append(item)
        except (json.JSONDecodeError, TypeError):
            unparseable.append(item)

    if unparseable:
        await dlq_claimed(USAGE_INFLIGHT_KEY, USAGE_DLQ_KEY, unparseable)
        _log.warning("gateway.usage_batch.unparseable", count=len(unparseable))

    if not records:
        return 0

    try:
        written = await _bulk_insert_usage(records)
    except Exception:
        _log.exception("gateway.usage_batch.db_write_failed", count=len(records))
        await retry_or_dlq_claimed(
            queue_key=USAGE_QUEUE_KEY,
            inflight_key=USAGE_INFLIGHT_KEY,
            dlq_key=USAGE_DLQ_KEY,
            items=valid_claims,
        )
        return 0

    await ack_claimed(USAGE_INFLIGHT_KEY, valid_claims)
    return written


async def _bulk_insert_usage(records: list[dict[str, Any]]) -> int:
    """Bulk-insert usage records, idempotent on ``request_id``.

    Deduplicates within the batch (so a single INSERT never lists the same
    ``request_id`` twice), then relies on the DB unique index +
    ``ON CONFLICT DO NOTHING`` to drop any row already written by another worker
    or a stale-claim recovery. A duplicate flush is therefore a safe no-op, not
    a double insert (Oracle #1). Returns the number of rows actually inserted.
    """
    from src.db.bulk import insert_ignore_conflicts
    from src.db.models.usage import UsageRecord
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
        rows.append(_to_usage_row(record))

    async with unit_of_work() as session:
        written = await insert_ignore_conflicts(
            session, UsageRecord, rows, index_elements=["request_id"]
        )

    skipped = len(records) - written
    if skipped:
        _log.info("gateway.usage_batch.duplicates_skipped", count=skipped)
    _log.debug("gateway.usage_batch.flushed", count=written)
    return written


def _to_usage_row(data: dict[str, Any]) -> dict[str, Any]:
    """Convert a queue payload dict to a UsageRecord insert-params dict.

    All rows carry an identical key set (so they batch as one executemany) with
    an explicit snowflake ``id``; ``created_at`` falls to the DB server_default.
    """
    from src.core.snowflake import next_id

    cost_raw = data.get("cost")
    cost: Decimal | None = None
    if cost_raw is not None:
        with suppress(Exception):
            cost = Decimal(cost_raw)

    return {
        "id": next_id(),
        "user_id": data["user_id"],
        "api_key_id": data.get("api_key_id"),
        "logical_model_id": data["logical_model_id"],
        "channel_id": data.get("channel_id"),
        "upstream_model": data.get("upstream_model"),
        "prompt_tokens": data.get("prompt_tokens", 0),
        "completion_tokens": data.get("completion_tokens", 0),
        "total_tokens": data.get("total_tokens", 0),
        "cost": cost,
        "status": data.get("status", "error"),
        "latency_ms": data.get("latency_ms"),
        "request_id": data.get("request_id"),
        "downgraded_features": data.get("downgraded_features"),
    }
