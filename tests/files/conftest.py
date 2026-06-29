"""Route-level fixtures for ``POST /attach/upload`` (end-to-end through the app).

Boots the real app via ``create_app`` and exercises the upload endpoint over
httpx ``ASGITransport``, overriding three dependencies:

- ``get_session`` → the in-memory SQLite session (so ``sys_attach`` rows persist).
- ``get_current_jwt_user`` → a fixed authenticated principal (auth is proven
  elsewhere; here we focus on the upload contract).
- ``get_object_storage`` → a fake recording storage (no real bucket; uploads are
  captured in-memory and presigned URLs are deterministic).

The AuthMiddleware still resolves the bearer token on every request and enforces
the session allowlist, so the minted token gets a live session registered.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest_asyncio
from httpx import ASGITransport
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from src.auth.dependencies import get_current_jwt_user
from src.auth.service import AuthenticatedUser
from src.core.oss import get_object_storage
from src.core.redis import get_redis
from src.core.security import issue_access_token
from src.core.session_store import SessionStore
from src.db.base import Base
from src.db.models.attach import SysAttach
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
from src.db.session import get_session
from src.main import create_app

USER_ID = 2000
DEPT_ID = 7

# The AuthMiddleware resolves the bearer token on every request against these
# tables (user + RBAC), so they must exist and the uploader must be seeded.
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
        SysAttach,
    )
]


class FakeStorage:
    """Records uploads/deletes; returns deterministic presigned URLs (no bucket)."""

    def __init__(self) -> None:
        self.uploads: list[dict[str, Any]] = []
        self.deletes: list[str] = []

    @property
    def bucket(self) -> str:
        return "private"

    async def upload(self, *, object_key: str, data: bytes, content_type: str) -> None:
        self.uploads.append(
            {"object_key": object_key, "data": data, "content_type": content_type}
        )

    async def presign_get(self, object_key: str, *, force_download: bool = False) -> str:
        return f"https://signed.example/{object_key}"

    async def delete(self, object_key: str) -> None:
        self.deletes.append(object_key)


@dataclass
class AttachState:
    """Mutable actor state the JWT override reads on each request."""

    user_id: int = USER_ID
    department_id: int | None = DEPT_ID
    permissions: set[str] = field(default_factory=set)


@dataclass
class AttachCtx:
    client: httpx.AsyncClient
    state: AttachState
    session: AsyncSession
    storage: FakeStorage


@pytest_asyncio.fixture
async def attach_ctx(
    sqlite_engine: AsyncEngine,
    sqlite_session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AttachCtx]:
    async with sqlite_engine.begin() as conn:
        await conn.run_sync(
            lambda c: Base.metadata.create_all(c, tables=_TABLES)
        )
    session = sqlite_session_factory()
    # Seed the uploader (id=USER_ID) so the AuthMiddleware can resolve the token.
    session.add(
        User(
            id=USER_ID,
            username="uploader",
            employee_no="E-uploader",
            department_id=DEPT_ID,
            status="active",
        )
    )
    await session.commit()
    state = AttachState()
    storage = FakeStorage()

    async def _override_session() -> AsyncIterator[AsyncSession]:
        yield session

    async def _override_user() -> AuthenticatedUser:
        return AuthenticatedUser(
            user_id=state.user_id,
            username="uploader",
            department_id=state.department_id,
            permissions=frozenset(state.permissions),
        )

    def _override_storage() -> FakeStorage:
        return storage

    app = create_app()
    app.dependency_overrides[get_session] = _override_session
    app.dependency_overrides[get_current_jwt_user] = _override_user
    app.dependency_overrides[get_object_storage] = _override_storage
    app.state.session_factory = sqlite_session_factory

    transport = ASGITransport(app=app)
    token, ttl, jti = issue_access_token(user_id=USER_ID)
    await SessionStore(get_redis()).create_session(
        user_id=USER_ID,
        access_jti=jti,
        access_ttl=ttl,
        refresh_hash="attach-fixture-refresh",
        refresh_ttl=ttl,
    )
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
    ) as client:
        yield AttachCtx(
            client=client, state=state, session=session, storage=storage
        )

    await session.close()
