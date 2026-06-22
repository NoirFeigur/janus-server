"""Admin usage read DTOs."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, field_serializer


class UsageRecordRead(BaseModel):
    """Usage ledger row read model."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    api_key_id: int | None
    logical_model_id: int
    channel_id: int | None
    upstream_model: str | None
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    cost: Decimal | None
    status: str
    latency_ms: int | None
    request_id: str | None
    downgraded_features: list[Any] | None
    created_at: datetime

    @field_serializer("id", "user_id", "logical_model_id")
    def _ser_required_id(self, value: int) -> str:
        return str(value)

    @field_serializer("api_key_id", "channel_id")
    def _ser_optional_id(self, value: int | None) -> str | None:
        return str(value) if value is not None else None


class UsageStats(BaseModel):
    """Aggregated usage statistics."""

    total_requests: int
    total_tokens: int
    total_cost: Decimal | None
    avg_latency_ms: float | None
    error_rate: float
    success_count: int
    error_count: int
