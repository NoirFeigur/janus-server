"""Catalog operational safety models (P4).

``CatalogChangeLog`` — records every admin mutation (create/update/delete/rotate)
with before/after JSON diffs for audit trail and rollback support.

``CatalogConfigSnapshot`` — point-in-time serialization of all catalog config
(channels, keys, models, deployments) for dry-run validation and rollback.
"""

from __future__ import annotations

from sqlalchemy import BigInteger, Boolean, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from src.db.base import LogEntity


class CatalogChangeLog(LogEntity):
    """Audit entry for catalog configuration changes."""

    __tablename__ = "catalog_change_log"
    __table_args__ = {"comment": "目录配置变更日志：每次渠道/模型/部署/密钥变更的前后快照"}

    actor_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, index=True, comment="操作人 sys_user.id"
    )
    resource_type: Mapped[str] = mapped_column(
        String(32), index=True, comment="变更资源类型：channel | key | model | deployment"
    )
    resource_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="变更资源 ID"
    )
    action: Mapped[str] = mapped_column(
        String(32), index=True, comment="操作：create | update | delete | rotate | auto_disable"
    )
    before_value: Mapped[dict | None] = mapped_column(
        JSONB, nullable=True, comment="变更前值（脱敏）"
    )
    after_value: Mapped[dict | None] = mapped_column(
        JSONB, nullable=True, comment="变更后值（脱敏）"
    )
    diff: Mapped[dict | None] = mapped_column(
        JSONB, nullable=True, comment="差异摘要（field→{old,new}）"
    )
    snapshot_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, comment="关联配置快照 ID（变更前自动创建）"
    )
    trace_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="请求 trace_id（关联网关日志）"
    )


class CatalogConfigSnapshot(LogEntity):
    """Point-in-time catalog configuration snapshot for rollback."""

    __tablename__ = "catalog_config_snapshot"
    __table_args__ = {"comment": "目录配置快照：用于 dry-run 校验和回滚"}

    actor_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, comment="创建快照的操作人"
    )
    reason: Mapped[str] = mapped_column(
        String(32),
        comment="创建原因：known_good | pre_write | rollback | dry_run",
    )
    config_hash: Mapped[str] = mapped_column(
        String(64), index=True, comment="配置内容 SHA-256（用于去重和比对）"
    )
    is_known_good: Mapped[bool] = mapped_column(
        Boolean, default=False, comment="是否为最近一次验证通过的已知良好配置"
    )
    config: Mapped[dict] = mapped_column(
        JSONB,
        comment="完整配置 JSON：channels/keys/models/deployments（密钥字段保留加密形式）",
    )
    remark: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="备注（回滚时记录原因）"
    )
