"""Admin API key credential DTOs (router layer contracts)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_serializer


class ApiKeyCreate(BaseModel):
    """Create a platform-issued sk-key credential."""

    user_id: int
    name: str = Field(min_length=1, max_length=64)
    expires_at: datetime | None = None
    remark: str | None = Field(default=None, max_length=255)


class ApiKeyUpdate(BaseModel):
    """Partial sk-key credential update; unset fields are left unchanged."""

    name: str | None = Field(default=None, min_length=1, max_length=64)
    status: Literal["active", "disabled"] | None = None
    expires_at: datetime | None = None
    remark: str | None = Field(default=None, max_length=255)


class ApiKeyRead(BaseModel):
    """API key read model; never exposes the key hash or plaintext key."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    name: str
    key_prefix: str
    status: str
    expires_at: datetime | None
    last_used_at: datetime | None
    remark: str | None
    created_at: datetime
    updated_at: datetime

    @field_serializer("id", "user_id")
    def _ser_id(self, value: int) -> str:
        return str(value)


class ApiKeyCreateResponse(ApiKeyRead):
    """Create response that includes plaintext once, immediately after creation."""

    plain_key: str
