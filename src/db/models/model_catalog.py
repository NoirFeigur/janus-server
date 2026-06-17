"""Model-catalog aggregate (data-model batch 2).

The gateway's heart: logical models (employee-facing names) collapse, via
deployments and channel key-pools, into a flat ``litellm.Router`` deployment
list at runtime (§2.4 Cartesian expansion).

- ``UpstreamChannel`` — a (provider + api_base + protocol) access point.
- ``ChannelKey``      — a key-pool under a channel; upstream keys, reversibly
  encrypted (opposite direction from ``ApiKey``).
- ``LogicalModel``    — the unified name an employee requests.
- ``ModelDeployment`` — N:M carrier relation with its own routing attributes.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import BigInteger, DateTime, Index, Numeric, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from src.db.base import BaseEntity


class UpstreamChannel(BaseEntity):
    """Upstream channel: provider + api_base + protocol (§2.1)."""

    __tablename__ = "upstream_channel"
    __table_args__ = (
        Index(
            "uq_channel_name_active",
            "name",
            unique=True,
            postgresql_where=text("is_deleted = false"),
        ),
        {"comment": "上游渠道：provider + api_base + protocol 接入点"},
    )

    name: Mapped[str] = mapped_column(
        String(64),
        comment="渠道名，如 anthropic-official / bedrock-claude / deepseek",
    )
    provider: Mapped[str] = mapped_column(
        String(32),
        index=True,
        comment="厂商：anthropic|gemini|deepseek|glm|qwen|mimo...（驱动 litellm 前缀/计价）",
    )
    protocol: Mapped[str] = mapped_column(
        String(16),
        index=True,
        comment="对上游的协议：anthropic|openai|gemini",
    )

    api_base: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
        comment="上游 base_url；官方厂商为空（litellm 默认）",
    )

    extra_config: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB,
        nullable=True,
        comment="非密配置（region/api_version）；密钥放 channel_key",
    )

    status: Mapped[str] = mapped_column(
        String(16),
        default="active",
        index=True,
        comment="状态 ChannelStatus：active | disabled",
    )

    remark: Mapped[str | None] = mapped_column(
        String(255), nullable=True, comment="备注"
    )


class ChannelKey(BaseEntity):
    """Key-pool entry: an upstream key under a channel, encrypted (§2.2)."""

    __tablename__ = "channel_key"
    __table_args__ = (
        Index(
            "ix_channel_key_channel_status", "channel_id", "status"
        ),  # Fetch active keys per channel at Router build time.
        {"comment": "号池条目：渠道下的一把上游 key（可逆加密存储）"},
    )

    channel_id: Mapped[int] = mapped_column(
        BigInteger, index=True, comment="所属渠道 upstream_channel.id"
    )
    alias: Mapped[str] = mapped_column(
        String(64),
        comment='可读标签，如 "anthropic-主号"',
    )

    # Upstream vendor key: reversibly encrypted (decrypted at Router build time);
    # never plaintext on disk, never in any XxxRead.
    api_key_encrypted: Mapped[str] = mapped_column(
        Text, comment="上游厂商 key 密文（Fernet/KMS，绝不存明文）"
    )
    key_hint: Mapped[str] = mapped_column(
        String(16),
        comment='明文末几位如 "...a1b2"，供运维识别',
    )

    status: Mapped[str] = mapped_column(
        String(16),
        default="active",
        index=True,
        comment="状态 ChannelKeyStatus：active | disabled（cooldown 是 Redis 运行态，不入库）",
    )

    rpm_limit: Mapped[int | None] = mapped_column(
        nullable=True,
        comment="单 deployment rpm 上限（喂给 litellm.Router）；null=不限",
    )
    tpm_limit: Mapped[int | None] = mapped_column(
        nullable=True, comment="单 deployment tpm 上限；null=不限"
    )

    weight: Mapped[int] = mapped_column(
        default=1, comment="加权轮询权重"
    )
    priority: Mapped[int] = mapped_column(
        default=0, comment="渠道内 key 排序（小者先；0=主）"
    )

    last_used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="最后使用时间（异步更新，用于僵尸 key 发现）",
    )

    remark: Mapped[str | None] = mapped_column(
        String(255), nullable=True, comment="备注"
    )


class LogicalModel(BaseEntity):
    """Unified model name an employee sees (§2.3)."""

    __tablename__ = "logical_model"
    __table_args__ = (
        Index(
            "uq_logical_model_name_active",
            "name",
            unique=True,
            postgresql_where=text("is_deleted = false"),
        ),
        {"comment": "逻辑模型：员工看到的统一模型名"},
    )

    name: Mapped[str] = mapped_column(
        String(64),
        comment='员工作为 model 发送的统一模型名，如 "claude-sonnet"',
    )
    display_name: Mapped[str] = mapped_column(
        String(64),
        comment='前端展示名，如 "Claude Sonnet (推荐)"',
    )

    category: Mapped[str | None] = mapped_column(
        String(32),
        nullable=True,
        index=True,
        comment='模型选择器分组，如 "通用"/"代码"/"长文"',
    )
    sort_order: Mapped[int] = mapped_column(default=0, comment="排序")

    context_length: Mapped[int | None] = mapped_column(
        nullable=True,
        comment="null=运行时由 litellm.get_max_tokens() 解析；有值=显式覆盖",
    )

    # Internal pricing coefficients (per-million-token), not real USD. null = not
    # separately priced. Input/output split because LLM in/out prices differ.
    price_input: Mapped[Decimal | None] = mapped_column(
        Numeric(12, 6),
        nullable=True,
        comment="输入计价系数（每百万 token，内部成本点非美元）；null=不单独计价",
    )
    price_output: Mapped[Decimal | None] = mapped_column(
        Numeric(12, 6),
        nullable=True,
        comment="输出计价系数（每百万 token，内部成本点非美元）；null=不单独计价",
    )

    status: Mapped[str] = mapped_column(
        String(16),
        default="active",
        index=True,
        comment="状态 ActiveStatus：active | disabled",
    )

    remark: Mapped[str | None] = mapped_column(
        String(255), nullable=True, comment="备注"
    )


class ModelDeployment(BaseEntity):
    """Carrier relation: logical model <-> channel, N:M core (§2.4)."""

    __tablename__ = "model_deployment"
    __table_args__ = (
        Index(
            "uq_deployment_logical_model_channel_active",
            "logical_model_id",
            "channel_id",
            unique=True,
            postgresql_where=text("is_deleted = false"),
        ),  # One carrier per (logical model, channel).
        Index(
            "ix_deployment_logical_status", "logical_model_id", "status"
        ),  # Fetch active carriers per logical model at Router build time.
        {"comment": "模型部署：逻辑模型↔渠道 的承载关系（N:M 核心）"},
    )

    logical_model_id: Mapped[int] = mapped_column(
        BigInteger, index=True, comment="逻辑模型 logical_model.id"
    )
    channel_id: Mapped[int] = mapped_column(
        BigInteger, index=True, comment="渠道 upstream_channel.id"
    )

    upstream_model: Mapped[str] = mapped_column(
        String(128),
        comment="该渠道下的上游真实模型名，如 claude-sonnet-4-6",
    )

    weight: Mapped[int] = mapped_column(
        default=1, comment="跨渠道权重"
    )
    priority: Mapped[int] = mapped_column(
        default=0, comment="跨渠道主备（小者先；0=主）"
    )

    status: Mapped[str] = mapped_column(
        String(16),
        default="active",
        index=True,
        comment="状态 ActiveStatus：active | disabled",
    )

    remark: Mapped[str | None] = mapped_column(
        String(255), nullable=True, comment="备注"
    )
