"""Admin menu DTOs (menus, routes, and button permission nodes)."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_serializer

from src.enums import ActiveStatus, MenuType


class MenuCreate(BaseModel):
    """Create a menu tree node."""

    name: str = Field(min_length=1, max_length=64)
    parent_id: int | None = None
    menu_type: MenuType
    perms: str | None = Field(default=None, max_length=128)
    path: str | None = Field(default=None, max_length=255)
    component: str | None = Field(default=None, max_length=255)
    query_param: str | None = Field(default=None, max_length=255)
    is_frame: bool = False
    is_cache: bool = True
    icon: str | None = Field(default=None, max_length=64)
    sort_order: int = 0
    visible: bool = True
    status: ActiveStatus = ActiveStatus.active
    remark: str | None = Field(default=None, max_length=255)


class MenuUpdate(BaseModel):
    """Partial menu update."""

    name: str | None = Field(default=None, min_length=1, max_length=64)
    parent_id: int | None = None
    menu_type: MenuType | None = None
    perms: str | None = Field(default=None, max_length=128)
    path: str | None = Field(default=None, max_length=255)
    component: str | None = Field(default=None, max_length=255)
    query_param: str | None = Field(default=None, max_length=255)
    is_frame: bool | None = None
    is_cache: bool | None = None
    icon: str | None = Field(default=None, max_length=64)
    sort_order: int | None = None
    visible: bool | None = None
    status: ActiveStatus | None = None
    remark: str | None = Field(default=None, max_length=255)


class MenuRead(BaseModel):
    """Menu read model."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    parent_id: int | None
    menu_type: str
    perms: str | None
    path: str | None
    component: str | None
    query_param: str | None
    is_frame: bool
    is_cache: bool
    icon: str | None
    sort_order: int
    visible: bool
    status: str
    remark: str | None
    created_at: datetime

    @field_serializer("id")
    def _ser_id(self, value: int) -> str:
        return str(value)

    @field_serializer("parent_id")
    def _ser_parent_id(self, value: int | None) -> str | None:
        return str(value) if value is not None else None
