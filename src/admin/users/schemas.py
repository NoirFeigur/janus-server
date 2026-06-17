"""Admin user DTOs (router layer contracts).

``password`` appears on create/update inputs only — it is never present on any
read model (§0.8 iron rule: the column stores an argon2 hash and no XxxRead
exposes it). Snowflake ids serialize as strings. ``role_ids`` is the set of
assigned role ids (string list on the wire).
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_serializer

from src.enums import UserStatus


class UserCreate(BaseModel):
    """Create a user. ``password`` optional (SSO-only users have none)."""

    username: str = Field(min_length=1, max_length=64)
    employee_no: str = Field(min_length=1, max_length=64)
    password: str | None = Field(default=None, min_length=1, max_length=128)
    real_name: str | None = Field(default=None, max_length=64)
    email: str | None = Field(default=None, max_length=255)
    mobile: str | None = Field(default=None, max_length=32)
    department_id: int | None = None
    status: UserStatus = UserStatus.active
    preferred_locale: str = "zh-CN"
    remark: str | None = Field(default=None, max_length=255)
    role_ids: list[int] = Field(default_factory=list)


class UserUpdate(BaseModel):
    """Partial user update; unset fields unchanged. ``role_ids`` set = replace."""

    password: str | None = Field(default=None, min_length=1, max_length=128)
    real_name: str | None = Field(default=None, max_length=64)
    email: str | None = Field(default=None, max_length=255)
    mobile: str | None = Field(default=None, max_length=32)
    department_id: int | None = None
    status: UserStatus | None = None
    preferred_locale: str | None = None
    remark: str | None = Field(default=None, max_length=255)
    role_ids: list[int] | None = None  # None = unchanged; [] = clear assignments.


class UserRead(BaseModel):
    """User read model. Never carries ``password`` (§0.8)."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    employee_no: str
    real_name: str | None
    email: str | None
    mobile: str | None
    department_id: int | None
    status: str
    preferred_locale: str
    remark: str | None
    created_at: datetime
    role_ids: list[str] = Field(default_factory=list)

    @field_serializer("id")
    def _ser_id(self, value: int) -> str:
        return str(value)

    @field_serializer("department_id")
    def _ser_dept(self, value: int | None) -> str | None:
        return str(value) if value is not None else None
