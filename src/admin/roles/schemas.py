"""Admin role DTOs (router layer contracts).

Snowflake ids serialize as strings. ``menu_ids`` is an id list exchanged as
strings (the role's permission grants).
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_serializer

from src.enums import ActiveStatus


class RoleCreate(BaseModel):
    """Create a role (optionally with initial menu grants)."""

    name: str = Field(min_length=1, max_length=64)
    code: str = Field(min_length=1, max_length=64)
    sort_order: int = 0
    status: ActiveStatus = ActiveStatus.active
    remark: str | None = Field(default=None, max_length=255)
    menu_ids: list[int] = Field(default_factory=list)


class RoleUpdate(BaseModel):
    """Partial role update; unset fields unchanged. Set list fields replace grants."""

    name: str | None = Field(default=None, min_length=1, max_length=64)
    sort_order: int | None = None
    status: ActiveStatus | None = None
    remark: str | None = Field(default=None, max_length=255)
    menu_ids: list[int] | None = None  # None = leave grants unchanged; [] = clear.


class RoleRead(BaseModel):
    """Role read model with current menu grants."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    code: str
    sort_order: int
    status: str
    remark: str | None
    created_at: datetime
    menu_ids: list[str] = Field(default_factory=list)

    @field_serializer("id")
    def _ser_id(self, value: int) -> str:
        return str(value)
