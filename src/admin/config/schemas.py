"""Platform config DTOs (router layer contracts).

Snowflake ``id`` serializes as a string (JS int64 precision). ``value_type``
is constrained to the :class:`ConfigValueType` enum; ``config_value`` is always
carried as a string (parsed by ``value_type`` on the runtime read path).
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_serializer

from src.enums import ConfigValueType


class SysConfigCreate(BaseModel):
    """Create a platform config entry."""

    config_key: str = Field(min_length=1, max_length=128)
    config_value: str
    value_type: ConfigValueType = ConfigValueType.string
    config_name: str = Field(min_length=1, max_length=128)
    is_builtin: bool = False
    remark: str | None = Field(default=None, max_length=255)


class SysConfigUpdate(BaseModel):
    """Partial config update; unset fields are left unchanged.

    ``config_key`` and ``is_builtin`` are intentionally not updatable: the key is
    the stable identity callers read by, and the builtin flag is a platform
    property, not an operator-editable field.
    """

    config_value: str | None = None
    value_type: ConfigValueType | None = None
    config_name: str | None = Field(default=None, min_length=1, max_length=128)
    remark: str | None = Field(default=None, max_length=255)


class SysConfigRead(BaseModel):
    """Config read model."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    config_key: str
    config_value: str
    value_type: ConfigValueType
    config_name: str
    is_builtin: bool
    remark: str | None
    created_at: datetime
    updated_at: datetime

    @field_serializer("id")
    def _ser_id(self, value: int) -> str:
        return str(value)
