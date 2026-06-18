"""End-to-end session-flow tests over real HTTP (login → refresh → logout).

Drives the *real* ``create_app()`` stack — Locale/TraceId/Auth/AdminAudit
middleware + the auth router — through ``httpx.ASGITransport``. This is the
"use it as a client would" proof for the session-security wave: the allowlist
revocation check, refresh rotation, and reuse detection all fire through the
actual middleware + endpoints, not just the service layer.

The in-memory SQLite engine uses a StaticPool (single shared connection), so the
seeded user is visible to the per-request sessions the middleware/router open.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest
import pytest_asyncio
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from httpx import ASGITransport
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from src.config import get_settings
from src.core.security import hash_password
from src.db.base import Base
from src.db.models.audit import LoginLog
from src.db.models.credential import ApiKey
from src.db.models.identity import Department, Menu, Role, RoleDept, RoleMenu, User, UserRole
from src.db.session import get_session
from src.main import create_app

pytestmark = pytest.mark.asyncio

_TABLES = [
    Base.metadata.tables[m.__tablename__]
    for m in (User, Department, Role, Menu, UserRole, RoleMenu, RoleDept, ApiKey, LoginLog)
]


@pytest_asyncio.fixture
async def client(
    sqlite_engine: AsyncEngine,
    sqlite_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[httpx.AsyncClient]:
    async with sqlite_engine.begin() as conn:
        await conn.run_sync(
            lambda sync_conn: Base.metadata.create_all(sync_conn, tables=_TABLES)
        )

    settings = get_settings()
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    priv_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode("utf-8")
    monkeypatch.setattr(settings, "platform_jwt_private_key", SecretStr(priv_pem))
    monkeypatch.setattr(settings, "platform_jwt_public_key", None)

    async with sqlite_session_factory() as seed:
        seed.add(
            User(
                username="alice",
                employee_no="E-1",
                status="active",
                password=hash_password("secret123"),
            )
        )
        await seed.commit()

    app = create_app()
    app.state.session_factory = sqlite_session_factory

    async def _override_session() -> AsyncIterator[AsyncSession]:
        async with sqlite_session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = _override_session

    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _login(client: httpx.AsyncClient) -> tuple[str, str]:
    resp = await client.post(
        "/auth/login", json={"username": "alice", "password": "secret123"}
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    return data["access_token"], data["refresh_token"]


async def test_login_returns_access_and_refresh(client: httpx.AsyncClient) -> None:
    access, refresh = await _login(client)
    assert access
    assert refresh
    me = await client.get("/auth/me", headers={"Authorization": f"Bearer {access}"})
    assert me.status_code == 200
    assert me.json()["data"]["username"] == "alice"


async def test_refresh_rotates_and_revokes_old_access(
    client: httpx.AsyncClient,
) -> None:
    """Full rotation over HTTP: new access works, old access is revoked."""
    old_access, refresh = await _login(client)
    resp = await client.post("/auth/refresh", json={"refresh_token": refresh})
    assert resp.status_code == 200, resp.text
    new = resp.json()["data"]
    new_access = new["access_token"]
    assert new["refresh_token"] != refresh  # rotated

    ok = await client.get(
        "/auth/me", headers={"Authorization": f"Bearer {new_access}"}
    )
    assert ok.status_code == 200

    revoked = await client.get(
        "/auth/me", headers={"Authorization": f"Bearer {old_access}"}
    )
    assert revoked.status_code == 401
    assert revoked.json()["code"] == "auth.token_revoked"


async def test_logout_then_token_rejected(client: httpx.AsyncClient) -> None:
    access, _ = await _login(client)
    out = await client.post(
        "/auth/logout", headers={"Authorization": f"Bearer {access}"}
    )
    assert out.status_code == 200
    me = await client.get("/auth/me", headers={"Authorization": f"Bearer {access}"})
    assert me.status_code == 401
    assert me.json()["code"] == "auth.token_revoked"


async def test_refresh_reuse_is_rejected_and_kills_session(
    client: httpx.AsyncClient,
) -> None:
    """Replaying a rotated refresh over HTTP fails and revokes the live session."""
    _, refresh = await _login(client)
    first = await client.post("/auth/refresh", json={"refresh_token": refresh})
    assert first.status_code == 200
    live_access = first.json()["data"]["access_token"]

    replay = await client.post("/auth/refresh", json={"refresh_token": refresh})
    assert replay.status_code == 401
    assert replay.json()["code"] == "auth.refresh_invalid"

    # reuse detection revoked the whole family — the live rotated session is dead
    me = await client.get(
        "/auth/me", headers={"Authorization": f"Bearer {live_access}"}
    )
    assert me.status_code == 401
    assert me.json()["code"] == "auth.token_revoked"


async def test_unknown_refresh_rejected(client: httpx.AsyncClient) -> None:
    resp = await client.post(
        "/auth/refresh", json={"refresh_token": "never-issued"}
    )
    assert resp.status_code == 401
    assert resp.json()["code"] == "auth.refresh_invalid"


async def test_repeated_bad_logins_lock_account_over_http(
    client: httpx.AsyncClient,
) -> None:
    """B6 over HTTP: 5 wrong-password POSTs lock the account; the 6th attempt is
    refused with 429 ``auth.account_locked`` even when the password is correct.
    """
    for _ in range(5):
        bad = await client.post(
            "/auth/login", json={"username": "alice", "password": "wrong"}
        )
        assert bad.status_code == 401

    locked = await client.post(
        "/auth/login", json={"username": "alice", "password": "secret123"}
    )
    assert locked.status_code == 429
    assert locked.json()["code"] == "auth.account_locked"


async def test_weak_password_change_rejected_over_http(
    client: httpx.AsyncClient,
) -> None:
    """B7 over HTTP: a weak new password is refused with 400 ``auth.password_too_weak``."""
    access, _ = await _login(client)
    resp = await client.put(
        "/auth/me/password",
        headers={"Authorization": f"Bearer {access}"},
        json={"old_password": "secret123", "new_password": "weak"},
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == "auth.password_too_weak"


async def test_password_change_forces_relogin_over_http(
    client: httpx.AsyncClient,
) -> None:
    """B7 over HTTP: a successful password change revokes the live session — the
    access token that performed the change is rejected on the very next request.
    """
    access, _ = await _login(client)
    changed = await client.put(
        "/auth/me/password",
        headers={"Authorization": f"Bearer {access}"},
        json={"old_password": "secret123", "new_password": "fresh-secret9"},
    )
    assert changed.status_code == 200, changed.text

    # The session that changed the password is now revoked (force re-login).
    me = await client.get("/auth/me", headers={"Authorization": f"Bearer {access}"})
    assert me.status_code == 401
    assert me.json()["code"] == "auth.token_revoked"

    # The new password works for a fresh login.
    relogin = await client.post(
        "/auth/login", json={"username": "alice", "password": "fresh-secret9"}
    )
    assert relogin.status_code == 200, relogin.text


