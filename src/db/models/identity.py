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
        comment="新工号：HR 权威主键（非空）",
    )
    legacy_employee_no: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        index=True,
        comment="旧工号==企微 UserID；仅老员工有",
    )

    username: Mapped[str] = mapped_column(
        String(64),
        comment="登录名（账密登录用）：非空，业务唯一",
    )
    real_name: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="真实姓名"
    )
    email: Mapped[str | None] = mapped_column(
        String(255), nullable=True, index=True, comment="邮箱"
    )
    mobile: Mapped[str | None] = mapped_column(
        String(32), nullable=True, index=True, comment="手机号"
    )

    # Column named ``password`` but stores an argon2/bcrypt hash, never plaintext;
    # only admins have one (SSO-only users keep it null). Never in any XxxRead.
    password: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
        comment="密码哈希（argon2，绝不存明文）；仅管理员有，SSO 用户为空",
    )

    department_id: Mapped[int | None] = mapped_column(
        BigInteger,
        nullable=True,
        index=True,
        comment="所属部门 sys_department.id（逻辑引用，无物理外键）",
    )

    status: Mapped[str] = mapped_column(
        String(16),
        default="active",
        index=True,
        comment="状态 UserStatus：active=在职 | disabled=停用/离职",
    )

    # Locale preference (G16): outbound-message rendering + frontend default seed.
    preferred_locale: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        server_default="zh-CN",
        comment="语言偏好（G16）：外发消息渲染 + 前端默认 locale 种子",
    )

    remark: Mapped[str | None] = mapped_column(
        String(255), nullable=True, comment="备注"
    )


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
        comment="父部门 id（指向自身）；顶级为空",
    )
    external_id: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        index=True,
        comment="HR/企微 部门 id，用于同步映射",
    )
    sort_order: Mapped[int] = mapped_column(default=0, comment="同级排序")
    remark: Mapped[str | None] = mapped_column(
        String(255), nullable=True, comment="备注"
    )


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
        comment="用户 sys_user.id（非空，仅硬匹配命中时落库）",
    )
    source: Mapped[str] = mapped_column(
        String(32),
        index=True,
        comment="身份来源 OAuthSource：wecom | ...",
    )
    uuid: Mapped[str] = mapped_column(
        String(64),
        index=True,
        comment="第三方用户 id（企微 UserID）",
    )

    # Non-authoritative profile snapshot returned by the provider.
    nickname: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="昵称（第三方返回快照，非权威）"
    )
    avatar: Mapped[str | None] = mapped_column(
        String(1000), nullable=True, comment="头像 URL（第三方返回快照）"
    )
    email: Mapped[str | None] = mapped_column(
        String(255), nullable=True, comment="邮箱（第三方返回快照）"
    )
    gender: Mapped[str | None] = mapped_column(
        String(16), nullable=True, comment="性别（第三方返回快照）"
    )

    raw: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB,
        nullable=True,
        comment="脱敏白名单档案字段；绝不存 access_token/user_ticket",
    )


class Role(BaseEntity):
    """Admin-console role with orthogonal data-scope (§1.5.1)."""

    __tablename__ = "sys_role"
    __table_args__ = (
        Index(
            "uq_role_code_active",
            "code",
            unique=True,
            postgresql_where=text("is_deleted = false"),
        ),
        {"comment": "系统角色：后台角色 + 正交数据权限范围"},
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
        comment="状态 ActiveStatus：active | disabled",
    )

    # Data-permission scope (orthogonal axis) — RuoYi 6-tier.
    data_scope: Mapped[str] = mapped_column(
        String(16),
        default="self",
        comment=(
            "数据权限范围 DataScope：all=全部 | custom=自定义(关联 sys_role_dept) "
            "| dept=本部门 | dept_and_child=本部门及子 | self=仅本人 "
            "| dept_and_child_or_self=部门子树+本人"
        ),
    )

    # Frontend tree-checkbox linkage toggles (pure UI behavior).
    menu_check_strictly: Mapped[bool] = mapped_column(
        default=True, comment="菜单树父子勾选是否严格关联（纯前端 UI 行为）"
    )
    dept_check_strictly: Mapped[bool] = mapped_column(
        default=True, comment="部门树父子勾选是否严格关联（纯前端 UI 行为）"
    )

    remark: Mapped[str | None] = mapped_column(
        String(255), nullable=True, comment="备注"
    )


class Menu(BaseEntity):
    """Unified menu/button/API permission node (§1.5.2)."""

    __tablename__ = "sys_menu"
    __table_args__ = ({"comment": "系统菜单：统一的菜单/按钮/API 权限节点"},)

    name: Mapped[str] = mapped_column(
        String(64),
        comment="i18n key（如 menu.system.user），前端翻译，非展示文本（G16）",
    )
    parent_id: Mapped[int | None] = mapped_column(
        BigInteger,
        nullable=True,
        index=True,
        comment="父节点 id（指向自身）；顶级为空",
    )
    menu_type: Mapped[str] = mapped_column(
        String(8),
        comment="菜单类型 MenuType：catalog=目录 | menu=页面 | button=按钮/权限点",
    )
    perms: Mapped[str | None] = mapped_column(
        String(128),
        nullable=True,
        index=True,
        comment="权限码，如 pool:key:add（按钮/API）",
    )
    path: Mapped[str | None] = mapped_column(
        String(255), nullable=True, comment="前端路由（menu 类型）"
    )
    component: Mapped[str | None] = mapped_column(
        String(255), nullable=True, comment="前端组件路径（menu 类型）"
    )
    query_param: Mapped[str | None] = mapped_column(
        String(255), nullable=True, comment="路由查询参数"
    )
    is_frame: Mapped[bool] = mapped_column(
        default=False, comment="是否外链"
    )
    is_cache: Mapped[bool] = mapped_column(
        default=True, comment="是否 keep-alive 缓存"
    )
    icon: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="图标"
    )
    sort_order: Mapped[int] = mapped_column(default=0, comment="排序")
    visible: Mapped[bool] = mapped_column(
        default=True, comment="是否显示；隐藏菜单仍授权，只是不展示"
    )
    status: Mapped[str] = mapped_column(
        String(16),
        default="active",
        comment="状态 ActiveStatus：active | disabled",
    )
    remark: Mapped[str | None] = mapped_column(
        String(255), nullable=True, comment="备注（name 是不透明 i18n key，此处给开发者读）"
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


class RoleDept(LinkEntity):
    """Role <-> Department, only for data_scope=custom (§1.5.5).

    Column named ``dept_id`` (not ``department_id``) by deliberate exception:
    RBAC data-scope terminology follows RuoYi convention (§1.5 naming note).
    """

    __tablename__ = "sys_role_dept"
    __table_args__ = (
        UniqueConstraint("role_id", "dept_id", name="uq_role_dept"),
        {"comment": "角色-部门关联表（仅 data_scope=custom 时使用）"},
    )

    role_id: Mapped[int] = mapped_column(
        BigInteger, index=True, comment="角色 sys_role.id"
    )
    dept_id: Mapped[int] = mapped_column(
        BigInteger,
        index=True,
        comment="部门 sys_department.id（RuoYi 风格短名）",
    )
