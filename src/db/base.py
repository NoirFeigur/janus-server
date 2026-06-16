"""Declarative base and shared entity base classes (data-model §0.3).

- ``Base``       — the single declarative base; Alembic autogenerate scans its
  ``metadata``.
- ``BaseEntity`` — config/business tables: snowflake PK + soft-delete + audit
  columns (6 public fields).
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
        BigInteger, primary_key=True, autoincrement=False, default=next_id
    )  # Snowflake id, application-assigned (§0.2).
    is_deleted: Mapped[bool] = mapped_column(default=False)  # Soft-delete flag.
    created_by: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True
    )  # sys_user.id soft reference; null = system action.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class LogEntity(Base):
    """Append-only streams (usage/audit): insert-only, snowflake PK."""

    __abstract__ = True

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=False, default=next_id
    )  # Snowflake id, application-assigned (§0.2).
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
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
        BigInteger, primary_key=True, autoincrement=False, default=next_id
    )  # Snowflake id, application-assigned (§0.2).
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
