"""Shared fixtures for auth-domain tests.

Builds the identity + RBAC + credential tables on the in-memory SQLite engine
(they create cleanly — PG partial indexes / dialect kwargs are ignored on
SQLite) and yields a session plus small seed helpers.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from src.core.security import hash_password
from src.db.base import Base
from src.db.models.attach import Attach
from src.db.models.audit import LoginLog
from src.db.models.credential import ApiKey
from src.db.models.identity import (
    Department,
    Menu,
    Role,
    RoleMenu,
    User,
    UserRole,
)

_AUTH_TABLES = [
    Base.metadata.tables[m.__tablename__]
    for m in (
        User,
        Department,
        Role,
        Menu,
        UserRole,
        RoleMenu,
        ApiKey,
        LoginLog,
        Attach,
    )
]


@pytest_asyncio.fixture
async def auth_session(
    sqlite_engine: AsyncEngine,
    sqlite_session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """Session with the auth/RBAC/credential tables created."""
    async with sqlite_engine.begin() as conn:
        await conn.run_sync(
            lambda sync_conn: Base.metadata.create_all(sync_conn, tables=_AUTH_TABLES)
        )
    async with sqlite_session_factory() as session:
        yield session


async def seed_user(
    session: AsyncSession,
    *,
    username: str = "alice",
    password: str | None = "secret123",
    department_id: int | None = None,
    status: str = "active",
) -> User:
    user = User(
        username=username,
        employee_no=f"E-{username}",
        password=hash_password(password) if password else None,
        department_id=department_id,
        status=status,
    )
    session.add(user)
    await session.flush()
    return user


async def grant_permission(
    session: AsyncSession, *, user: User, perm: str, role_code: str = "r1"
) -> Role:
    """Wire user → role → menu(perm) so the perm aggregates."""
    role = Role(name=role_code, code=role_code, status="active")
    session.add(role)
    await session.flush()
    menu = Menu(name=f"m.{perm}", menu_type="button", perms=perm, status="active")
    session.add(menu)
    await session.flush()
    session.add(UserRole(user_id=user.id, role_id=role.id))
    session.add(RoleMenu(role_id=role.id, menu_id=menu.id))
    await session.flush()
    return role
