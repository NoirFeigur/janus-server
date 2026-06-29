"""Identity & RBAC aggregate (data-model batch 1 + 1.5).

Two orthogonal permission axes live here:
- Identity: ``User`` / ``Department`` / ``UserOAuth`` — who the principal is.
- RBAC: ``Role`` / ``Menu`` + 2 link tables — admin-console operation rights.

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
        {"comment": "系统用户：员工权威档案，从 HR 同步"},
    )

    # Employee numbers: HR rolled out new numbers but WeCom UserID still uses the
    # old ones, so the two are split into independent columns (§1.1).
    employee_no: Mapped[str] = mapped_column(
        String(64),
        index=True,
        comment="新工号（HR 权威主键）",
    )
    legacy_employee_no: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        index=True,
        comment="旧工号==企微 UserID；仅老员工有",
    )

    username: Mapped[str] = mapped_column(
        String(64),
        comment="登录名（账密登录用）",
    )
    real_name: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="真实姓名"
    )
    email: Mapped[str | None] = mapped_column(
        String(255), nullable=True, index=True
    )
    mobile: Mapped[str | None] = mapped_column(
        String(32), nullable=True, index=True, comment="手机号"
    )

    # Column named ``password`` but stores an argon2/bcrypt hash, never plaintext;
    # only admins have one (SSO-only users keep it null). Never in any XxxRead.
    password: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
        comment="密码哈希（argon2，绝不存明文）；SSO 用户为空",
    )

    department_id: Mapped[int | None] = mapped_column(
        BigInteger,
        nullable=True,
        index=True,
        comment="所属部门 sys_department.id",
    )

    avatar: Mapped[int | None] = mapped_column(
        BigInteger,
        nullable=True,
        comment="头像附件 sys_attach.id",
    )

    status: Mapped[str] = mapped_column(
        String(16),
        default="active",
        index=True,
        comment="状态 UserStatus",
    )

    # Locale preference (G16): outbound-message rendering + frontend default seed.
    preferred_locale: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        server_default="zh-CN",
        comment="语言偏好（外发消息渲染 + 前端默认 locale）",
    )

    remark: Mapped[str | None] = mapped_column(String(255), nullable=True)


class Department(BaseEntity):
    """Organization department tree (adjacency list, §1.2)."""

    __tablename__ = "sys_department"
    __table_args__ = (
        Index(
            "uq_dept_external_active",
            "external_id",
            unique=True,
            postgresql_where=text("external_id IS NOT NULL AND is_deleted = false"),
        ),
        {"comment": "系统部门：组织部门树（邻接表）"},
    )

    name: Mapped[str] = mapped_column(String(128), comment="部门名称")
    parent_id: Mapped[int | None] = mapped_column(
        BigInteger,
        nullable=True,
        index=True,
        comment="父部门 id；顶级为空",
    )
    external_id: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        index=True,
        comment="HR/企微 部门 id（同步映射用）",
    )
    sort_order: Mapped[int] = mapped_column(default=0, comment="同级排序")
    remark: Mapped[str | None] = mapped_column(String(255), nullable=True)


class UserOAuth(BaseEntity):
    """Third-party login identity snapshot (WeCom etc., §1.3)."""

    __tablename__ = "sys_user_oauth"
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
        {"comment": "用户第三方登录身份快照（企微等）"},
    )

    user_id: Mapped[int] = mapped_column(
        BigInteger,
        index=True,
        comment="用户 sys_user.id（仅硬匹配命中时落库）",
    )
    source: Mapped[str] = mapped_column(
        String(32),
        index=True,
        comment="身份来源 OAuthSource",
    )
    uuid: Mapped[str] = mapped_column(
        String(64),
        index=True,
        comment="第三方用户 id（企微 UserID）",
    )

    # Non-authoritative profile snapshot returned by the provider.
    nickname: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="昵称（第三方快照，非权威）"
    )
    avatar: Mapped[str | None] = mapped_column(
        String(1000), nullable=True, comment="头像 URL（第三方快照）"
    )
    email: Mapped[str | None] = mapped_column(
        String(255), nullable=True, comment="邮箱（第三方快照）"
    )
    gender: Mapped[str | None] = mapped_column(
        String(16), nullable=True, comment="性别（第三方快照）"
    )

    raw: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB,
        nullable=True,
        comment="脱敏白名单档案字段；绝不存 access_token/user_ticket",
    )


class Role(BaseEntity):
    """Admin-console role: a named bundle of menu/operation permissions (§1.5.1)."""

    __tablename__ = "sys_role"
    __table_args__ = (
        Index(
            "uq_role_code_active",
            "code",
            unique=True,
            postgresql_where=text("is_deleted = false"),
        ),
        {"comment": "系统角色：后台角色（菜单/操作权限集合）"},
    )

    name: Mapped[str] = mapped_column(
        String(64), comment='角色显示名，如 "平台管理员"'
    )
    code: Mapped[str] = mapped_column(
        String(64),
        index=True,
        comment='角色标识，如 "platform_admin"',
    )
    sort_order: Mapped[int] = mapped_column(default=0, comment="排序")
    status: Mapped[str] = mapped_column(
        String(16),
        default="active",
        comment="状态 ActiveStatus",
    )

    # Frontend tree-checkbox linkage toggle (pure UI behavior).
    menu_check_strictly: Mapped[bool] = mapped_column(
        default=True, comment="菜单树父子勾选是否严格关联"
    )

    remark: Mapped[str | None] = mapped_column(String(255), nullable=True)


class Menu(BaseEntity):
    """Unified menu/button/API permission node (§1.5.2)."""

    __tablename__ = "sys_menu"
    __table_args__ = ({"comment": "系统菜单：统一的菜单/按钮/API 权限节点"},)

    name: Mapped[str] = mapped_column(
        String(64),
        comment="i18n key（如 menu.system.user），前端翻译",
    )
    parent_id: Mapped[int | None] = mapped_column(
        BigInteger,
        nullable=True,
        index=True,
        comment="父节点 id；顶级为空",
    )
    menu_type: Mapped[str] = mapped_column(
        String(8),
        comment="菜单类型 MenuType",
    )
    perms: Mapped[str | None] = mapped_column(
        String(128),
        nullable=True,
        index=True,
        comment="权限码，如 pool:key:add",
    )
    path: Mapped[str | None] = mapped_column(
        String(255), nullable=True, comment="前端路由"
    )
    component: Mapped[str | None] = mapped_column(
        String(255), nullable=True, comment="前端组件路径"
    )
    query_param: Mapped[str | None] = mapped_column(
        String(255), nullable=True, comment="路由查询参数"
    )
    is_frame: Mapped[bool] = mapped_column(default=False, comment="是否外链")
    is_cache: Mapped[bool] = mapped_column(
        default=True, comment="是否 keep-alive 缓存"
    )
    icon: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="图标"
    )
    sort_order: Mapped[int] = mapped_column(default=0, comment="排序")
    visible: Mapped[bool] = mapped_column(
        default=True, comment="是否显示；隐藏菜单仍授权"
    )
    status: Mapped[str] = mapped_column(
        String(16),
        default="active",
        comment="状态 ActiveStatus",
    )
    remark: Mapped[str | None] = mapped_column(
        String(255), nullable=True, comment="name 是 i18n key，此处给开发者读"
    )


class UserRole(LinkEntity):
    """User <-> Role (§1.5.3)."""

    __tablename__ = "sys_user_role"
    __table_args__ = (
        UniqueConstraint("user_id", "role_id", name="uq_user_role"),
        {"comment": "用户-角色关联表"},
    )

    user_id: Mapped[int] = mapped_column(
        BigInteger, index=True, comment="用户 sys_user.id"
    )
    role_id: Mapped[int] = mapped_column(
        BigInteger, index=True, comment="角色 sys_role.id"
    )


class RoleMenu(LinkEntity):
    """Role <-> Menu permission (§1.5.4)."""

    __tablename__ = "sys_role_menu"
    __table_args__ = (
        UniqueConstraint("role_id", "menu_id", name="uq_role_menu"),
        {"comment": "角色-菜单权限关联表"},
    )

    role_id: Mapped[int] = mapped_column(
        BigInteger, index=True, comment="角色 sys_role.id"
    )
    menu_id: Mapped[int] = mapped_column(
        BigInteger, index=True, comment="菜单 sys_menu.id"
    )
