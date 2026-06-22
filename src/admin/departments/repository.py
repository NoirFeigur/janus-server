"""Admin department data access (repository layer)."""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import select

from src.db.models.identity import Department, User
from src.db.repository import BaseRepository


class DepartmentRepository(BaseRepository[Department]):
    model = Department

    async def get_for_update(self, dept_id: int) -> Department | None:
        """Fetch a non-deleted department and hold a row lock until commit.

        ``SELECT ... FOR UPDATE`` serializes the delete-vs-create race (B-级一致性):
        a concurrent ``create_department`` child-insert / user-attach that locks
        the same parent row blocks until this transaction commits, so a parent can
        never be soft-deleted in the gap between its emptiness check and the
        commit. On SQLite (tests) ``FOR UPDATE`` is a documented no-op, so the
        query degrades to a plain read without error.
        """
        stmt = (
            select(Department)
            .where(Department.id == dept_id)
            .where(Department.is_deleted.is_(False))
            .with_for_update()
        )
        dept: Department | None = await self.session.scalar(stmt)
        return dept

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
