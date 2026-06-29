"""Auth domain data access (README: repository layer, four-layer discipline).

Queries spanning the identity + RBAC + credential aggregates needed for
authentication and authorization. All ORM access for the auth domain is收口
here; the service layer receives domain objects / value sets, never raw
``select()`` statements.

No physical foreign keys (§0.7): joins are expressed on the snowflake id
columns directly. Link tables (UserRole/RoleMenu) are physically
deleted (no ``is_deleted``); active/not-deleted filters apply only to the
BaseEntity tables (Role/Menu).
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models.attach import Attach
from src.db.models.audit import LoginLog
from src.db.models.credential import ApiKey
from src.db.models.identity import Menu, Role, RoleMenu, User, UserRole
from src.enums import ActiveStatus, ApiKeyStatus, AttachBizType, UserStatus


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

    async def get_owned_avatar(self, attach_id: int, owner_id: int) -> Attach | None:
        """An avatar attachment owned by ``owner_id`` (None if absent/not owned).

        Self-service avatar binding may only point at an attachment the caller
        uploaded (``created_by``) and that is of ``avatar`` biz type — prevents
        binding someone else's object or a non-avatar file as a profile picture.
        """
        stmt = (
            select(Attach)
            .where(Attach.id == attach_id)
            .where(Attach.is_deleted.is_(False))
            .where(Attach.biz_type == AttachBizType.avatar.value)
            .where(Attach.created_by == owner_id)
        )
        attach: Attach | None = await self.session.scalar(stmt)
        return attach

    async def get_attach_object_key(self, attach_id: int) -> str | None:
        """Object key for an attachment by id (None if absent/soft-deleted).

        Read path for presigning an already-bound avatar URL on ``/me`` — the
        avatar id was ownership-validated at bind time, so the read just needs the
        key (no owner filter). Soft-deleted rows return ``None`` (avatar cleared).
        """
        stmt = (
            select(Attach.object_key)
            .where(Attach.id == attach_id)
            .where(Attach.is_deleted.is_(False))
        )
        object_key: str | None = await self.session.scalar(stmt)
        return object_key

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

    async def list_permission_codes_for_menus(
        self, menu_ids: Sequence[int]
    ) -> frozenset[str]:
        """Permission codes conferred directly by a set of menus.

        Used by the role-admin layer to enforce that an actor cannot craft a
        role granting permissions the actor does not itself hold (role-edit
        privilege-escalation guard). Same active/not-deleted + non-null-perms
        filters as the per-user aggregation, but keyed on the menu ids directly
        (no role/user join — the menus are the ones about to be linked).
        """
        if not menu_ids:
            return frozenset()
        stmt = (
            select(Menu.perms)
            .where(Menu.id.in_(menu_ids))
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

    async def list_active_roles_by_ids(self, role_ids: Sequence[int]) -> Sequence[Role]:
        """Active, non-deleted role rows for a given id set (assignment guards).

        Used by the user-admin escalation guard to inspect a role's ``code``
        (super-admin marker) before letting an actor assign it — a perms-only
        subset check is blind to it (a ``superadmin`` role with no menus confers
        zero perms yet grants unrestricted authority). Same active/not-deleted
        filters as the other RBAC reads.
        """
        if not role_ids:
            return ()
        stmt = (
            select(Role)
            .where(Role.id.in_(role_ids))
            .where(Role.is_deleted.is_(False))
            .where(Role.status == ActiveStatus.active.value)
        )
        result = await self.session.scalars(stmt)
        return result.all()

    async def append_login_log(self, row: LoginLog) -> LoginLog:
        """Append one login-attempt audit row and flush its snowflake id.

        Login auditing is an auth-domain concern: the row is written from the
        auth service's own session (its own unit of work for failures, see
        ``AuthService._append_login_log``). Owning the insert here keeps auth from
        importing the admin audit repository — admin reads the same ``LoginLog``
        table from its side, but the write belongs to the domain that produces it.
        ``LoginLog`` inherits ``LogEntity`` (append-only), so this is a bare
        add + flush with no update/soft-delete semantics.
        """
        self.session.add(row)
        await self.session.flush()
        return row
