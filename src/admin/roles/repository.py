"""Admin role data access (repository layer).

Beyond base CRUD on ``Role``, manages the role's ``RoleMenu`` link table
(permission grants). Links are physically replaced (LinkEntity has no
soft-delete): assignment is a delete-all-then-insert within the caller's
transaction.
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm.attributes import InstrumentedAttribute
from sqlalchemy.sql.elements import ColumnElement

from src.db.models.identity import Menu, Role, RoleMenu, UserRole
from src.db.repository import BaseRepository


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

    async def list_roles(
        self,
        *,
        keyword: str | None = None,
        sort: tuple[InstrumentedAttribute[object], bool] | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> Sequence[Role]:
        """List non-deleted roles (keyword-filtered, sorted, paginated)."""
        stmt = select(Role).where(Role.is_deleted.is_(False))
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

    async def count_roles(
        self,
        *,
        keyword: str | None = None,
    ) -> int:
        """Count non-deleted roles (keyword-filtered)."""
        stmt = select(func.count()).select_from(Role).where(Role.is_deleted.is_(False))
        keyword_predicate = self._keyword_predicate(keyword)
        if keyword_predicate is not None:
            stmt = stmt.where(keyword_predicate)
        total = await self.session.scalar(stmt)
        return int(total or 0)

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

    async def replace_menus(self, role_id: int, menu_ids: Sequence[int]) -> None:
        """Replace the role's menu grants (delete-all then insert, no commit)."""
        await self.session.execute(
            delete(RoleMenu).where(RoleMenu.role_id == role_id)
        )
        for menu_id in dict.fromkeys(menu_ids):  # de-dup, preserve order
            self.session.add(RoleMenu(role_id=role_id, menu_id=menu_id))
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
