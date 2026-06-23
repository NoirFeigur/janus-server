"""Admin quota rule DTOs."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_serializer


class QuotaCreate(BaseModel):
    """Create a quota rule."""

    scope: Literal["user", "department", "global"]
    scope_id: int | None = None
    logical_model_id: int | None = None
    period: Literal["daily", "monthly", "total"]
    metric: Literal["requests", "tokens", "cost"] = "tokens"
    limit_value: Decimal = Field(gt=0)
    enforce: bool = True
    status: Literal["active", "disabled"] = "active"
    remark: str | None = Field(default=None, max_length=255)


class QuotaUpdate(BaseModel):
    """Partial quota update; identity fields are intentionally immutable."""

    limit_value: Decimal | None = Field(default=None, gt=0)
    enforce: bool | None = None
    status: Literal["active", "disabled"] | None = None
    remark: str | None = Field(default=None, max_length=255)


class QuotaRead(BaseModel):
    """Quota read model."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    scope: str
    scope_id: int | None
    logical_model_id: int | None
    period: str
    metric: str
    limit_value: Decimal
    enforce: bool
    status: str
    remark: str | None
    created_at: datetime
    updated_at: datetime

    @field_serializer("id")
    def _ser_id(self, value: int) -> str:
        return str(value)

    @field_serializer("scope_id", "logical_model_id")
    def _ser_optional_id(self, value: int | None) -> str | None:
        if value is None:
            return None
        return str(value)
