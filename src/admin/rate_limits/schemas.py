"""Pydantic schemas for admin rate limit endpoints."""

from __future__ import annotations

from pydantic import BaseModel, Field, field_serializer, model_validator

from src.enums import RateLimitScope


class RateLimitRuleCreate(BaseModel):
    subject_type: RateLimitScope  # global | user | department | api_key
    subject_id: int | None = None
    logical_model_id: int | None = None
    rpm_limit: int | None = Field(default=None, ge=1)
    tpm_limit: int | None = Field(default=None, ge=1)
    tpm_burst_limit: int | None = Field(default=None, ge=1)
    max_concurrent: int | None = Field(default=None, ge=1)
    enforce: bool = True
    remark: str | None = None

    @model_validator(mode="after")
    def _check_subject_id(self) -> RateLimitRuleCreate:
        """A global rule targets the whole platform (subject_id must be null);
        every other scope targets a concrete subject (subject_id required)."""
        if self.subject_type == RateLimitScope.global_:
            if self.subject_id is not None:
                raise ValueError("global rule must not carry a subject_id")
        elif self.subject_id is None:
            raise ValueError(f"{self.subject_type} rule requires a subject_id")
        return self


class RateLimitRuleUpdate(BaseModel):
    rpm_limit: int | None = Field(default=None, ge=1)
    tpm_limit: int | None = Field(default=None, ge=1)
    tpm_burst_limit: int | None = Field(default=None, ge=1)
    max_concurrent: int | None = Field(default=None, ge=1)
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

    @field_serializer("id")
    def _ser_id(self, value: int) -> str:
        # Snowflake IDs exceed JS Number.MAX_SAFE_INTEGER; serialize as string so
        # the frontend never silently rounds them.
        return str(value)

    @field_serializer("subject_id", "logical_model_id")
    def _ser_optional_id(self, value: int | None) -> str | None:
        return str(value) if value is not None else None
