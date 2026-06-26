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
    __table_args__ = (
        Index(
            "uq_apikey_hash", "key_hash", unique=True
        ),  # Global hash uniqueness (no is_deleted filter: same hash = same key).
        {"comment": "平台 sk-key 凭证：只存哈希（平台仅验签，绝不可逆）"},
    )

    user_id: Mapped[int] = mapped_column(
        BigInteger, index=True, comment="key 所属用户 sys_user.id"
    )
    name: Mapped[str] = mapped_column(
        String(64),
        comment='用途标签，如 "本地开发"',
    )

    key_hash: Mapped[str] = mapped_column(
        String(64),
        index=True,
        comment="sk-key 的 sha256 哈希；明文仅创建时返回一次",
    )
    key_prefix: Mapped[str] = mapped_column(
        String(16),
        comment='明文前缀如 "sk-a1b2"，列表脱敏展示',
    )

    status: Mapped[str] = mapped_column(
        String(16),
        default="active",
        index=True,
        comment="状态 ApiKeyStatus",
    )
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="过期时间；null=永不过期"
    )
    last_used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="最后使用时间（异步更新）",
    )

    remark: Mapped[str | None] = mapped_column(String(255), nullable=True)
