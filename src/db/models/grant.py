"""Grant aggregate (data-model batch 2, table 2.5).

AI-resource consumption rights (the B axis) — orthogonal to RBAC admin rights.
Decides which logical models a user/department may use, plus the default model.
Carries the ``is_default`` business attribute, so it uses ``BaseEntity`` (not
``LinkEntity``).
"""

from __future__ import annotations

from sqlalchemy import BigInteger, Index, String, text
from sqlalchemy.orm import Mapped, mapped_column

from src.db.base import BaseEntity


class UserModelGrant(BaseEntity):
    """Model assignment: who may use which logical models (§2.5)."""

    __tablename__ = "user_model_grant"
    __table_args__ = (
        Index(
            "uq_grant_scope_logical_model_active",
            "scope",
            "scope_id",
            "logical_model_id",
            unique=True,
            postgresql_where=text("is_deleted = false"),
        ),  # No duplicate grant of the same model to the same subject.
        Index(
            "uq_grant_one_default",
            "scope",
            "scope_id",
            unique=True,
            postgresql_where=text("is_default = true AND is_deleted = false"),
        ),  # At most one default model per subject.
        {"comment": "模型分配：谁（用户/部门）可用哪些逻辑模型 + 默认模型"},
    )

    scope: Mapped[str] = mapped_column(
        String(16),
        index=True,
        comment="授予范围 GrantScope：user=用户 | department=部门",
    )
    scope_id: Mapped[int] = mapped_column(
        BigInteger,
        index=True,
        comment="scope=user 指向 sys_user.id；scope=department 指向 sys_department.id",
    )

    logical_model_id: Mapped[int] = mapped_column(
        BigInteger, index=True, comment="逻辑模型 logical_model.id"
    )
    is_default: Mapped[bool] = mapped_column(
        default=False,
        comment="是否该 scope 的默认模型（员工未传 model 时用）",
    )

    remark: Mapped[str | None] = mapped_column(
        String(255), nullable=True, comment="备注"
    )
