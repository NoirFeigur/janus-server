"""Admin department business logic (service layer).

Enforces the referential rules the DB does not (no physical FKs, §0.7): a
referenced ``parent_id`` must exist, a department cannot become its own ancestor
(cycle guard), and a department with active children or members cannot be
deleted.

The transaction is owned by the request-level Unit of Work, not here: this layer
only ``flush()``es. Every successful mutation must invalidate the cross-replica
department-tree cache; that invalidation is registered as an **after-commit
hook** so it fires only once the write lands (a rolled-back request leaves the
cache untouched).

All "bad request" conditions raise ``AppError(request_invalid, 400)``; a missing
target raises the same (opaque, locale-agnostic code — the frontend renders text).
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy.ext.asyncio import AsyncSession
from starlette import status

from src.admin.departments.repository import DepartmentRepository
from src.admin.departments.schemas import DepartmentCreate, DepartmentUpdate
from src.auth.dept_tree_cache import invalidate_department_tree
from src.auth.service import AuthenticatedUser, AuthService, DataScopeFilter
from src.core.query import BatchResult
from src.db.models.identity import Department
from src.db.session import add_after_commit_hook
from src.enums import ErrorCode
from src.exceptions import AppError


class DepartmentService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.repo = DepartmentRepository(session)
        self.auth = AuthService(session)

    async def _scope(self, actor: AuthenticatedUser) -> DataScopeFilter:
        return await self.auth.resolve_data_scope(actor)

    async def _require(self, dept_id: int) -> Department:
        dept = await self.repo.get(dept_id)
        if dept is None:
            raise AppError(ErrorCode.request_invalid, status.HTTP_404_NOT_FOUND)
        return dept

    @staticmethod
    def _in_scope(dept_id: int, scope: DataScopeFilter) -> bool:
        """Whether a department itself is managed by the actor's scope.

        The department resource's scope key is the dept's OWN id, not the generic
        ``create_dept`` audit column other business tables use: the tree IS the
        scope definition, so a ``dept_and_child`` admin manages exactly the
        subtree it can see. Keying off ``create_dept`` would let whoever created
        an out-of-scope department later mutate the scope boundary itself.
        """
        return scope.unrestricted or dept_id in scope.department_ids

    async def _require_visible(
        self, dept_id: int, actor: AuthenticatedUser
    ) -> Department:
        """Fetch a department the actor's scope manages, else opaque 403.

        A missing department and an out-of-scope one collapse to the same opaque
        403 (no "exists but hidden" oracle), matching the user surface.
        """
        scope = await self._scope(actor)
        dept = await self.repo.get(dept_id)
        if dept is None or not self._in_scope(dept_id, scope):
            raise AppError(ErrorCode.auth_forbidden, status.HTTP_403_FORBIDDEN)
        return dept

    async def _require_parent_in_scope(
        self, parent_id: int | None, actor: AuthenticatedUser
    ) -> None:
        """A scoped actor may only place/move a department under a parent it
        manages. ``parent_id IS NULL`` (a root department) is allowed only for an
        unrestricted actor — a scoped actor cannot mint a root it would then be
        unable to manage (no write-outside-scope hole)."""
        scope = await self._scope(actor)
        if scope.unrestricted:
            return
        if parent_id is None or parent_id not in scope.department_ids:
            raise AppError(ErrorCode.auth_forbidden, status.HTTP_403_FORBIDDEN)

    async def _require_parent_exists(self, parent_id: int | None) -> None:
        if parent_id is not None and await self.repo.get(parent_id) is None:
            raise AppError(ErrorCode.request_invalid, status.HTTP_400_BAD_REQUEST)

    async def list_departments(
        self, actor: AuthenticatedUser, *, keyword: str | None = None
    ) -> Sequence[Department]:
        scope = await self._scope(actor)
        departments = await self.repo.list_all()
        if not scope.unrestricted:
            # Narrow to the scope-managed set FIRST so the keyword ancestor walk
            # below can never pull an out-of-scope ancestor back into the result.
            departments = [
                dept for dept in departments if dept.id in scope.department_ids
            ]
        if keyword is None:
            return departments

        needle = keyword.casefold()
        all_depts = {dept.id: dept for dept in departments}
        included_ids: set[int] = set()
        for dept in departments:
            if needle not in dept.name.casefold():
                continue

            cursor: int | None = dept.id
            seen: set[int] = set()
            while cursor is not None and cursor not in seen:
                seen.add(cursor)
                current = all_depts.get(cursor)
                if current is None:
                    break
                included_ids.add(current.id)
                cursor = current.parent_id

        return [dept for dept in departments if dept.id in included_ids]

    async def get_department(
        self, dept_id: int, *, actor: AuthenticatedUser
    ) -> Department:
        return await self._require_visible(dept_id, actor)

    async def create_department(
        self, payload: DepartmentCreate, *, actor: AuthenticatedUser
    ) -> Department:
        await self._require_parent_exists(payload.parent_id)
        await self._require_parent_in_scope(payload.parent_id, actor)
        # Lock the parent row so a concurrent delete_department of the parent
        # blocks until this child insert commits (no orphan under a deleted dept).
        if (
            payload.parent_id is not None
            and await self.repo.get_for_update(payload.parent_id) is None
        ):
            raise AppError(ErrorCode.request_invalid, status.HTTP_400_BAD_REQUEST)
        dept = Department(
            name=payload.name,
            parent_id=payload.parent_id,
            sort_order=payload.sort_order,
            remark=payload.remark,
            created_by=actor.user_id,
            create_dept=actor.department_id,
            updated_by=actor.user_id,
        )
        await self.repo.create(dept)
        await self.session.flush()
        add_after_commit_hook(self.session, invalidate_department_tree)
        return dept

    async def update_department(
        self, dept_id: int, payload: DepartmentUpdate, *, actor: AuthenticatedUser
    ) -> Department:
        dept = await self._require_visible(dept_id, actor)
        values = payload.model_dump(exclude_unset=True)
        if "parent_id" in values:
            await self._validate_reparent(dept_id, values["parent_id"])
            await self._require_parent_in_scope(values["parent_id"], actor)
        values["updated_by"] = actor.user_id
        await self.repo.update(dept, **values)
        await self.session.flush()
        add_after_commit_hook(self.session, invalidate_department_tree)
        return dept

    async def delete_department(self, dept_id: int, *, actor: AuthenticatedUser) -> None:
        await self._require_visible(dept_id, actor)
        # Lock the row, THEN re-check emptiness under the lock: a concurrent
        # create-child / user-attach that locks this same row serializes behind
        # us, so the children/members checks below cannot be invalidated between
        # check and commit (closes the delete-vs-create race).
        dept = await self.repo.get_for_update(dept_id)
        if dept is None:
            raise AppError(ErrorCode.request_invalid, status.HTTP_404_NOT_FOUND)
        if await self.repo.has_active_children(dept_id):
            raise AppError(ErrorCode.request_invalid, status.HTTP_400_BAD_REQUEST)
        if await self.repo.has_active_members(dept_id):
            raise AppError(ErrorCode.request_invalid, status.HTTP_400_BAD_REQUEST)
        dept.updated_by = actor.user_id
        await self.repo.soft_delete(dept)
        await self.session.flush()
        add_after_commit_hook(self.session, invalidate_department_tree)

    async def batch_delete_departments(
        self, ids: Sequence[int], *, actor: AuthenticatedUser
    ) -> BatchResult:
        requested_ids = list(dict.fromkeys(ids))
        scope = await self._scope(actor)
        skipped_ids: list[int] = []
        affected = 0

        for dept_id in requested_ids:
            if not self._in_scope(dept_id, scope):
                skipped_ids.append(dept_id)
                continue
            dept = await self.repo.get_for_update(dept_id)
            if dept is None:
                skipped_ids.append(dept_id)
                continue
            if await self.repo.has_active_children(dept_id):
                skipped_ids.append(dept_id)
                continue
            if await self.repo.has_active_members(dept_id):
                skipped_ids.append(dept_id)
                continue

            dept.updated_by = actor.user_id
            await self.repo.soft_delete(dept)
            affected += 1

        await self.session.flush()
        if affected > 0:
            add_after_commit_hook(self.session, invalidate_department_tree)
        return BatchResult.of(
            requested=len(requested_ids), affected=affected, skipped=skipped_ids
        )

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
