"""Admin role data access (repository layer).

Beyond base CRUD on ``Role``, manages the two link tables a role owns:
``RoleMenu`` (permission grants) and ``RoleDept`` (custom data-scope depts).
Links are physically replaced (LinkEntity has no soft-delete): assignment is a
delete-all-then-insert within the caller's transaction.

The resolved scope is consumed via the :class:`~src.db.scope.DataScope`
structural Protocol, not the concrete ``auth.service.DataScopeFilter`` — a
repository is a ``db``-layer citizen and must not import upward from ``auth``.
``DataScopeFilter`` satisfies the Protocol structurally, so callers pass it
unchanged.
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm.attributes import InstrumentedAttribute
from sqlalchemy.sql.elements import ColumnElement

from src.db.models.identity import Department, Menu, Role, RoleDept, RoleMenu, UserRole
from src.db.repository import BaseRepository
from src.db.scope import DataScope, data_scope_predicate


class RoleRepository(BaseRepository[Role]):
    model = Role

    def _keyword_predicate(self, keyword: str | None) -> ColumnElement[bool] | None:
        if keyword is None:
            return None
        normalized = keyword.strip().lower()
        if not normalized:
            return None
        pattern = f"%{normalized}%"
        return or_(
            func.lower(Role.name).like(pattern),
            func.lower(Role.code).like(pattern),
        )

    async def get_by_code(self, code: str) -> Role | None:
        """Active-or-disabled, non-deleted role by unique code (uniqueness guard)."""
        stmt = (
            select(Role).where(Role.code == code).where(Role.is_deleted.is_(False))
        )
        role: Role | None = await self.session.scalar(stmt)
        return role

    def _scope_predicate(
        self, scope: DataScope, *, actor_id: int
    ) -> ColumnElement[bool] | None:
        """Generic management-resource data-scope predicate (audit columns).

        Delegates to the shared :func:`data_scope_predicate` so every
        audit-column-owned business domain (role / api_key / channel / grant /
        quota) gets the identical visibility rule.
        """
        return data_scope_predicate(Role, scope, actor_id=actor_id)

    async def list_in_scope(
        self,
        scope: DataScope,
        *,
        actor_id: int,
        keyword: str | None = None,
        sort: tuple[InstrumentedAttribute[object], bool] | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> Sequence[Role]:
        """List non-deleted roles visible under generic data scope."""
        stmt = select(Role).where(Role.is_deleted.is_(False))
        predicate = self._scope_predicate(scope, actor_id=actor_id)
        if predicate is not None:
            stmt = stmt.where(predicate)
        keyword_predicate = self._keyword_predicate(keyword)
        if keyword_predicate is not None:
            stmt = stmt.where(keyword_predicate)
        if sort is None:
            stmt = stmt.order_by(Role.sort_order, Role.id)
        else:
            sort_column, descending = sort
            stmt = stmt.order_by(
                sort_column.desc() if descending else sort_column.asc(), Role.id
            )
        if offset is not None:
            stmt = stmt.offset(offset)
        if limit is not None:
            stmt = stmt.limit(limit)
        result = await self.session.scalars(stmt)
        return result.all()

    async def count_in_scope(
        self,
        scope: DataScope,
        *,
        actor_id: int,
        keyword: str | None = None,
    ) -> int:
        """Count non-deleted roles visible under generic data scope."""
        stmt = select(func.count()).select_from(Role).where(Role.is_deleted.is_(False))
        predicate = self._scope_predicate(scope, actor_id=actor_id)
        if predicate is not None:
            stmt = stmt.where(predicate)
        keyword_predicate = self._keyword_predicate(keyword)
        if keyword_predicate is not None:
            stmt = stmt.where(keyword_predicate)
        total = await self.session.scalar(stmt)
        return int(total or 0)

    def is_visible(self, role: Role, scope: DataScope, *, actor_id: int) -> bool:
        """In-Python scope check for one role."""
        if scope.unrestricted:
            return True
        if role.create_dept is not None and role.create_dept in scope.department_ids:
            return True
        return bool(scope.include_self and role.created_by == actor_id)

    async def list_by_ids(self, role_ids: Sequence[int]) -> Sequence[Role]:
        """Live (non-deleted) role rows for a set of ids (batch dominance guard).

        Returns rows in arbitrary order; the caller maps by ``id``. Missing or
        already-deleted ids are simply absent — the caller treats them as
        un-dominatable (skipped).
        """
        if not role_ids:
            return []
        stmt = (
            select(Role)
            .where(Role.id.in_(role_ids))
            .where(Role.is_deleted.is_(False))
        )
        result = await self.session.scalars(stmt)
        return list(result.all())

    async def list_menu_ids(self, role_id: int) -> list[int]:
        """Menu ids currently granted to the role."""
        stmt = select(RoleMenu.menu_id).where(RoleMenu.role_id == role_id)
        result = await self.session.scalars(stmt)
        return list(result.all())

    async def list_dept_ids(self, role_id: int) -> list[int]:
        """Custom-scope department ids currently granted to the role."""
        stmt = select(RoleDept.dept_id).where(RoleDept.role_id == role_id)
        result = await self.session.scalars(stmt)
        return list(result.all())

    async def list_menu_ids_for_roles(
        self, role_ids: Sequence[int]
    ) -> dict[int, list[int]]:
        """Menu ids for many roles in one query (avoids 1+N on listing).

        Returns a ``role_id -> [menu_id, ...]`` map; roles with no menus are
        absent (caller defaults to an empty list).
        """
        if not role_ids:
            return {}
        stmt = (
            select(RoleMenu.role_id, RoleMenu.menu_id)
            .where(RoleMenu.role_id.in_(role_ids))
            .order_by(RoleMenu.role_id, RoleMenu.menu_id)
        )
        result = await self.session.execute(stmt)
        grouped: dict[int, list[int]] = {}
        for role_id, menu_id in result.all():
            grouped.setdefault(role_id, []).append(menu_id)
        return grouped

    async def list_dept_ids_for_roles(
        self, role_ids: Sequence[int]
    ) -> dict[int, list[int]]:
        """Custom-scope dept ids for many roles in one query (avoids 1+N).

        Returns a ``role_id -> [dept_id, ...]`` map; roles with no dept grants
        are absent (caller defaults to an empty list).
        """
        if not role_ids:
            return {}
        stmt = (
            select(RoleDept.role_id, RoleDept.dept_id)
            .where(RoleDept.role_id.in_(role_ids))
            .order_by(RoleDept.role_id, RoleDept.dept_id)
        )
        result = await self.session.execute(stmt)
        grouped: dict[int, list[int]] = {}
        for role_id, dept_id in result.all():
            grouped.setdefault(role_id, []).append(dept_id)
        return grouped

    async def existing_menu_ids(self, menu_ids: Sequence[int]) -> set[int]:
        """Subset of menu_ids that exist and are non-deleted (FK-less integrity)."""
        if not menu_ids:
            return set()
        stmt = (
            select(Menu.id)
            .where(Menu.id.in_(menu_ids))
            .where(Menu.is_deleted.is_(False))
        )
        result = await self.session.scalars(stmt)
        return set(result.all())

    async def existing_dept_ids(self, dept_ids: Sequence[int]) -> set[int]:
        """Subset of dept_ids that exist and are non-deleted (FK-less integrity)."""
        if not dept_ids:
            return set()
        stmt = (
            select(Department.id)
            .where(Department.id.in_(dept_ids))
            .where(Department.is_deleted.is_(False))
        )
        result = await self.session.scalars(stmt)
        return set(result.all())

    async def replace_menus(self, role_id: int, menu_ids: Sequence[int]) -> None:
        """Replace the role's menu grants (delete-all then insert, no commit)."""
        await self.session.execute(
            delete(RoleMenu).where(RoleMenu.role_id == role_id)
        )
        for menu_id in dict.fromkeys(menu_ids):  # de-dup, preserve order
            self.session.add(RoleMenu(role_id=role_id, menu_id=menu_id))
        await self.session.flush()

    async def replace_depts(self, role_id: int, dept_ids: Sequence[int]) -> None:
        """Replace the role's custom-scope dept grants (delete-all then insert)."""
        await self.session.execute(
            delete(RoleDept).where(RoleDept.role_id == role_id)
        )
        for dept_id in dict.fromkeys(dept_ids):
            self.session.add(RoleDept(role_id=role_id, dept_id=dept_id))
        await self.session.flush()

    async def delete_user_links(self, role_id: int) -> None:
        """Physically remove all UserRole rows for the role (FK-less cascade).

        Called on role deletion so soft-deleted roles leave no dangling user
        assignments (which would otherwise surface as stale role ids on users).
        """
        await self.session.execute(
            delete(UserRole).where(UserRole.role_id == role_id)
        )
        await self.session.flush()
