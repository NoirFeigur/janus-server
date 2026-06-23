"""Pydantic schemas for channel health admin endpoints."""

from __future__ import annotations

from pydantic import BaseModel


class ChannelHealthRead(BaseModel):
    channel_id: int
    status: str  # "healthy" | "degraded" | "disabled"
    error_rate: float | None = None
    total_requests: int | None = None
    error_count: int | None = None
    degraded_since: str | None = None
    probe_failures: int | None = None
    last_probe_at: str | None = None


class ChannelHealthAction(BaseModel):
    reason: str | None = None
