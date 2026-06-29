"""Admin user data access (repository layer).

Base CRUD on ``User`` plus role-link management (``UserRole``) and the
keyword-filtered listing the admin user surface requires.
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm.attributes import InstrumentedAttribute
from sqlalchemy.sql.elements import ColumnElement

from src.db.models.identity import User, UserRole
from src.db.repository import BaseRepository


class UserRepository(BaseRepository[User]):
    model = User

    def _keyword_predicate(self, keyword: str | None) -> ColumnElement[bool] | None:
        if keyword is None:
            return None
        normalized = keyword.strip().lower()
        if not normalized:
            return None
        pattern = f"%{normalized}%"
        return or_(
            func.lower(User.username).like(pattern),
            func.lower(User.employee_no).like(pattern),
            func.lower(User.real_name).like(pattern),
        )

    async def get_by_username(self, username: str) -> User | None:
        """Non-deleted user by username (uniqueness guard; any status)."""
        stmt = (
            select(User)
            .where(User.username == username)
            .where(User.is_deleted.is_(False))
        )
        user: User | None = await self.session.scalar(stmt)
        return user

    async def list_users(
        self,
        *,
        keyword: str | None = None,
        sort: tuple[InstrumentedAttribute[object], bool] | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> Sequence[User]:
        """List non-deleted users (keyword-filtered, sorted, paginated)."""
        stmt = select(User).where(User.is_deleted.is_(False))
        keyword_predicate = self._keyword_predicate(keyword)
        if keyword_predicate is not None:
            stmt = stmt.where(keyword_predicate)
        if sort is None:
            stmt = stmt.order_by(User.id)
        else:
            sort_column, descending = sort
            stmt = stmt.order_by(sort_column.desc() if descending else sort_column.asc())
        if offset is not None:
            stmt = stmt.offset(offset)
        if limit is not None:
            stmt = stmt.limit(limit)
        result = await self.session.scalars(stmt)
        return result.all()

    async def count_users(
        self,
        *,
        keyword: str | None = None,
    ) -> int:
        """Count non-deleted users (keyword-filtered)."""
        stmt = select(func.count()).select_from(User).where(User.is_deleted.is_(False))
        keyword_predicate = self._keyword_predicate(keyword)
        if keyword_predicate is not None:
            stmt = stmt.where(keyword_predicate)
        total = await self.session.scalar(stmt)
        return int(total or 0)

    async def list_role_ids(self, user_id: int) -> list[int]:
        """Role ids currently assigned to the user."""
        stmt = select(UserRole.role_id).where(UserRole.user_id == user_id)
        result = await self.session.scalars(stmt)
        return list(result.all())

    async def list_role_ids_for_users(
        self, user_ids: Sequence[int]
    ) -> dict[int, list[int]]:
        """Role ids for many users in one query (avoids 1+N on listing).

        Returns a ``user_id -> [role_id, ...]`` map. Users with no roles are
        absent from the map; the caller defaults them to an empty list.
        """
        if not user_ids:
            return {}
        stmt = (
            select(UserRole.user_id, UserRole.role_id)
            .where(UserRole.user_id.in_(user_ids))
            .order_by(UserRole.user_id, UserRole.role_id)
        )
        result = await self.session.execute(stmt)
        grouped: dict[int, list[int]] = {}
        for user_id, role_id in result.all():
            grouped.setdefault(user_id, []).append(role_id)
        return grouped

    async def replace_roles(self, user_id: int, role_ids: Sequence[int]) -> None:
        """Replace the user's role assignments (delete-all then insert, no commit)."""
        await self.session.execute(
            delete(UserRole).where(UserRole.user_id == user_id)
        )
        for role_id in dict.fromkeys(role_ids):
            self.session.add(UserRole(user_id=user_id, role_id=role_id))
        await self.session.flush()
