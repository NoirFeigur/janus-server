"""Tests for login-attempt audit logging (C5).

Every call to ``AuthService.authenticate_password`` must append exactly one
``login_log`` row — success or failure — capturing the internal
``LoginFailureReason`` (which is *never* leaked to the client; the HTTP layer
still returns one opaque 401). IP / user-agent / trace_id thread in from the
router as plain keyword args (no Request object in the service layer).
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from src.auth.service import AuthService
from src.db.base import Base
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
from src.exceptions import AppError
from tests.auth.conftest import seed_user

pytestmark = pytest.mark.asyncio

_TABLES = [
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
    )
]


@pytest_asyncio.fixture
async def login_session(
    sqlite_engine: AsyncEngine,
    sqlite_session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """Session with auth/RBAC/credential tables + login_log created."""
    async with sqlite_engine.begin() as conn:
        await conn.run_sync(
            lambda sync_conn: Base.metadata.create_all(sync_conn, tables=_TABLES)
        )
    async with sqlite_session_factory() as session:
        yield session


async def test_successful_login_records_success_row(
    login_session: AsyncSession,
) -> None:
    await seed_user(login_session, username="alice", password="secret123")
    await login_session.commit()

    service = AuthService(login_session)
    token, ttl, _ = await service.authenticate_password(
        "alice",
        "secret123",
        request_ip="10.0.0.5",
        user_agent="pytest-UA",
        trace_id="trace-xyz",
    )
    assert token
    assert ttl > 0

    rows = (await login_session.scalars(select(LoginLog))).all()
    assert len(rows) == 1
    row = rows[0]
    assert row.username == "alice"
    assert row.status == "success"
    assert row.failure_reason is None
    assert row.user_id is not None
    assert row.request_ip == "10.0.0.5"
    assert row.user_agent == "pytest-UA"
    assert row.trace_id == "trace-xyz"


async def test_unknown_user_records_user_not_found(
    login_session: AsyncSession,
) -> None:
    service = AuthService(login_session)
    with pytest.raises(AppError):
        await service.authenticate_password("ghost", "whatever")

    rows = (await login_session.scalars(select(LoginLog))).all()
    assert len(rows) == 1
    assert rows[0].status == "failure"
    assert rows[0].failure_reason == "user_not_found"
    assert rows[0].user_id is None
    assert rows[0].username == "ghost"


async def test_wrong_password_records_bad_credentials(
    login_session: AsyncSession,
) -> None:
    await seed_user(login_session, username="bob", password="rightpass")
    await login_session.commit()

    service = AuthService(login_session)
    with pytest.raises(AppError):
        await service.authenticate_password("bob", "wrongpass")

    rows = (await login_session.scalars(select(LoginLog))).all()
    assert len(rows) == 1
    assert rows[0].status == "failure"
    assert rows[0].failure_reason == "bad_credentials"
    assert rows[0].user_id is not None


async def test_sso_only_user_records_bad_credentials(
    login_session: AsyncSession,
) -> None:
    # An SSO-only user has a null local password; password login is rejected.
    await seed_user(login_session, username="ssouser", password=None)
    await login_session.commit()

    service = AuthService(login_session)
    with pytest.raises(AppError):
        await service.authenticate_password("ssouser", "anything")

    rows = (await login_session.scalars(select(LoginLog))).all()
    assert len(rows) == 1
    assert rows[0].status == "failure"
    assert rows[0].failure_reason == "bad_credentials"
