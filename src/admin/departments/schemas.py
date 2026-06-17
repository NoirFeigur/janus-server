"""Admin department DTOs (router layer contracts).

Snowflake ids serialize as strings (``id``/``parent_id``) — see the auth
``CurrentUserRead`` note: int64 ids would lose precision in the JS client.
Create/update accept ``parent_id`` as an optional string (pydantic coerces).
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_serializer


class DepartmentCreate(BaseModel):
    """Create a department."""

    name: str = Field(min_length=1, max_length=128)
    parent_id: int | None = None  # None = top-level.
    sort_order: int = 0
    remark: str | None = Field(default=None, max_length=255)


class DepartmentUpdate(BaseModel):
    """Partial department update; unset fields are left unchanged."""

    name: str | None = Field(default=None, min_length=1, max_length=128)
    parent_id: int | None = None
    sort_order: int | None = None
    remark: str | None = Field(default=None, max_length=255)


class DepartmentRead(BaseModel):
    """Department read model."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    parent_id: int | None
    sort_order: int
    remark: str | None
    created_at: datetime

    @field_serializer("id")
    def _ser_id(self, value: int) -> str:
        return str(value)

    @field_serializer("parent_id")
    def _ser_parent_id(self, value: int | None) -> str | None:
        return str(value) if value is not None else None
