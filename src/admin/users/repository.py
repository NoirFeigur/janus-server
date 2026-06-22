"""Admin user data access (repository layer).

Base CRUD on ``User`` plus role-link management (``UserRole``) and the
data-scope-aware listing the admin user surface requires. The data-scope filter
is the only place the resolved scope touches SQL: a row is visible when
unrestricted, OR its department is in the allowed set, OR (include_self) it is
the actor's own record.

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

from src.db.models.identity import User, UserRole
from src.db.repository import BaseRepository
from src.db.scope import DataScope


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

    def _scope_predicate(
        self, scope: DataScope, *, actor_id: int
    ) -> ColumnElement[bool] | None:
        """Build the WHERE predicate for a data scope (None = no restriction)."""
        if scope.unrestricted:
            return None
        clauses: list[ColumnElement[bool]] = []
        if scope.department_ids:
            clauses.append(User.department_id.in_(scope.department_ids))
        if scope.include_self:
            clauses.append(User.id == actor_id)
        if not clauses:
            # Restricted scope with no allowed depts and no self → match nothing.
            return User.id == -1
        return or_(*clauses)

    async def list_in_scope(
        self,
        scope: DataScope,
        *,
        actor_id: int,
        keyword: str | None = None,
        sort: tuple[InstrumentedAttribute[object], bool] | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> Sequence[User]:
        """List non-deleted users visible under the data scope."""
        stmt = select(User).where(User.is_deleted.is_(False))
        predicate = self._scope_predicate(scope, actor_id=actor_id)
        if predicate is not None:
            stmt = stmt.where(predicate)
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

    async def count_in_scope(
        self,
        scope: DataScope,
        *,
        actor_id: int,
        keyword: str | None = None,
    ) -> int:
        """Count non-deleted users visible under the data scope."""
        stmt = select(func.count()).select_from(User).where(User.is_deleted.is_(False))
        predicate = self._scope_predicate(scope, actor_id=actor_id)
        if predicate is not None:
            stmt = stmt.where(predicate)
        keyword_predicate = self._keyword_predicate(keyword)
        if keyword_predicate is not None:
            stmt = stmt.where(keyword_predicate)
        total = await self.session.scalar(stmt)
        return int(total or 0)

    def is_visible(
        self, user: User, scope: DataScope, *, actor_id: int
    ) -> bool:
        """In-Python scope check for a single row (mutation guard)."""
        if scope.unrestricted:
            return True
        if user.department_id is not None and user.department_id in scope.department_ids:
            return True
        return bool(scope.include_self and user.id == actor_id)

    async def list_role_ids(self, user_id: int) -> list[int]:
        """Role ids currently assigned to the user."""
        stmt = select(UserRole.role_id).where(UserRole.user_id == user_id)
        result = await self.session.scalars(stmt)
        return list(result.all())

    async def department_ids_for_users(
        self, user_ids: Sequence[int]
    ) -> dict[int, int | None]:
        """Map ``user_id -> department_id`` for many users in one query.

        Used by the batch-dominance pre-filter to resolve each target's relative
        role scope against its OWN department (not the actor's). Absent ids are
        defaulted by the caller's ``.get``.
        """
        if not user_ids:
            return {}
        stmt = select(User.id, User.department_id).where(User.id.in_(user_ids))
        result = await self.session.execute(stmt)
        return {user_id: department_id for user_id, department_id in result.all()}

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
