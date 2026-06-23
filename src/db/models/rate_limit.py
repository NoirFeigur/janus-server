"""Rate limit rules (P2).

Admission-level traffic shaping — separate from quota (accounting).
RPM/TPM/concurrent limits applied BEFORE quota reservation.
"""

from __future__ import annotations

from sqlalchemy import BigInteger, Boolean, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from src.db.base import BaseEntity


class RateLimitRule(BaseEntity):
    """A rate limit rule targeting a subject (user/department/global/api_key)."""

    __tablename__ = "rate_limit_rule"
    __table_args__ = (
        UniqueConstraint(
            "subject_type",
            "subject_id",
            "logical_model_id",
            name="uq_rate_limit_active_rule",
        ),
        {"comment": "速率限制规则：RPM/TPM/并发上限（流量整形，非计费配额）"},
    )

    # --- Subject scope ---
    subject_type: Mapped[str] = mapped_column(
        String(16),
        index=True,
        comment="限制主体类型：global | user | department | api_key",
    )
    subject_id: Mapped[int | None] = mapped_column(
        BigInteger,
        nullable=True,
        index=True,
        comment="主体 ID（global 时为 null）",
    )
    logical_model_id: Mapped[int | None] = mapped_column(
        BigInteger,
        nullable=True,
        index=True,
        comment="限制特定模型（null=所有模型）",
    )

    # --- Limits ---
    rpm_limit: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="每分钟请求上限（null=不限）"
    )
    tpm_limit: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="每分钟 token 上限（null=不限）"
    )
    tpm_burst_limit: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="TPM 突发上限（null=不限）"
    )
    max_concurrent: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="最大并发流式请求数（null=不限）"
    )

    # --- Control ---
    enforce: Mapped[bool] = mapped_column(
        Boolean, default=True, comment="是否执行（false=仅记录不拦截）"
    )
    status: Mapped[str] = mapped_column(
        String(16), default="active", index=True, comment="active | disabled"
    )
    remark: Mapped[str | None] = mapped_column(
        String(255), nullable=True, comment="备注说明"
    )
