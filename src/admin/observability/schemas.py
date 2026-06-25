"""Pydantic schemas for admin observability endpoints."""

from __future__ import annotations

from pydantic import BaseModel, field_serializer


class GatewayLogRead(BaseModel):
    id: int
    request_id: str
    user_id: int | None = None
    api_key_id: int | None = None
    logical_model_id: int | None = None
    model: str | None = None
    channel_id: int | None = None
    upstream_model: str | None = None
    provider: str | None = None
    status_code: int = 200
    error_code: str | None = None
    error_body: str | None = None
    latency_ms: int | None = None
    ttft_ms: int | None = None
    tokens_in: int = 0
    tokens_out: int = 0
    stream: bool = False
    cache_hit: bool = False
    retry_count: int = 0
    fallback_used: bool = False
    created_at: str | None = None

    model_config = {"from_attributes": True}

    @field_serializer("id")
    def _ser_id(self, value: int) -> str:
        # Snowflake IDs exceed JS Number.MAX_SAFE_INTEGER; serialize as string.
        return str(value)

    @field_serializer("user_id", "api_key_id", "logical_model_id", "channel_id")
    def _ser_optional_id(self, value: int | None) -> str | None:
        return str(value) if value is not None else None


class QueueHealthRead(BaseModel):
    usage_pending: int = 0
    usage_inflight: int = 0
    log_pending: int = 0
    log_inflight: int = 0
    usage_dlq: int = 0
    log_dlq: int = 0


class DlqItemRead(BaseModel):
    """Single dead-letter queue item (raw JSON payload)."""

    data: dict
