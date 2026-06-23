"""Batch writer for gateway request log events (P1 observability).

Same pattern as usage_batch: ARQ cron pops from Redis log queue, bulk-inserts
GatewayRequestLog rows, moves failures to DLQ.
"""

from __future__ import annotations

import json
from typing import Any

from src.core.logging import get_logger
from src.gateway.events import (
    DEFAULT_BATCH_SIZE,
    LOG_DLQ_KEY,
    LOG_QUEUE_KEY,
    pop_batch,
    send_to_dlq,
)

_log = get_logger(__name__)


async def flush_gateway_logs(ctx: dict[str, Any]) -> int:
    """ARQ task: pop log events from Redis queue and bulk-insert to DB.

    Returns the number of records successfully written.
    """
    items = await pop_batch(LOG_QUEUE_KEY, DEFAULT_BATCH_SIZE)
    if not items:
        return 0

    records: list[dict[str, Any]] = []
    unparseable: list[str] = []
    for raw in items:
        try:
            records.append(json.loads(raw))
        except (json.JSONDecodeError, TypeError):
            unparseable.append(raw)

    if unparseable:
        await send_to_dlq(LOG_DLQ_KEY, unparseable)
        _log.warning("gateway.log_batch.unparseable", count=len(unparseable))

    if not records:
        return 0

    return await _bulk_insert_logs(records)


async def _bulk_insert_logs(records: list[dict[str, Any]]) -> int:
    """Bulk-insert gateway request log records."""
    try:
        from src.db.session import unit_of_work

        async with unit_of_work() as session:
            orm_records = [_to_log_record(r) for r in records]
            session.add_all(orm_records)

        _log.debug("gateway.log_batch.flushed", count=len(records))
        return len(records)
    except Exception:
        _log.exception("gateway.log_batch.db_write_failed", count=len(records))
        failed_items = [json.dumps(r, default=str) for r in records]
        await send_to_dlq(LOG_DLQ_KEY, failed_items)
        return 0


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
