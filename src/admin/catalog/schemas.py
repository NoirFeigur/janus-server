"""Admin catalog DTOs (router layer contracts)."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_serializer


class UpstreamChannelCreate(BaseModel):
    """Create an upstream channel."""

    name: str = Field(min_length=1, max_length=64)
    provider: str = Field(min_length=1, max_length=32)
    protocol: str = Field(min_length=1, max_length=16)
    api_base: str | None = Field(default=None, max_length=255)
    extra_config: dict[str, Any] | None = None
    status: str = Field(default="active", max_length=16)
    remark: str | None = Field(default=None, max_length=255)


class UpstreamChannelUpdate(BaseModel):
    """Partial upstream channel update; unset fields are left unchanged."""

    name: str | None = Field(default=None, min_length=1, max_length=64)
    provider: str | None = Field(default=None, min_length=1, max_length=32)
    protocol: str | None = Field(default=None, min_length=1, max_length=16)
    api_base: str | None = Field(default=None, max_length=255)
    extra_config: dict[str, Any] | None = None
    status: str | None = Field(default=None, max_length=16)
    remark: str | None = Field(default=None, max_length=255)


class UpstreamChannelRead(BaseModel):
    """Upstream channel read model."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    provider: str
    protocol: str
    api_base: str | None
    extra_config: dict[str, Any] | None
    status: str
    remark: str | None
    created_at: datetime
    updated_at: datetime

    @field_serializer("id")
    def _ser_id(self, value: int) -> str:
        return str(value)


class ChannelKeyCreate(BaseModel):
    """Create a channel key; plaintext is accepted only on create."""

    channel_id: int
    alias: str = Field(min_length=1, max_length=64)
    api_key: str = Field(min_length=1)
    status: str = Field(default="active", max_length=16)
    rpm_limit: int | None = None
    tpm_limit: int | None = None
    weight: int = 1
    priority: int = 0
    remark: str | None = Field(default=None, max_length=255)


class ChannelKeyUpdate(BaseModel):
    """Partial channel key update; secret material is not updatable here."""

    channel_id: int | None = None
    alias: str | None = Field(default=None, min_length=1, max_length=64)
    status: str | None = Field(default=None, max_length=16)
    rpm_limit: int | None = None
    tpm_limit: int | None = None
    weight: int | None = None
    priority: int | None = None
    remark: str | None = Field(default=None, max_length=255)


class ChannelKeyRead(BaseModel):
    """Channel key read model; never exposes plaintext or ciphertext."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    channel_id: int
    alias: str
    key_hint: str
    status: str
    rpm_limit: int | None
    tpm_limit: int | None
    weight: int
    priority: int
    remark: str | None
    created_at: datetime
    updated_at: datetime

    @field_serializer("id", "channel_id")
    def _ser_id(self, value: int) -> str:
        return str(value)


class LogicalModelCreate(BaseModel):
    """Create a logical model."""

    name: str = Field(min_length=1, max_length=64)
    display_name: str = Field(min_length=1, max_length=64)
    category: str | None = Field(default=None, max_length=32)
    sort_order: int = 0
    context_length: int | None = None
    price_input: Decimal | None = None
    price_output: Decimal | None = None
    status: str = Field(default="active", max_length=16)
    remark: str | None = Field(default=None, max_length=255)


class LogicalModelUpdate(BaseModel):
    """Partial logical model update; unset fields are left unchanged."""

    name: str | None = Field(default=None, min_length=1, max_length=64)
    display_name: str | None = Field(default=None, min_length=1, max_length=64)
    category: str | None = Field(default=None, max_length=32)
    sort_order: int | None = None
    context_length: int | None = None
    price_input: Decimal | None = None
    price_output: Decimal | None = None
    status: str | None = Field(default=None, max_length=16)
    remark: str | None = Field(default=None, max_length=255)


class LogicalModelRead(BaseModel):
    """Logical model read model."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    display_name: str
    category: str | None
    sort_order: int
    context_length: int | None
    price_input: Decimal | None
    price_output: Decimal | None
    status: str
    remark: str | None
    created_at: datetime
    updated_at: datetime

    @field_serializer("id")
    def _ser_id(self, value: int) -> str:
        return str(value)


class ModelDeploymentCreate(BaseModel):
    """Create a model deployment."""

    logical_model_id: int
    channel_id: int
    upstream_model: str = Field(min_length=1, max_length=128)
    weight: int = 1
    priority: int = 0
    status: str = Field(default="active", max_length=16)
    remark: str | None = Field(default=None, max_length=255)


class ModelDeploymentUpdate(BaseModel):
    """Partial model deployment update; unset fields are left unchanged."""

    logical_model_id: int | None = None
    channel_id: int | None = None
    upstream_model: str | None = Field(default=None, min_length=1, max_length=128)
    weight: int | None = None
    priority: int | None = None
    status: str | None = Field(default=None, max_length=16)
    remark: str | None = Field(default=None, max_length=255)


class ModelDeploymentRead(BaseModel):
    """Model deployment read model."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    logical_model_id: int
    channel_id: int
    upstream_model: str
    weight: int
    priority: int
    status: str
    remark: str | None
    created_at: datetime
    updated_at: datetime

    @field_serializer("id", "logical_model_id", "channel_id")
    def _ser_id(self, value: int) -> str:
        return str(value)
