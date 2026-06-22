"""Route + service tests for admin online-session list / kick (B5).

Route tests drive the real app through ``admin_ctx`` (the AuthMiddleware itself
registers the admin actor's session in the allowlist, so it shows up online).
Service tests drive ``OnlineSessionService`` directly against the in-memory Redis
double + a seeded session, keeping coverage of the service body honest.
"""

from __future__ import annotations

from typing import cast

import pytest
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from src.admin.online.service import OnlineSessionService
from src.core.redis import get_redis
from src.core.session_store import SessionStore
from src.db.models.identity import User
from tests._async_redis_double import AsyncRedisDouble
from tests.admin.conftest import ADMIN_ID, AdminCtx

pytestmark = pytest.mark.asyncio


# ---- route-level: perm gating + envelope ----------------------------------


async def test_list_online_sessions_includes_admin_self(admin_ctx: AdminCtx) -> None:
    """The middleware-registered admin session must appear in the online list."""
    resp = await admin_ctx.client.get("/admin/online/sessions")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["success"] is True
    user_ids = {row["user_id"] for row in body["data"]}
    assert str(ADMIN_ID) in user_ids


async def test_list_online_sessions_resolves_username(admin_ctx: AdminCtx) -> None:
    resp = await admin_ctx.client.get("/admin/online/sessions")
    rows = resp.json()["data"]
    admin_row = next(r for r in rows if r["user_id"] == str(ADMIN_ID))
    assert admin_row["username"] == "admin"


async def test_list_online_sessions_filter_by_user(admin_ctx: AdminCtx) -> None:
    resp = await admin_ctx.client.get(
        "/admin/online/sessions", params={"user_id": ADMIN_ID}
    )
    assert resp.status_code == 200, resp.text
    rows = resp.json()["data"]
    assert rows
    assert all(r["user_id"] == str(ADMIN_ID) for r in rows)


async def test_list_online_sessions_requires_perm(admin_ctx: AdminCtx) -> None:
    admin_ctx.state.perms = {"system:user:list"}  # 缺 system:online:list
    resp = await admin_ctx.client.get("/admin/online/sessions")
    assert resp.status_code == 403
    assert resp.json()["code"] == "auth.forbidden"


async def test_kick_session_revokes_it(admin_ctx: AdminCtx) -> None:
    """Kicking the admin's own jti drops it from the allowlist (next list empty of it)."""
    listing = await admin_ctx.client.get("/admin/online/sessions")
    target_jti = listing.json()["data"][0]["access_jti"]

    resp = await admin_ctx.client.delete(f"/admin/online/sessions/{target_jti}")
    assert resp.status_code == 200, resp.text

    store = SessionStore(get_redis())
    assert await store.is_access_active(target_jti) is False


async def test_kick_session_requires_perm(admin_ctx: AdminCtx) -> None:
    admin_ctx.state.perms = {"system:online:list"}  # 有 list 无 kick
    resp = await admin_ctx.client.delete("/admin/online/sessions/whatever-jti")
    assert resp.status_code == 403
    assert resp.json()["code"] == "auth.forbidden"


async def test_kick_unknown_session_is_idempotent(admin_ctx: AdminCtx) -> None:
    resp = await admin_ctx.client.delete("/admin/online/sessions/never-issued")
    assert resp.status_code == 200, resp.text
    assert resp.json()["success"] is True


# ---- service-level: username resolution + sort ----------------------------


@pytest.fixture
def double() -> AsyncRedisDouble:
    return AsyncRedisDouble(decode_responses=True)


@pytest.fixture
def store(double: AsyncRedisDouble) -> SessionStore:
    return SessionStore(cast(Redis, double))


async def test_service_lists_with_usernames(
    admin_session: AsyncSession, store: SessionStore
) -> None:
    admin_session.add(
        User(id=501, username="bob", employee_no="E-501", status="active")
    )
    await admin_session.commit()
    await store.create_session(
        user_id=501,
        access_jti="acc-bob",
        access_ttl=7200,
        refresh_hash="ref-bob",
        refresh_ttl=1000,
        ip="1.2.3.4",
        user_agent="Mozilla/5.0",
    )
    service = OnlineSessionService(admin_session, store)
    sessions = await service.list_sessions()
    assert len(sessions) == 1
    assert sessions[0].username == "bob"
    assert sessions[0].ip == "1.2.3.4"


async def test_service_username_none_when_user_absent(
    admin_session: AsyncSession, store: SessionStore
) -> None:
    """Session whose user row is missing → username None (no crash)."""
    await store.create_session(
        user_id=999,
        access_jti="acc-ghost",
        access_ttl=7200,
        refresh_hash="ref-ghost",
        refresh_ttl=1000,
    )
    service = OnlineSessionService(admin_session, store)
    sessions = await service.list_sessions()
    assert sessions[0].username is None


async def test_service_username_none_when_user_soft_deleted(
    admin_session: AsyncSession, store: SessionStore
) -> None:
    """A soft-deleted user with a stale Redis session must NOT resolve a username
    — the docstring promises deleted users are absent from the map (P2-1)."""
    admin_session.add(
        User(
            id=801,
            username="ghosted",
            employee_no="E-801",
            status="active",
            is_deleted=True,
        )
    )
    await admin_session.commit()
    await store.create_session(
        user_id=801,
        access_jti="acc-deleted",
        access_ttl=7200,
        refresh_hash="ref-deleted",
        refresh_ttl=1000,
    )
    service = OnlineSessionService(admin_session, store)
    sessions = await service.list_sessions()
    deleted_row = next(s for s in sessions if s.access_jti == "acc-deleted")
    assert deleted_row.username is None


async def test_service_sorts_recent_first(admin_session: AsyncSession, store: SessionStore) -> None:
    """Most-recent login_at surfaces first."""
    await store.create_session(
        user_id=601,
        access_jti="acc-old",
        access_ttl=7200,
        refresh_hash="ref-old",
        refresh_ttl=1000,
    )
    await store.create_session(
        user_id=602,
        access_jti="acc-new",
        access_ttl=7200,
        refresh_hash="ref-new",
        refresh_ttl=1000,
    )
    service = OnlineSessionService(admin_session, store)
    sessions = await service.list_sessions()
    # 两条 login_at 单调:后建的排在前(降序)
    assert sessions[0].login_at >= sessions[-1].login_at


async def test_service_kick_revokes(admin_session: AsyncSession, store: SessionStore) -> None:
    await store.create_session(
        user_id=701,
        access_jti="acc-kick",
        access_ttl=7200,
        refresh_hash="ref-kick",
        refresh_ttl=1000,
    )
    service = OnlineSessionService(admin_session, store)
    await service.kick("acc-kick")
    assert await store.is_access_active("acc-kick") is False
