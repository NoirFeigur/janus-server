"""Credential aggregate (data-model batch 1, table 1.4).

Platform-issued ``sk-...`` keys. Opposite encryption direction from
``ChannelKey``: the platform only *verifies* these, so it stores a one-way
sha256 hash (never the plaintext, never reversible). Plaintext is shown exactly
once at creation time.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from src.db.base import BaseEntity


class ApiKey(BaseEntity):
    """Platform-issued sk-key credential, stored as a hash (§1.4)."""

    __tablename__ = "api_key"

    user_id: Mapped[int] = mapped_column(
        BigInteger, index=True
    )  # -> sys_user, key owner.
    name: Mapped[str] = mapped_column(
        String(64)
    )  # Purpose label, e.g. "本地开发" / "oa-定时任务".

    key_hash: Mapped[str] = mapped_column(
        String(64), index=True
    )  # sha256 of the sk-key; plaintext returned once at creation.
    key_prefix: Mapped[str] = mapped_column(
        String(16)
    )  # Plaintext prefix like "sk-a1b2" for list display (non-sensitive).

    status: Mapped[str] = mapped_column(
        String(16), default="active", index=True
    )  # ApiKeyStatus: active | disabled.
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )  # Expiry; null = never expires.
    last_used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )  # Last use (updated asynchronously to avoid a hot row).

    remark: Mapped[str | None] = mapped_column(String(255), nullable=True)

    __table_args__ = (
        Index(
            "uq_apikey_hash", "key_hash", unique=True
        ),  # Global hash uniqueness (no is_deleted filter: same hash = same key).
    )
