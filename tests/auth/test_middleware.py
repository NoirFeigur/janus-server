"""Tests for central auth middleware path policy."""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest
from fastapi import FastAPI, Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.auth.middleware import AuthMiddleware
from src.core.security import generate_api_key
from src.db.base import Base
from src.db.models.credential import ApiKey
from src.db.models.identity import Department, Menu, Role, RoleDept, RoleMenu, User, UserRole

pytestmark = pytest.mark.asyncio

_TABLES = [
    Base.metadata.tables[m.__tablename__]
    for m in (User, Department, Role, Menu, UserRole, RoleMenu, RoleDept, ApiKey)
]


async def _client(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[tuple[httpx.AsyncClient, AsyncSession]]:
    app = FastAPI()
    app.state.session_factory = sqlite_session_factory
    app.state.api_prefix = ""
    app.add_middleware(AuthMiddleware)

    @app.get("/auth/login")
    async def public_login() -> dict[str, str]:
        return {"ok": "public"}

    @app.get("/admin/ping")
    async def admin_ping(request: Request) -> dict[str, int]:
        return {"user_id": request.state.user.user_id}

    @app.get("/gateway/ping")
    async def gateway_ping(request: Request) -> dict[str, int | None]:
        return {"api_key_id": request.state.user.api_key_id}

    async with sqlite_session_factory() as session:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client, session


async def test_public_path_bypasses_auth(
    sqlite_engine,
    sqlite_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with sqlite_engine.begin() as conn:
        await conn.run_sync(
            lambda sync_conn: Base.metadata.create_all(sync_conn, tables=_TABLES)
        )
    async for client, _session in _client(sqlite_session_factory):
        resp = await client.get("/auth/login")
        assert resp.status_code == 200
        assert resp.json() == {"ok": "public"}


async def test_admin_rejects_missing_and_sk_key(
    sqlite_engine,
    sqlite_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with sqlite_engine.begin() as conn:
        await conn.run_sync(
            lambda sync_conn: Base.metadata.create_all(sync_conn, tables=_TABLES)
        )
    async for client, _session in _client(sqlite_session_factory):
        missing = await client.get("/admin/ping")
        assert missing.status_code == 401
        assert missing.json()["code"] == "auth.invalid_token"

        sk = await client.get("/admin/ping", headers={"Authorization": "Bearer sk-test"})
        assert sk.status_code == 401
        assert sk.json()["code"] == "auth.invalid_token"


async def test_gateway_allows_sk_key(
    sqlite_engine,
    sqlite_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with sqlite_engine.begin() as conn:
        await conn.run_sync(
            lambda sync_conn: Base.metadata.create_all(sync_conn, tables=_TABLES)
        )
    async for client, session in _client(sqlite_session_factory):
        user = User(username="api", employee_no="E-api", status="active")
        plaintext, key_hash, prefix = generate_api_key()
        session.add(user)
        await session.flush()
        key = ApiKey(
            user_id=user.id,
            name="gateway",
            key_hash=key_hash,
            key_prefix=prefix,
            status="active",
        )
        session.add(key)
        await session.commit()

        resp = await client.get("/gateway/ping", headers={"X-API-Key": plaintext})
        assert resp.status_code == 200, resp.text
        assert resp.json()["api_key_id"] == key.id
