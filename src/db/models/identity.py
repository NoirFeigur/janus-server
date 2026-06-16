"""Identity & RBAC aggregate (data-model batch 1 + 1.5).

Two orthogonal permission axes live here:
- Identity: ``User`` / ``Department`` / ``UserOAuth`` — who the principal is.
- RBAC: ``Role`` / ``Menu`` + 3 link tables — admin-console operation rights.

AI-resource consumption rights (``UserModelGrant``) are a *separate* axis and
live in ``grant.py``.

No physical foreign keys anywhere (§0.7): association columns store the target
snowflake id and carry an index, but referential integrity is maintained in the
service layer.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import BigInteger, Index, String, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from src.db.base import BaseEntity, LinkEntity


class User(BaseEntity):
    """Authoritative employee profile, synced from HR (§1.1)."""

    __tablename__ = "sys_user"

    # Employee numbers: HR rolled out new numbers but WeCom UserID still uses the
    # old ones, so the two are split into independent columns (§1.1).
    employee_no: Mapped[str] = mapped_column(
        String(64), index=True
    )  # New employee no, HR authoritative key (NOT NULL).
    legacy_employee_no: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True
    )  # Old employee no == WeCom UserID; only legacy employees have one.

    username: Mapped[str] = mapped_column(
        String(64)
    )  # Login key (password login); NOT NULL, business-unique.
    real_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    mobile: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)

    # Column named ``password`` but stores an argon2/bcrypt hash, never plaintext;
    # only admins have one (SSO-only users keep it null). Never in any XxxRead.
    password: Mapped[str | None] = mapped_column(String(255), nullable=True)

    department_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, index=True
    )  # -> sys_department, logical reference (no FK).

    status: Mapped[str] = mapped_column(
        String(16), default="active", index=True
    )  # UserStatus: active | disabled.

    # Locale preference (G16): outbound-message rendering + frontend default seed.
    preferred_locale: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="zh-CN"
    )

    remark: Mapped[str | None] = mapped_column(String(255), nullable=True)

    __table_args__ = (
        Index(
            "uq_user_username_active",
            "username",
            unique=True,
            postgresql_where=text("is_deleted = false"),
        ),
        Index(
            "uq_user_employee_no_active",
            "employee_no",
            unique=True,
            postgresql_where=text("is_deleted = false"),
        ),
        Index(
            "uq_user_legacy_no_active",
            "legacy_employee_no",
            unique=True,
            postgresql_where=text(
                "legacy_employee_no IS NOT NULL AND is_deleted = false"
            ),
        ),
    )


class Department(BaseEntity):
    """Organization department tree (adjacency list, §1.2)."""

    __tablename__ = "sys_department"

    name: Mapped[str] = mapped_column(String(128))
    parent_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, index=True
    )  # -> self; top-level is null.
    external_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True
    )  # HR/WeCom department id, used for sync mapping.
    sort_order: Mapped[int] = mapped_column(default=0)  # Sibling ordering.
    remark: Mapped[str | None] = mapped_column(String(255), nullable=True)

    __table_args__ = (
        Index(
            "uq_dept_external_active",
            "external_id",
            unique=True,
            postgresql_where=text("external_id IS NOT NULL AND is_deleted = false"),
        ),
    )


class UserOAuth(BaseEntity):
    """Third-party login identity snapshot (WeCom etc., §1.3)."""

    __tablename__ = "sys_user_oauth"

    user_id: Mapped[int] = mapped_column(
        BigInteger, index=True
    )  # -> sys_user, NOT NULL (only persisted on a hard-match hit).
    source: Mapped[str] = mapped_column(
        String(32), index=True
    )  # OAuthSource: wecom | ...
    uuid: Mapped[str] = mapped_column(
        String(64), index=True
    )  # Third-party user id (WeCom UserID).

    # Non-authoritative profile snapshot returned by the provider.
    nickname: Mapped[str | None] = mapped_column(String(64), nullable=True)
    avatar: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    gender: Mapped[str | None] = mapped_column(String(16), nullable=True)

    raw: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, nullable=True
    )  # Sanitized whitelist profile fields only; never access_token/user_ticket.

    __table_args__ = (
        Index(
            "uq_oauth_source_uuid_active",
            "source",
            "uuid",
            unique=True,
            postgresql_where=text("is_deleted = false"),
        ),
        Index(
            "uq_oauth_user_source_active",
            "user_id",
            "source",
            unique=True,
            postgresql_where=text("is_deleted = false"),
        ),  # One identity per (user, source).
    )


class Role(BaseEntity):
    """Admin-console role with orthogonal data-scope (§1.5.1)."""

    __tablename__ = "sys_role"

    name: Mapped[str] = mapped_column(String(64))  # Display name, e.g. "平台管理员".
    code: Mapped[str] = mapped_column(
        String(64), index=True
    )  # Role identifier, e.g. "platform_admin".
    sort_order: Mapped[int] = mapped_column(default=0)
    status: Mapped[str] = mapped_column(
        String(16), default="active"
    )  # ActiveStatus: active | disabled.

    # Data-permission scope (orthogonal axis) — RuoYi 6-tier.
    data_scope: Mapped[str] = mapped_column(String(16), default="self")
    # DataScope: all | custom (joins sys_role_dept) | dept | dept_and_child
    #          | self | dept_and_child_or_self.

    # Frontend tree-checkbox linkage toggles (pure UI behavior).
    menu_check_strictly: Mapped[bool] = mapped_column(default=True)
    dept_check_strictly: Mapped[bool] = mapped_column(default=True)

    remark: Mapped[str | None] = mapped_column(String(255), nullable=True)

    __table_args__ = (
        Index(
            "uq_role_code_active",
            "code",
            unique=True,
            postgresql_where=text("is_deleted = false"),
        ),
    )


class Menu(BaseEntity):
    """Unified menu/button/API permission node (§1.5.2)."""

    __tablename__ = "sys_menu"

    name: Mapped[str] = mapped_column(
        String(64)
    )  # i18n key (e.g. menu.system.user), translated by frontend; not display text (G16).
    parent_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, index=True
    )  # -> self; top-level is null.
    menu_type: Mapped[str] = mapped_column(
        String(8)
    )  # MenuType: catalog | menu | button.
    perms: Mapped[str | None] = mapped_column(
        String(128), nullable=True, index=True
    )  # Permission code, e.g. pool:key:add (button/API).
    path: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )  # Frontend route (menu).
    component: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )  # Frontend component path (menu).
    query_param: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_frame: Mapped[bool] = mapped_column(default=False)  # External link.
    is_cache: Mapped[bool] = mapped_column(default=True)  # keep-alive cache.
    icon: Mapped[str | None] = mapped_column(String(64), nullable=True)
    sort_order: Mapped[int] = mapped_column(default=0)
    visible: Mapped[bool] = mapped_column(
        default=True
    )  # Hidden menu still authorizes, just not shown.
    status: Mapped[str] = mapped_column(
        String(16), default="active"
    )  # ActiveStatus: active | disabled.
    remark: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )  # Developer-readable note (name is an opaque i18n key).


class UserRole(LinkEntity):
    """User <-> Role (§1.5.3)."""

    __tablename__ = "sys_user_role"

    user_id: Mapped[int] = mapped_column(BigInteger, index=True)  # -> sys_user.
    role_id: Mapped[int] = mapped_column(BigInteger, index=True)  # -> sys_role.

    __table_args__ = (UniqueConstraint("user_id", "role_id", name="uq_user_role"),)


class RoleMenu(LinkEntity):
    """Role <-> Menu permission (§1.5.4)."""

    __tablename__ = "sys_role_menu"

    role_id: Mapped[int] = mapped_column(BigInteger, index=True)  # -> sys_role.
    menu_id: Mapped[int] = mapped_column(BigInteger, index=True)  # -> sys_menu.

    __table_args__ = (UniqueConstraint("role_id", "menu_id", name="uq_role_menu"),)


class RoleDept(LinkEntity):
    """Role <-> Department, only for data_scope=custom (§1.5.5).

    Column named ``dept_id`` (not ``department_id``) by deliberate exception:
    RBAC data-scope terminology follows RuoYi convention (§1.5 naming note).
    """

    __tablename__ = "sys_role_dept"

    role_id: Mapped[int] = mapped_column(BigInteger, index=True)  # -> sys_role.
    dept_id: Mapped[int] = mapped_column(
        BigInteger, index=True
    )  # -> sys_department (RuoYi-style short name).

    __table_args__ = (UniqueConstraint("role_id", "dept_id", name="uq_role_dept"),)
