"""Shared fixtures for admin route-level tests.

Drives the real app through ``httpx.AsyncClient`` + ``ASGITransport`` (same event
loop as the test, so the shared in-memory SQLite session is safe). Two
dependencies are overridden:

- ``get_session`` → one shared session (tables created, persists across requests).
- ``get_current_account`` → a configurable principal; tests mutate ``state`` to
  change the actor's permissions / id / department without minting real tokens.

An admin user (id 1000) with a ``data_scope=all`` role is seeded so user listing
is unrestricted by default; data-scope tests override the actor's department.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field

import httpx
import pytest_asyncio
from httpx import ASGITransport
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from src.auth.dependencies import get_current_account
from src.auth.service import AuthenticatedAccount
from src.db.base import Base
from src.db.models.credential import ApiKey
from src.db.models.identity import (
    Department,
    Menu,
    Role,
    RoleDept,
    RoleMenu,
    User,
    UserRole,
)
from src.db.session import get_session
from src.main import create_app

ADMIN_ID = 1000

_TABLES = [
    Base.metadata.tables[m.__tablename__]
    for m in (User, Department, Role, Menu, UserRole, RoleMenu, RoleDept, ApiKey)
]


@dataclass
class AdminState:
    """Mutable actor state the override reads on each request."""

    perms: set[str] = field(default_factory=lambda: {"*:*:*"})
    account_id: int = ADMIN_ID
    department_id: int | None = None


@dataclass
class AdminCtx:
    client: httpx.AsyncClient
    state: AdminState
    session: AsyncSession


@pytest_asyncio.fixture
async def admin_ctx(
    sqlite_engine: AsyncEngine,
    sqlite_session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AdminCtx]:
    async with sqlite_engine.begin() as conn:
        await conn.run_sync(
            lambda sync_conn: Base.metadata.create_all(sync_conn, tables=_TABLES)
        )
    session = sqlite_session_factory()

    # Seed the admin actor + an all-scope role so user listing is unrestricted.
    role = Role(name="admin", code="admin", data_scope="all", status="active")
    session.add(role)
    await session.flush()
    session.add(
        User(id=ADMIN_ID, username="admin", employee_no="E-admin", status="active")
    )
    session.add(UserRole(user_id=ADMIN_ID, role_id=role.id))
    await session.commit()

    state = AdminState()

    async def _override_session() -> AsyncIterator[AsyncSession]:
        yield session

    async def _override_account() -> AuthenticatedAccount:
        return AuthenticatedAccount(
            account_id=state.account_id,
            username="admin",
            department_id=state.department_id,
            permissions=frozenset(state.perms),
        )

    app = create_app()
    app.dependency_overrides[get_session] = _override_session
    app.dependency_overrides[get_current_account] = _override_account

    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as client:
        yield AdminCtx(client=client, state=state, session=session)

    await session.close()
