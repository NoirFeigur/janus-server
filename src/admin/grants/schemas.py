"""Admin model-grant DTOs (router layer contracts)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_serializer


class GrantCreate(BaseModel):
    """Create a user/department model grant."""

    scope: Literal["user", "department"]
    scope_id: int
    logical_model_id: int
    is_default: bool = False
    remark: str | None = Field(default=None, max_length=255)


class GrantUpdate(BaseModel):
    """Partial grant update; identity fields are immutable."""

    is_default: bool | None = None
    remark: str | None = Field(default=None, max_length=255)


class GrantRead(BaseModel):
    """Model grant read model."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    scope: str
    scope_id: int
    logical_model_id: int
    is_default: bool
    remark: str | None
    created_at: datetime
    updated_at: datetime

    @field_serializer("id", "scope_id", "logical_model_id")
    def _ser_id(self, value: int) -> str:
        return str(value)
