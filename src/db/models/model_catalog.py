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

    name: Mapped[str] = mapped_column(
        String(64)
    )  # Channel name, e.g. anthropic-official / bedrock-claude / deepseek.
    provider: Mapped[str] = mapped_column(
        String(32), index=True
    )  # Vendor: anthropic|gemini|deepseek|glm|qwen|mimo... (drives litellm prefix/pricing).
    protocol: Mapped[str] = mapped_column(
        String(16), index=True
    )  # Wire protocol to the upstream: anthropic|openai|gemini.

    api_base: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )  # Upstream base_url; null for official vendors (litellm default).

    extra_config: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, nullable=True
    )  # Non-secret provider config (region/api_version); secrets go in channel_key.

    status: Mapped[str] = mapped_column(
        String(16), default="active", index=True
    )  # ChannelStatus: active | disabled.

    remark: Mapped[str | None] = mapped_column(String(255), nullable=True)

    __table_args__ = (
        Index(
            "uq_channel_name_active",
            "name",
            unique=True,
            postgresql_where=text("is_deleted = false"),
        ),
    )


class ChannelKey(BaseEntity):
    """Key-pool entry: an upstream key under a channel, encrypted (§2.2)."""

    __tablename__ = "channel_key"

    channel_id: Mapped[int] = mapped_column(
        BigInteger, index=True
    )  # -> upstream_channel.
    alias: Mapped[str] = mapped_column(
        String(64)
    )  # Human-readable label, e.g. "anthropic-主号".

    # Upstream vendor key: reversibly encrypted (decrypted at Router build time);
    # never plaintext on disk, never in any XxxRead.
    api_key_encrypted: Mapped[str] = mapped_column(Text)  # Fernet/KMS ciphertext.
    key_hint: Mapped[str] = mapped_column(
        String(16)
    )  # Plaintext last few chars like "...a1b2" for ops identification.

    status: Mapped[str] = mapped_column(
        String(16), default="active", index=True
    )  # ChannelKeyStatus: active | disabled. cooldown is a Redis-only runtime state, not stored.

    rpm_limit: Mapped[int | None] = mapped_column(
        nullable=True
    )  # Per-deployment rpm cap fed to litellm.Router; null = unlimited.
    tpm_limit: Mapped[int | None] = mapped_column(
        nullable=True
    )  # Per-deployment tpm cap; null = unlimited.

    weight: Mapped[int] = mapped_column(default=1)  # Weighted round-robin weight.
    priority: Mapped[int] = mapped_column(
        default=0
    )  # Intra-channel key ordering (smaller first; 0 = primary).

    last_used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )  # Zombie-key discovery (updated asynchronously).

    remark: Mapped[str | None] = mapped_column(String(255), nullable=True)

    __table_args__ = (
        Index(
            "ix_channel_key_channel_status", "channel_id", "status"
        ),  # Fetch active keys per channel at Router build time.
    )


class LogicalModel(BaseEntity):
    """Unified model name an employee sees (§2.3)."""

    __tablename__ = "logical_model"

    name: Mapped[str] = mapped_column(
        String(64)
    )  # Unified model name the employee sends as ``model``, e.g. "claude-sonnet".
    display_name: Mapped[str] = mapped_column(
        String(64)
    )  # Frontend display name, e.g. "Claude Sonnet (推荐)".

    category: Mapped[str | None] = mapped_column(
        String(32), nullable=True, index=True
    )  # Grouping for the model picker, e.g. "通用"/"代码"/"长文".
    sort_order: Mapped[int] = mapped_column(default=0)

    context_length: Mapped[int | None] = mapped_column(
        nullable=True
    )  # null = resolved at runtime via litellm.get_max_tokens(); set = explicit override.

    # Internal pricing coefficients (per-million-token), not real USD. null = not
    # separately priced. Input/output split because LLM in/out prices differ.
    price_input: Mapped[Decimal | None] = mapped_column(Numeric(12, 6), nullable=True)
    price_output: Mapped[Decimal | None] = mapped_column(Numeric(12, 6), nullable=True)

    status: Mapped[str] = mapped_column(
        String(16), default="active", index=True
    )  # ActiveStatus: active | disabled.

    remark: Mapped[str | None] = mapped_column(String(255), nullable=True)

    __table_args__ = (
        Index(
            "uq_logical_model_name_active",
            "name",
            unique=True,
            postgresql_where=text("is_deleted = false"),
        ),
    )


class ModelDeployment(BaseEntity):
    """Carrier relation: logical model <-> channel, N:M core (§2.4)."""

    __tablename__ = "model_deployment"

    logical_model_id: Mapped[int] = mapped_column(
        BigInteger, index=True
    )  # -> logical_model.
    channel_id: Mapped[int] = mapped_column(
        BigInteger, index=True
    )  # -> upstream_channel.

    upstream_model: Mapped[str] = mapped_column(
        String(128)
    )  # Real upstream model name under this channel, e.g. claude-sonnet-4-6.

    weight: Mapped[int] = mapped_column(default=1)  # Inter-channel weight.
    priority: Mapped[int] = mapped_column(
        default=0
    )  # Inter-channel primary/backup (smaller first; 0 = primary).

    status: Mapped[str] = mapped_column(
        String(16), default="active", index=True
    )  # ActiveStatus: active | disabled.

    remark: Mapped[str | None] = mapped_column(String(255), nullable=True)

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
    )
