"""Gateway observability models (P1).

``GatewayRequestLog`` — append-only ledger of every LLM gateway call for
operational visibility, debugging, and SLA tracking.  Separate from
``UsageRecord`` (accounting): this captures routing/error/timing details that
are irrelevant to billing but essential for ops.
"""

from __future__ import annotations

from sqlalchemy import BigInteger, Boolean, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from src.db.base import LogEntity


class GatewayRequestLog(LogEntity):
    """One row per gateway LLM request (success or failure)."""

    __tablename__ = "gateway_request_log"
    __table_args__ = (
        Index("ix_gwlog_user_created", "user_id", "created_at"),
        Index("ix_gwlog_model_created", "model", "created_at"),
        Index("ix_gwlog_channel_created", "channel_id", "created_at"),
        Index("ix_gwlog_status_code", "status_code"),
        {"comment": "网关请求日志：每次 LLM 调用的路由/错误/延迟明细（运维排障用）"},
    )

    # --- Identity ---
    request_id: Mapped[str] = mapped_column(
        String(64), unique=True, comment="请求唯一标识（关联 usage_record.request_id）"
    )
    user_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, index=True, comment="调用者 users.id"
    )
    api_key_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, comment="调用者 api_key.id"
    )

    # --- Model routing ---
    logical_model_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, comment="逻辑模型 logical_model.id"
    )
    model: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="请求中的 model 名（逻辑模型 name）"
    )
    channel_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, comment="实际路由到的渠道 upstream_channel.id"
    )
    upstream_model: Mapped[str | None] = mapped_column(
        String(128), nullable=True, comment="上游真实模型名"
    )
    provider: Mapped[str | None] = mapped_column(
        String(32), nullable=True, index=True, comment="上游厂商（openai/anthropic/gemini）"
    )

    # --- Result ---
    status_code: Mapped[int] = mapped_column(
        Integer, default=200, comment="HTTP 状态码（200/429/502/504 等）"
    )
    error_code: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="ErrorCode 枚举值（仅失败时有值）"
    )
    error_body: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="上游错误体（截断至 2048 字符，脱敏）"
    )

    # --- Timing ---
    latency_ms: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="端到端延迟（毫秒）"
    )
    ttft_ms: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="首 token 延迟（毫秒，仅流式）"
    )

    # --- Usage ---
    tokens_in: Mapped[int] = mapped_column(Integer, default=0, comment="输入 token 数")
    tokens_out: Mapped[int] = mapped_column(Integer, default=0, comment="输出 token 数")

    # --- Flags ---
    stream: Mapped[bool] = mapped_column(Boolean, default=False, comment="是否流式请求")
    cache_hit: Mapped[bool] = mapped_column(
        Boolean, default=False, comment="是否命中响应缓存"
    )
    retry_count: Mapped[int] = mapped_column(
        Integer, default=0, comment="litellm Router 重试次数"
    )
    fallback_used: Mapped[bool] = mapped_column(
        Boolean, default=False, comment="是否使用了 fallback 渠道"
    )
