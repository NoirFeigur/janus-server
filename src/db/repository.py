"""Generic base repository (README: db layer, four-layer discipline).

``BaseRepository[Model]`` provides reusable CRUD against any ``BaseEntity``
subclass. Per-domain repositories subclass it and add their own queries.

Discipline:
- The repository receives an injected ``AsyncSession``; it never builds an
  engine or session itself.
- Soft-deletable reads exclude ``is_deleted = true`` rows by default.
- ``flush`` (not ``commit``) is used so the caller/service owns the transaction
  boundary; the snowflake id is assigned on flush via the column default.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Generic, TypeVar

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.base import BaseEntity

Model = TypeVar("Model", bound=BaseEntity)


class BaseRepository(Generic[Model]):
    """Reusable CRUD for a single ``BaseEntity`` subclass."""

    model: type[Model]

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, id_: int, *, include_deleted: bool = False) -> Model | None:
        """Fetch one row by primary key, excluding soft-deleted by default."""
        stmt = select(self.model).where(self.model.id == id_)
        if not include_deleted:
            stmt = stmt.where(self.model.is_deleted.is_(False))
        instance: Model | None = await self.session.scalar(stmt)
        return instance

    async def list(
        self,
        *,
        include_deleted: bool = False,
        limit: int | None = None,
        offset: int | None = None,
    ) -> Sequence[Model]:
        """List rows, excluding soft-deleted by default."""
        stmt = select(self.model)
        if not include_deleted:
            stmt = stmt.where(self.model.is_deleted.is_(False))
        if offset is not None:
            stmt = stmt.offset(offset)
        if limit is not None:
            stmt = stmt.limit(limit)
        result = await self.session.scalars(stmt)
        return result.all()

    async def create(self, instance: Model) -> Model:
        """Add a new instance and flush so its snowflake id is assigned."""
        self.session.add(instance)
        await self.session.flush()
        return instance

    async def update(self, instance: Model, **values: Any) -> Model:
        """Apply attribute updates to a managed instance and flush."""
        for key, value in values.items():
            setattr(instance, key, value)
        await self.session.flush()
        return instance

    async def soft_delete(self, instance: Model) -> Model:
        """Mark an instance soft-deleted (logical delete, never physical)."""
        instance.is_deleted = True
        await self.session.flush()
        return instance
