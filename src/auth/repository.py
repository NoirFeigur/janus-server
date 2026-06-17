"""Auth domain data access (README: repository layer, four-layer discipline).

Queries spanning the identity + RBAC + credential aggregates needed for
authentication and authorization. All ORM access for the auth domain is收口
here; the service layer receives domain objects / value sets, never raw
``select()`` statements.

No physical foreign keys (§0.7): joins are expressed on the snowflake id
columns directly. Link tables (UserRole/RoleMenu/RoleDept) are physically
deleted (no ``is_deleted``); active/not-deleted filters apply only to the
BaseEntity tables (Role/Menu).
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models.credential import ApiKey
from src.db.models.identity import Department, Menu, Role, RoleDept, RoleMenu, User, UserRole
from src.enums import ActiveStatus, ApiKeyStatus, UserStatus


class AuthRepository:
    """Read access for authentication + RBAC resolution."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_user_by_username(self, username: str) -> User | None:
        """Active, non-deleted user by login username (None if absent/disabled)."""
        stmt = (
            select(User)
            .where(User.username == username)
            .where(User.is_deleted.is_(False))
            .where(User.status == UserStatus.active.value)
        )
        user: User | None = await self.session.scalar(stmt)
        return user

    async def get_user_by_id(self, user_id: int) -> User | None:
        """Active, non-deleted user by id (None if absent/disabled)."""
        stmt = (
            select(User)
            .where(User.id == user_id)
            .where(User.is_deleted.is_(False))
            .where(User.status == UserStatus.active.value)
        )
        user: User | None = await self.session.scalar(stmt)
        return user

    async def get_api_key_by_hash(self, key_hash: str) -> ApiKey | None:
        """Active, non-deleted api_key by sha256 hash (None if absent/disabled).

        Expiry is NOT checked here (it's time-dependent); the service compares
        ``expires_at`` so the repository stays a pure lookup.
        """
        stmt = (
            select(ApiKey)
            .where(ApiKey.key_hash == key_hash)
            .where(ApiKey.is_deleted.is_(False))
            .where(ApiKey.status == ApiKeyStatus.active.value)
        )
        api_key: ApiKey | None = await self.session.scalar(stmt)
        return api_key

    async def list_permission_codes(self, user_id: int) -> frozenset[str]:
        """Aggregate the user's effective permission codes in one query.

        Walk user → roles → menus, collecting ``Menu.perms``. Filters active +
        non-deleted on Role/Menu (link tables have no such columns). Menus with a
        null ``perms`` (catalog/menu nodes without an operation code) are excluded
        by the NOT NULL predicate.
        """
        stmt = (
            select(Menu.perms)
            .join(RoleMenu, RoleMenu.menu_id == Menu.id)
            .join(Role, Role.id == RoleMenu.role_id)
            .join(UserRole, UserRole.role_id == Role.id)
            .where(UserRole.user_id == user_id)
            .where(Role.is_deleted.is_(False))
            .where(Role.status == ActiveStatus.active.value)
            .where(Menu.is_deleted.is_(False))
            .where(Menu.status == ActiveStatus.active.value)
            .where(Menu.perms.is_not(None))
        )
        result = await self.session.scalars(stmt)
        return frozenset(code for code in result.all() if code)

    async def list_permission_codes_for_roles(
        self, role_ids: Sequence[int]
    ) -> frozenset[str]:
        """Aggregate permission codes conferred by a specific set of roles.

        Used by the user-admin layer to enforce that an actor cannot assign a
        role granting permissions the actor does not itself hold (privilege-
        escalation guard). Same active/not-deleted filters as the per-user
        aggregation; the link table (RoleMenu) is not filtered (no is_deleted).
        """
        if not role_ids:
            return frozenset()
        stmt = (
            select(Menu.perms)
            .join(RoleMenu, RoleMenu.menu_id == Menu.id)
            .join(Role, Role.id == RoleMenu.role_id)
            .where(RoleMenu.role_id.in_(role_ids))
            .where(Role.is_deleted.is_(False))
            .where(Role.status == ActiveStatus.active.value)
            .where(Menu.is_deleted.is_(False))
            .where(Menu.status == ActiveStatus.active.value)
            .where(Menu.perms.is_not(None))
        )
        result = await self.session.scalars(stmt)
        return frozenset(code for code in result.all() if code)

    async def list_active_role_codes(self, user_id: int) -> frozenset[str]:
        """Codes of the user's active, non-deleted roles (super-admin marker).

        Drives ``AuthenticatedUser.is_superuser`` (code-based super-admin). Same
        active/not-deleted filters as the other RBAC aggregations; the link
        table (UserRole) has no such columns.
        """
        stmt = (
            select(Role.code)
            .join(UserRole, UserRole.role_id == Role.id)
            .where(UserRole.user_id == user_id)
            .where(Role.is_deleted.is_(False))
            .where(Role.status == ActiveStatus.active.value)
        )
        result = await self.session.scalars(stmt)
        return frozenset(result.all())

    async def list_active_roles(self, user_id: int) -> Sequence[Role]:
        """User's active, non-deleted roles (for data-scope resolution)."""
        stmt = (
            select(Role)
            .join(UserRole, UserRole.role_id == Role.id)
            .where(UserRole.user_id == user_id)
            .where(Role.is_deleted.is_(False))
            .where(Role.status == ActiveStatus.active.value)
        )
        result = await self.session.scalars(stmt)
        return result.all()

    async def list_role_department_ids(self, role_ids: Sequence[int]) -> frozenset[int]:
        """Custom-scope department ids granted to the given roles (sys_role_dept)."""
        if not role_ids:
            return frozenset()
        stmt = select(RoleDept.dept_id).where(RoleDept.role_id.in_(role_ids))
        result = await self.session.scalars(stmt)
        return frozenset(result.all())

    async def list_all_departments(self) -> Sequence[Department]:
        """All non-deleted departments (id + parent_id) for subtree resolution.

        The tree is small (org departments); loading it and walking the adjacency
        list in Python avoids a recursive CTE and is portable across PG/SQLite.
        """
        stmt = select(Department).where(Department.is_deleted.is_(False))
        result = await self.session.scalars(stmt)
        return result.all()
