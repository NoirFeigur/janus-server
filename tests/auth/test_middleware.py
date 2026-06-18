"""Tests for central auth middleware path policy."""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest
from fastapi import FastAPI, Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.auth.middleware import AuthMiddleware
from src.auth.service import AuthService
from src.core.security import generate_api_key, hash_password
from src.db.base import Base
from src.db.models.audit import LoginLog
from src.db.models.credential import ApiKey
from src.db.models.identity import Department, Menu, Role, RoleDept, RoleMenu, User, UserRole

pytestmark = pytest.mark.asyncio

_TABLES = [
    Base.metadata.tables[m.__tablename__]
    for m in (User, Department, Role, Menu, UserRole, RoleMenu, RoleDept, ApiKey, LoginLog)
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

    @app.post("/v1/chat/completions")
    async def openai_chat(request: Request) -> dict[str, int | None]:
        return {"api_key_id": request.state.user.api_key_id}

    @app.post("/v1/messages")
    async def anthropic_messages(request: Request) -> dict[str, int | None]:
        return {"api_key_id": request.state.user.api_key_id}

    @app.post("/v1beta/models/{model_and_action:path}")
    async def gemini_generate(
        model_and_action: str, request: Request
    ) -> dict[str, int | None]:
        return {"api_key_id": request.state.user.api_key_id}

    @app.post("/mcp")
    async def mcp_protocol(request: Request) -> dict[str, int | None]:
        return {"api_key_id": request.state.user.api_key_id}

    @app.get("/gateway/keys")
    async def gateway_resource(request: Request) -> dict[str, int | None]:
        return {"api_key_id": request.state.user.api_key_id}

    @app.get("/v1/messages")
    async def anthropic_messages_resource(request: Request) -> dict[str, int | None]:
        return {"api_key_id": request.state.user.api_key_id}

    @app.get("/mcp/tools")
    async def mcp_resource(request: Request) -> dict[str, int | None]:
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


async def test_llm_and_mcp_protocol_endpoints_allow_sk_key(
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
            name="programmatic",
            key_hash=key_hash,
            key_prefix=prefix,
            status="active",
        )
        session.add(key)
        await session.commit()

        for method, path in (
            ("POST", "/v1/chat/completions"),
            ("POST", "/v1/messages"),
            ("POST", "/v1beta/models/gemini-pro:generateContent"),
            ("POST", "/mcp"),
        ):
            resp = await client.request(method, path, headers={"X-API-Key": plaintext})
            assert resp.status_code == 200, resp.text
            assert resp.json()["api_key_id"] == key.id


async def test_resource_management_paths_reject_sk_key(
    sqlite_engine,
    sqlite_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with sqlite_engine.begin() as conn:
        await conn.run_sync(
            lambda sync_conn: Base.metadata.create_all(sync_conn, tables=_TABLES)
        )
    async for client, _session in _client(sqlite_session_factory):
        for path in ("/gateway/keys", "/mcp/tools", "/v1/messages"):
            resp = await client.get(path, headers={"Authorization": "Bearer sk-test"})
            assert resp.status_code == 401
            assert resp.json()["code"] == "auth.invalid_token"


async def test_revoked_session_rejected_through_middleware(
    sqlite_engine,
    sqlite_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end via the real AuthMiddleware: a logged-in token works, then after
    logout the same token is rejected with ``auth.token_revoked`` (the allowlist
    revocation check fires in the middleware, not just the service)."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from pydantic import SecretStr

    from src.config import get_settings

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    priv_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode("utf-8")
    settings = get_settings()
    monkeypatch.setattr(settings, "platform_jwt_private_key", SecretStr(priv_pem))
    monkeypatch.setattr(settings, "platform_jwt_public_key", None)

    async with sqlite_engine.begin() as conn:
        await conn.run_sync(
            lambda sync_conn: Base.metadata.create_all(sync_conn, tables=_TABLES)
        )
    async for client, session in _client(sqlite_session_factory):
        session.add(
            User(
                username="alice",
                employee_no="E-1",
                status="active",
                password=hash_password("secret123"),
            )
        )
        await session.commit()

        service = AuthService(session)
        token, _, _ = await service.authenticate_password("alice", "secret123")
        headers = {"Authorization": f"Bearer {token}"}

        ok = await client.get("/admin/ping", headers=headers)
        assert ok.status_code == 200

        await service.logout(token)

        revoked = await client.get("/admin/ping", headers=headers)
        assert revoked.status_code == 401
        assert revoked.json()["code"] == "auth.token_revoked"
