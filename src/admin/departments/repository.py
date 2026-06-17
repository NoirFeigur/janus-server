"""Admin department data access (repository layer)."""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import select

from src.db.models.identity import Department, User
from src.db.repository import BaseRepository


class DepartmentRepository(BaseRepository[Department]):
    model = Department

    async def has_active_children(self, dept_id: int) -> bool:
        """Whether any non-deleted department has this one as parent."""
        stmt = (
            select(Department.id)
            .where(Department.parent_id == dept_id)
            .where(Department.is_deleted.is_(False))
            .limit(1)
        )
        return await self.session.scalar(stmt) is not None

    async def has_active_members(self, dept_id: int) -> bool:
        """Whether any non-deleted user belongs to this department."""
        stmt = (
            select(User.id)
            .where(User.department_id == dept_id)
            .where(User.is_deleted.is_(False))
            .limit(1)
        )
        return await self.session.scalar(stmt) is not None

    async def list_all(self) -> Sequence[Department]:
        """All non-deleted departments ordered for tree assembly."""
        stmt = (
            select(Department)
            .where(Department.is_deleted.is_(False))
            .order_by(Department.sort_order, Department.id)
        )
        result = await self.session.scalars(stmt)
        return result.all()
