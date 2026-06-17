"""Admin department business logic (service layer).

Owns the transaction boundary (commits) and enforces the referential rules the
DB does not (no physical FKs, §0.7): a referenced ``parent_id`` must exist, a
department cannot become its own ancestor (cycle guard), and a department with
active children or members cannot be deleted.

All "bad request" conditions raise ``AppError(request_invalid, 400)``; a missing
target raises the same (opaque, locale-agnostic code — the frontend renders text).
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy.ext.asyncio import AsyncSession
from starlette import status

from src.admin.departments.repository import DepartmentRepository
from src.admin.departments.schemas import DepartmentCreate, DepartmentUpdate
from src.db.models.identity import Department
from src.enums import ErrorCode
from src.exceptions import AppError


class DepartmentService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.repo = DepartmentRepository(session)

    async def _require(self, dept_id: int) -> Department:
        dept = await self.repo.get(dept_id)
        if dept is None:
            raise AppError(ErrorCode.request_invalid, status.HTTP_404_NOT_FOUND)
        return dept

    async def _require_parent_exists(self, parent_id: int | None) -> None:
        if parent_id is not None and await self.repo.get(parent_id) is None:
            raise AppError(ErrorCode.request_invalid, status.HTTP_400_BAD_REQUEST)

    async def list_departments(self) -> Sequence[Department]:
        return await self.repo.list_all()

    async def get_department(self, dept_id: int) -> Department:
        return await self._require(dept_id)

    async def create_department(
        self, payload: DepartmentCreate, *, actor_id: int
    ) -> Department:
        await self._require_parent_exists(payload.parent_id)
        dept = Department(
            name=payload.name,
            parent_id=payload.parent_id,
            sort_order=payload.sort_order,
            remark=payload.remark,
            created_by=actor_id,
            updated_by=actor_id,
        )
        await self.repo.create(dept)
        await self.session.commit()
        return dept

    async def update_department(
        self, dept_id: int, payload: DepartmentUpdate, *, actor_id: int
    ) -> Department:
        dept = await self._require(dept_id)
        values = payload.model_dump(exclude_unset=True)
        if "parent_id" in values:
            await self._validate_reparent(dept_id, values["parent_id"])
        values["updated_by"] = actor_id
        await self.repo.update(dept, **values)
        await self.session.commit()
        return dept

    async def delete_department(self, dept_id: int, *, actor_id: int) -> None:
        dept = await self._require(dept_id)
        if await self.repo.has_active_children(dept_id):
            raise AppError(ErrorCode.request_invalid, status.HTTP_400_BAD_REQUEST)
        if await self.repo.has_active_members(dept_id):
            raise AppError(ErrorCode.request_invalid, status.HTTP_400_BAD_REQUEST)
        dept.updated_by = actor_id
        await self.repo.soft_delete(dept)
        await self.session.commit()

    async def _validate_reparent(self, dept_id: int, new_parent: int | None) -> None:
        """Reject a parent that doesn't exist, is self, or is a descendant."""
        if new_parent is None:
            return
        if new_parent == dept_id:
            raise AppError(ErrorCode.request_invalid, status.HTTP_400_BAD_REQUEST)
        await self._require_parent_exists(new_parent)
        # Walk the ancestor chain of new_parent; if dept_id appears, it's a cycle.
        all_depts = {d.id: d for d in await self.repo.list_all()}
        cursor: int | None = new_parent
        seen: set[int] = set()
        while cursor is not None and cursor not in seen:
            if cursor == dept_id:
                raise AppError(ErrorCode.request_invalid, status.HTTP_400_BAD_REQUEST)
            seen.add(cursor)
            parent = all_depts.get(cursor)
            cursor = parent.parent_id if parent else None
