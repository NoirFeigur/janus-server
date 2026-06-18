"""Audit log read DTOs (router layer contracts).

Read-only wire models over the append-only audit tables. Snowflake ids
serialize as strings; JSONB snapshot columns pass through as opaque dicts.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, field_serializer


class OperLogRead(BaseModel):
    """Operation audit log read model."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    actor_id: int | None
    actor_name: str | None
    module: str
    action: str
    method: str
    path: str
    target_id: str | None
    request_ip: str | None
    user_agent: str | None
    trace_id: str | None
    before_value: dict[str, Any] | None
    after_value: dict[str, Any] | None
    status: str
    error_code: str | None
    latency_ms: int | None
    created_at: datetime

    @field_serializer("id")
    def _ser_id(self, value: int) -> str:
        return str(value)

    @field_serializer("actor_id")
    def _ser_actor_id(self, value: int | None) -> str | None:
        return str(value) if value is not None else None


class LoginLogRead(BaseModel):
    """Login audit log read model."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int | None
    username: str
    status: str
    failure_reason: str | None
    request_ip: str | None
    user_agent: str | None
    trace_id: str | None
    created_at: datetime

    @field_serializer("id")
    def _ser_id(self, value: int) -> str:
        return str(value)

    @field_serializer("user_id")
    def _ser_user_id(self, value: int | None) -> str | None:
        return str(value) if value is not None else None
