"""Admin role DTOs (router layer contracts).

Snowflake ids serialize as strings. ``data_scope`` is the :class:`DataScope`
enum (validated against the 6-tier set). ``menu_ids`` / ``dept_ids`` are id lists
exchanged as strings; ``dept_ids`` is only meaningful when ``data_scope=custom``.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_serializer

from src.enums import ActiveStatus, DataScope


class RoleCreate(BaseModel):
    """Create a role (optionally with initial menu/dept grants)."""

    name: str = Field(min_length=1, max_length=64)
    code: str = Field(min_length=1, max_length=64)
    data_scope: DataScope = DataScope.self_only
    sort_order: int = 0
    status: ActiveStatus = ActiveStatus.active
    remark: str | None = Field(default=None, max_length=255)
    menu_ids: list[int] = Field(default_factory=list)
    dept_ids: list[int] = Field(default_factory=list)  # only used when data_scope=custom


class RoleUpdate(BaseModel):
    """Partial role update; unset fields unchanged. Set list fields replace grants."""

    name: str | None = Field(default=None, min_length=1, max_length=64)
    data_scope: DataScope | None = None
    sort_order: int | None = None
    status: ActiveStatus | None = None
    remark: str | None = Field(default=None, max_length=255)
    menu_ids: list[int] | None = None  # None = leave grants unchanged; [] = clear.
    dept_ids: list[int] | None = None


class RoleRead(BaseModel):
    """Role read model with current menu/dept grants."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    code: str
    data_scope: str
    sort_order: int
    status: str
    remark: str | None
    created_at: datetime
    menu_ids: list[str] = Field(default_factory=list)
    dept_ids: list[str] = Field(default_factory=list)

    @field_serializer("id")
    def _ser_id(self, value: int) -> str:
        return str(value)
