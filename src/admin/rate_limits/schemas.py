"""Pydantic schemas for admin rate limit endpoints."""

from __future__ import annotations

from pydantic import BaseModel


class RateLimitRuleCreate(BaseModel):
    subject_type: str  # global | user | department | api_key
    subject_id: int | None = None
    logical_model_id: int | None = None
    rpm_limit: int | None = None
    tpm_limit: int | None = None
    tpm_burst_limit: int | None = None
    max_concurrent: int | None = None
    enforce: bool = True
    remark: str | None = None


class RateLimitRuleUpdate(BaseModel):
    rpm_limit: int | None = None
    tpm_limit: int | None = None
    tpm_burst_limit: int | None = None
    max_concurrent: int | None = None
    enforce: bool | None = None
    status: str | None = None
    remark: str | None = None


class RateLimitRuleRead(BaseModel):
    id: int
    subject_type: str
    subject_id: int | None = None
    logical_model_id: int | None = None
    rpm_limit: int | None = None
    tpm_limit: int | None = None
    tpm_burst_limit: int | None = None
    max_concurrent: int | None = None
    enforce: bool = True
    status: str = "active"
    remark: str | None = None

    model_config = {"from_attributes": True}
