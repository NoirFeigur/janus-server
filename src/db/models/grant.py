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

    scope: Mapped[str] = mapped_column(
        String(16), index=True
    )  # GrantScope: user | department.
    scope_id: Mapped[int] = mapped_column(
        BigInteger, index=True
    )  # scope=user -> sys_user.id; scope=department -> sys_department.id.

    logical_model_id: Mapped[int] = mapped_column(
        BigInteger, index=True
    )  # -> logical_model.
    is_default: Mapped[bool] = mapped_column(
        default=False
    )  # Default model for this scope when the employee omits ``model``.

    remark: Mapped[str | None] = mapped_column(String(255), nullable=True)

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
    )
