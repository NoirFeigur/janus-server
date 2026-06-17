"""Declarative base and shared entity base classes (data-model §0.3).

- ``Base``       — the single declarative base; Alembic autogenerate scans its
  ``metadata``.
- ``BaseEntity`` — config/business tables: snowflake PK + soft-delete + audit
  columns (creator + creator-department + timestamps).
- ``LogEntity``  — append-only streams (usage/audit): snowflake PK + created_at
  only (never updated, never soft-deleted).
- ``LinkEntity`` — many-to-many association tables: snowflake PK + created_at,
  physically deleted (no soft-delete), no audit columns (§1.5 RBAC).

All timestamps are ``timestamptz`` stored in UTC, defaulted by the database
(``func.now()``), not the application clock.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from src.core.snowflake import next_id


class Base(DeclarativeBase):
    """Single declarative base. Alembic autogenerate targets ``Base.metadata``."""


class BaseEntity(Base):
    """Config/business tables: soft-deletable, audited, snowflake PK."""

    __abstract__ = True

    id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        autoincrement=False,
        default=next_id,
        comment="主键：雪花 ID，应用层分配（非自增）",
    )
    is_deleted: Mapped[bool] = mapped_column(
        default=False, comment="软删标记：true=已删除（逻辑删除，不物理删行）"
    )
    created_by: Mapped[int | None] = mapped_column(
        BigInteger,
        nullable=True,
        comment="创建人 sys_user.id（逻辑引用，无物理外键）；null=系统操作",
    )
    create_dept: Mapped[int | None] = mapped_column(
        BigInteger,
        nullable=True,
        index=True,
        comment="创建部门 sys_department.id（逻辑引用），用于数据权限过滤",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        comment="创建时间（timestamptz，UTC，由数据库 now() 生成）",
    )
    updated_by: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, comment="最后更新人 sys_user.id（逻辑引用）"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        comment="最后更新时间（timestamptz，UTC，更新时自动刷新）",
    )


class LogEntity(Base):
    """Append-only streams (usage/audit): insert-only, snowflake PK."""

    __abstract__ = True

    id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        autoincrement=False,
        default=next_id,
        comment="主键：雪花 ID，应用层分配（非自增）",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        comment="创建时间（timestamptz，UTC，由数据库 now() 生成）",
    )


class LinkEntity(Base):
    """M:N association tables: snowflake PK + created_at, physically deleted.

    No ``is_deleted`` (associations are physically added/removed, never
    soft-deleted), no ``updated_*`` (only insert/delete), no ``created_by``
    (authorization accountability is deliberately out of scope — §1.5).
    Uniqueness/dedup is enforced per-table via ``UniqueConstraint``.
    """

    __abstract__ = True

    id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        autoincrement=False,
        default=next_id,
        comment="主键：雪花 ID，应用层分配（非自增）",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        comment="创建时间（timestamptz，UTC，由数据库 now() 生成）",
    )
