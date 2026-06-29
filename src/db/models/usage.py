"""Usage aggregate (data-model batch 3, table 3.1).

Append-only per-call ledger (``LogEntity``): the authoritative source for
billing, usage reports, and downgrade-rate statistics. Real-time quota counting
lives in Redis; this table is the durable user-level ledger of record.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from sqlalchemy import BigInteger, Index, Numeric, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from src.db.base import LogEntity


class UsageRecord(LogEntity):
    """One row per LLM call (§3.1)."""

    __tablename__ = "usage_record"
    __table_args__ = (
        Index("ix_usage_user_created", "user_id", "created_at"),
        Index("ix_usage_logical_model_created", "logical_model_id", "created_at"),
        {"comment": "用量流水：每次 LLM 调用一行，记账/报表/降级率统计的权威源"},
    )

    # —— Principal (who it is billed to) ——
    user_id: Mapped[int] = mapped_column(
        BigInteger, index=True, comment="计费主体用户 users.id"
    )
    api_key_id: Mapped[int | None] = mapped_column(
        BigInteger,
        nullable=True,
        index=True,
        comment="发起调用的 api_key.id；直连 SSO 时为空",
    )

    # —— Model and carrier (what was used) ——
    logical_model_id: Mapped[int] = mapped_column(
        BigInteger,
        index=True,
        comment="员工请求的逻辑模型 logical_model.id",
    )
    channel_id: Mapped[int | None] = mapped_column(
        BigInteger,
        nullable=True,
        index=True,
        comment="实际路由到的渠道 upstream_channel.id",
    )
    upstream_model: Mapped[str | None] = mapped_column(
        String(128), nullable=True, comment="实际调用的上游真实模型名"
    )

    # —— Metering (how much) ——
    prompt_tokens: Mapped[int] = mapped_column(default=0, comment="输入 token 数")
    completion_tokens: Mapped[int] = mapped_column(
        default=0, comment="输出 token 数"
    )
    total_tokens: Mapped[int] = mapped_column(default=0, comment="总 token 数（冗余）")
    cost: Mapped[Decimal | None] = mapped_column(
        Numeric(14, 6),
        nullable=True,
        comment="调用时冻结的内部成本：(in*price_in + out*price_out) / 1e6",
    )

    # —— Result and observability ——
    status: Mapped[str] = mapped_column(
        String(16),
        index=True,
        comment="结果 UsageStatus",
    )
    latency_ms: Mapped[int | None] = mapped_column(
        nullable=True, comment="端到端延迟（毫秒）"
    )
    request_id: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        unique=True,
        index=True,
        comment="贯穿网关日志/Redis 的关联 id（唯一：记账幂等防重复扣费）",
    )
    downgraded_features: Mapped[list[Any] | None] = mapped_column(
        JSONB,
        nullable=True,
        comment="本次调用被降级的特性；null/空=未降级",
    )
