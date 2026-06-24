"""Shared fixtures for admin route-level tests.

Drives the real app through ``httpx.AsyncClient`` + ``ASGITransport`` (same event
loop as the test, so the shared in-memory SQLite session is safe). One
dependency is overridden:

- ``get_session`` → one shared session (tables created, persists across requests).
The auth middleware still runs; requests carry a test JWT and the middleware uses
the same SQLite session factory.

An admin user (id 1000) with a ``data_scope=all`` role is seeded so user listing
is unrestricted by default; data-scope tests override the actor's department.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field

import httpx
import pytest_asyncio
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from httpx import ASGITransport
from pydantic import SecretStr
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.schema import CreateIndex

from src.auth.constants import SUPERADMIN_ROLE_CODE
from src.auth.dependencies import get_current_jwt_user
from src.auth.service import AuthenticatedUser
from src.config import get_settings
from src.core.redis import get_redis
from src.core.security import issue_access_token
from src.core.session_store import SessionStore
from src.db.base import Base
from src.db.models.audit import LoginLog, OperLog
from src.db.models.catalog_ops import CatalogChangeLog
from src.db.models.credential import ApiKey
from src.db.models.grant import UserModelGrant
from src.db.models.identity import (
    Department,
    Menu,
    Role,
    RoleDept,
    RoleMenu,
    User,
    UserRole,
)
from src.db.models.model_catalog import (
    ChannelKey,
    LogicalModel,
    ModelDeployment,
    UpstreamChannel,
)
from src.db.models.quota import Quota
from src.db.models.sys_config import SysConfig
from src.db.models.usage import UsageRecord
from src.db.session import get_session
from src.main import create_app

ADMIN_ID = 1000


@compiles(JSONB, "sqlite")
def _compile_jsonb_for_sqlite(element: JSONB, compiler, **kw) -> str:
    return "JSON"


@compiles(CreateIndex, "sqlite")
def _compile_index_for_sqlite(element: CreateIndex, compiler, **kw) -> str:
    if element.element.name == "uq_grant_one_default":
        return "SELECT 1"
    return compiler.visit_create_index(element, **kw)

_TABLES = [
    Base.metadata.tables[m.__tablename__]
    for m in (
        User,
        Department,
        Role,
        Menu,
        UserRole,
        RoleMenu,
        RoleDept,
        ApiKey,
        UpstreamChannel,
        ChannelKey,
        LogicalModel,
        ModelDeployment,
        UserModelGrant,
        Quota,
        UsageRecord,
        OperLog,
        LoginLog,
        SysConfig,
        CatalogChangeLog,
    )
]


@dataclass
class AdminState:
    """Mutable actor state the override reads on each request."""

    perms: set[str] = field(default_factory=lambda: {"*:*:*"})
    user_id: int = ADMIN_ID
    department_id: int | None = None


@dataclass
class AdminCtx:
    client: httpx.AsyncClient
    state: AdminState
    session: AsyncSession


@pytest_asyncio.fixture
async def admin_session(
    sqlite_engine: AsyncEngine,
    sqlite_session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """A bare session with the admin tables created, for *direct* service tests.

    Route tests (``admin_ctx``) prove behaviour end-to-end, but their handlers run
    under httpx ``ASGITransport`` / anyio, which corrupts coverage.py's C tracer on
    CPython 3.11 — service bodies execute but read back as uncovered. Driving the
    service directly with a plain ``await`` keeps the tracer armed, so coverage of
    the service layer is honest (and the tests are faster + branch-focused).
    """
    async with sqlite_engine.begin() as conn:
        await conn.run_sync(
            lambda sync_conn: Base.metadata.create_all(sync_conn, tables=_TABLES)
        )
    session = sqlite_session_factory()
    try:
        yield session
    finally:
        await session.close()


@pytest_asyncio.fixture
async def admin_ctx(
    sqlite_engine: AsyncEngine,
    sqlite_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch,
) -> AsyncIterator[AdminCtx]:
    async with sqlite_engine.begin() as conn:
        await conn.run_sync(
            lambda sync_conn: Base.metadata.create_all(sync_conn, tables=_TABLES)
        )
    session = sqlite_session_factory()

    settings = get_settings()
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    priv_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode("utf-8")
    monkeypatch.setattr(settings, "platform_jwt_private_key", SecretStr(priv_pem))
    monkeypatch.setattr(settings, "platform_jwt_public_key", None)

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

    async def _override_user() -> AuthenticatedUser:
        # Convention: an actor carrying the ``*:*:*`` perm represents the
        # super-admin. Production now keys super-admin off the role *code*, so
        # translate that convention into the ``superadmin`` role code here.
        role_codes = (
            frozenset({SUPERADMIN_ROLE_CODE})
            if "*:*:*" in state.perms
            else frozenset()
        )
        return AuthenticatedUser(
            user_id=state.user_id,
            username="admin",
            department_id=state.department_id,
            permissions=frozenset(state.perms),
            role_codes=role_codes,
        )

    app = create_app()
    app.dependency_overrides[get_session] = _override_session
    app.dependency_overrides[get_current_jwt_user] = _override_user
    app.state.session_factory = sqlite_session_factory

    transport = ASGITransport(app=app)
    token, ttl, jti = issue_access_token(user_id=ADMIN_ID)
    # The AuthMiddleware resolves this token on every request and now enforces the
    # session allowlist, so the minted token must have a live session registered.
    await SessionStore(get_redis()).create_session(
        user_id=ADMIN_ID,
        access_jti=jti,
        access_ttl=ttl,
        refresh_hash="admin-fixture-refresh",
        refresh_ttl=ttl,
    )
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
    ) as client:
        yield AdminCtx(client=client, state=state, session=session)

    await session.close()
