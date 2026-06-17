"""Admin menu data access."""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import select

from src.db.models.identity import Menu, Role, RoleMenu, UserRole
from src.db.repository import BaseRepository
from src.enums import ActiveStatus


class MenuRepository(BaseRepository[Menu]):
    model = Menu

    async def list_all(self) -> Sequence[Menu]:
        """List all non-deleted menus in tree order."""
        stmt = (
            select(Menu)
            .where(Menu.is_deleted.is_(False))
            .order_by(Menu.sort_order, Menu.id)
        )
        result = await self.session.scalars(stmt)
        return result.all()

    async def list_active_visible_for_user(
        self, user_id: int, *, unrestricted: bool
    ) -> Sequence[Menu]:
        """Menus visible to the current user for dynamic frontend routing."""
        stmt = (
            select(Menu)
            .where(Menu.is_deleted.is_(False))
            .where(Menu.status == ActiveStatus.active.value)
            .where(Menu.visible.is_(True))
            .order_by(Menu.sort_order, Menu.id)
        )
        if not unrestricted:
            stmt = (
                stmt.join(RoleMenu, RoleMenu.menu_id == Menu.id)
                .join(Role, Role.id == RoleMenu.role_id)
                .join(UserRole, UserRole.role_id == Role.id)
                .where(UserRole.user_id == user_id)
                .where(Role.is_deleted.is_(False))
                .where(Role.status == ActiveStatus.active.value)
                .distinct()
            )
        result = await self.session.scalars(stmt)
        return result.all()

    async def has_children(self, menu_id: int) -> bool:
        stmt = (
            select(Menu.id)
            .where(Menu.parent_id == menu_id)
            .where(Menu.is_deleted.is_(False))
            .limit(1)
        )
        return await self.session.scalar(stmt) is not None

    async def is_role_granted(self, menu_id: int) -> bool:
        stmt = select(RoleMenu.id).where(RoleMenu.menu_id == menu_id).limit(1)
        return await self.session.scalar(stmt) is not None
