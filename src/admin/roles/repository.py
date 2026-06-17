"""Admin role data access (repository layer).

Beyond base CRUD on ``Role``, manages the two link tables a role owns:
``RoleMenu`` (permission grants) and ``RoleDept`` (custom data-scope depts).
Links are physically replaced (LinkEntity has no soft-delete): assignment is a
delete-all-then-insert within the caller's transaction.
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import delete, select

from src.db.models.identity import Department, Menu, Role, RoleDept, RoleMenu, UserRole
from src.db.repository import BaseRepository


class RoleRepository(BaseRepository[Role]):
    model = Role

    async def get_by_code(self, code: str) -> Role | None:
        """Active-or-disabled, non-deleted role by unique code (uniqueness guard)."""
        stmt = (
            select(Role).where(Role.code == code).where(Role.is_deleted.is_(False))
        )
        role: Role | None = await self.session.scalar(stmt)
        return role

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
