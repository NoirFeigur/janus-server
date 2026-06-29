"""Audit log models (append-only LogEntity tables)."""

from __future__ import annotations

from typing import Any

from sqlalchemy import JSON, BigInteger, Index, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from src.db.base import LogEntity

_SQLITE_JSONB = JSONB().with_variant(JSON(), "sqlite")


class OperLog(LogEntity):
    """Operation audit log: who did what to which resource and with what result."""

    __tablename__ = "oper_log"
    __table_args__ = (
        Index("ix_oper_log_actor_created", "actor_id", "created_at"),
        Index("ix_oper_log_module_created", "module", "created_at"),
        {
            "comment": (
                "操作审计日志：后台写操作留痕（谁/何时/对什么/做了什么/前后值/"
                "trace_id/IP），追加写不可改"
            )
        },
    )

    actor_id: Mapped[int | None] = mapped_column(
        BigInteger,
        nullable=True,
        index=True,
        comment="操作人 users.id；null=系统操作",
    )
    actor_name: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        comment="操作人用户名快照（改名/删除后仍可读）",
    )
    module: Mapped[str] = mapped_column(
        String(64),
        index=True,
        comment="业务模块/资源域，如 user/role/menu/dept",
    )
    action: Mapped[str] = mapped_column(
        String(32),
        comment="操作动作，如 create/update/delete",
    )
    method: Mapped[str] = mapped_column(String(8), comment="HTTP 方法")
    path: Mapped[str] = mapped_column(String(255), comment="请求路径")
    target_id: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        comment="受影响资源 id",
    )
    request_ip: Mapped[str | None] = mapped_column(
        String(45), nullable=True, comment="请求来源 IP"
    )
    user_agent: Mapped[str | None] = mapped_column(
        String(255), nullable=True, comment="请求 User-Agent"
    )
    trace_id: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        index=True,
        comment="关联 trace_id",
    )
    before_value: Mapped[dict[str, Any] | None] = mapped_column(
        _SQLITE_JSONB,
        nullable=True,
        comment="变更前快照；null=不适用",
    )
    after_value: Mapped[dict[str, Any] | None] = mapped_column(
        _SQLITE_JSONB,
        nullable=True,
        comment="变更后快照；null=不适用",
    )
    status: Mapped[str] = mapped_column(
        String(16),
        index=True,
        comment="操作结果 AuditOutcome",
    )
    error_code: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="失败时的 ErrorCode；成功为空"
    )
    latency_ms: Mapped[int | None] = mapped_column(
        nullable=True, comment="端到端处理耗时（毫秒）"
    )


class LoginLog(LogEntity):
    """Login audit log: every login attempt, successful or failed."""

    __tablename__ = "login_log"
    __table_args__ = (
        Index("ix_login_log_user_created", "user_id", "created_at"),
        Index("ix_login_log_username_created", "username", "created_at"),
        {
            "comment": (
                "登录审计日志：记录每次登录尝试（成功/失败、用户名、IP、UA、失败原因），"
                "追加写不可改"
            )
        },
    )

    user_id: Mapped[int | None] = mapped_column(
        BigInteger,
        nullable=True,
        index=True,
        comment="命中的 users.id；用户名无法解析时为空",
    )
    username: Mapped[str] = mapped_column(
        String(64), index=True, comment="尝试登录的用户名"
    )
    status: Mapped[str] = mapped_column(
        String(16),
        index=True,
        comment="登录结果 AuditOutcome",
    )
    failure_reason: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        comment="失败原因 LoginFailureReason；成功为空",
    )
    request_ip: Mapped[str | None] = mapped_column(
        String(45), nullable=True, comment="登录来源 IP"
    )
    user_agent: Mapped[str | None] = mapped_column(
        String(255), nullable=True, comment="登录 User-Agent"
    )
    trace_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True, comment="关联 trace_id"
    )
