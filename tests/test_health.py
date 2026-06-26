"""Tests for health probes (src/main.py).

liveness is a static {"status":"ok"}. readiness actually probes PG + Redis and
returns 503 if either is down. We monkeypatch the module-level ``engine`` and
``redis_ping`` so the shared instances are never touched, and assert the
behavior contract: all-up → 200, any-down → 503 with per-dependency checks.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine

import src.main as main_module
from src.main import app, create_app, lifespan


@pytest_asyncio.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_liveness_returns_ok(client: httpx.AsyncClient) -> None:
    resp = await client.get("/health/live")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


async def test_readiness_all_up_returns_200(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PG + Redis 均可达 → 200,checks 全 ok。"""
    sqlite = create_async_engine("sqlite+aiosqlite:///:memory:")

    async def _ok_ping() -> bool:
        return True

    monkeypatch.setattr(main_module, "engine", sqlite)
    monkeypatch.setattr(main_module, "redis_ping", _ok_ping)
    try:
        resp = await client.get("/health/ready")
    finally:
        await sqlite.dispose()
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["checks"] == {"postgres": "ok", "redis": "ok"}


async def test_readiness_redis_down_returns_503(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Redis 不可达 → 503,postgres ok 但 redis down。"""
    sqlite = create_async_engine("sqlite+aiosqlite:///:memory:")

    async def _fail_ping() -> bool:
        raise ConnectionError("redis unreachable")

    monkeypatch.setattr(main_module, "engine", sqlite)
    monkeypatch.setattr(main_module, "redis_ping", _fail_ping)
    try:
        resp = await client.get("/health/ready")
    finally:
        await sqlite.dispose()
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "unavailable"
    assert body["checks"]["postgres"] == "ok"
    assert body["checks"]["redis"] == "down"


async def test_readiness_postgres_down_returns_503(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PG 不可达 → 503,redis ok 但 postgres down。"""

    class _BrokenEngine:
        def connect(self) -> object:
            raise ConnectionError("pg unreachable")

    async def _ok_ping() -> bool:
        return True

    monkeypatch.setattr(main_module, "engine", _BrokenEngine())
    monkeypatch.setattr(main_module, "redis_ping", _ok_ping)
    resp = await client.get("/health/ready")
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "unavailable"
    assert body["checks"]["postgres"] == "down"
    assert body["checks"]["redis"] == "ok"


async def test_lifespan_startup_and_shutdown_run() -> None:
    """The lifespan context configures logging on entry and closes Redis on
    exit. The autouse ``fake_redis`` fixture backs ``close_redis`` so no shared
    instance is touched."""
    test_app = create_app()
    async with lifespan(test_app):
        # Inside the context the app is "started"; nothing to assert beyond
        # the body having executed without raising (yield reached).
        pass
    # Exiting the context ran the shutdown branch (await close_redis()).


async def test_lifespan_enables_chat_completions_for_anthropic_messages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: lifespan must flip litellm to route anthropic_messages
    through /v1/chat/completions instead of /v1/responses.

    Without this flag, litellm force-routes the ``openai`` provider's
    anthropic_messages (the /v1/messages client entry) to the Responses API and
    then parses the reply with the chat-completion parser, choking on every
    OpenAI-compatible upstream (deepseek/glm/qwen/...) -> 502 upstream.error.
    We assert lifespan sets the flag regardless of its prior value (start from
    False to prove lifespan, not a stale global, is what turns it on).

    The heavy startup steps (worker-id lease, RouterManager) are stubbed: they
    do real Redis/DB I/O and bind global async primitives to the test loop,
    which is orthogonal to the flag this test guards. The flag is set early in
    lifespan (before those steps), so stubbing them keeps the test focused and
    loop-independent."""
    import litellm

    async def _no_worker_id() -> None:
        return None

    async def _noop_startup(*args: object, **kwargs: object) -> None:
        return None

    monkeypatch.setattr(
        litellm, "use_chat_completions_url_for_anthropic_messages", False
    )
    monkeypatch.setattr(main_module, "acquire_worker_id", _no_worker_id)
    monkeypatch.setattr(main_module.RouterManager, "startup", _noop_startup)
    monkeypatch.setattr(main_module.RouterManager, "shutdown", _noop_startup)

    test_app = create_app()
    async with lifespan(test_app):
        assert litellm.use_chat_completions_url_for_anthropic_messages is True



async def test_readiness_success_path_direct(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drive the readiness handler directly (not via ASGITransport) so the C
    tracer stays armed and the ``SELECT 1`` execution line is covered."""
    sqlite = create_async_engine("sqlite+aiosqlite:///:memory:")

    async def _ok_ping() -> bool:
        return True

    monkeypatch.setattr(main_module, "engine", sqlite)
    monkeypatch.setattr(main_module, "redis_ping", _ok_ping)

    # Pull the readiness closure off a freshly built app and call it directly.
    test_app = create_app()
    readiness = next(
        r.endpoint
        for r in test_app.routes
        if getattr(r, "path", None) == "/health/ready"
    )
    try:
        resp = await readiness()
    finally:
        await sqlite.dispose()
    assert resp.status_code == 200


def _has_route(app_obj: object, path: str) -> bool:
    return any(getattr(r, "path", None) == path for r in app_obj.routes)  # type: ignore[attr-defined]


def test_docs_enabled_in_local(monkeypatch: pytest.MonkeyPatch) -> None:
    """local exposes the interactive docs (developer convenience)."""
    from src.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("JANUS_ENVIRONMENT", "local")
    try:
        test_app = create_app()
    finally:
        get_settings.cache_clear()
    assert test_app.docs_url == "/docs"
    assert test_app.redoc_url == "/redoc"
    assert test_app.openapi_url == "/openapi.json"
    assert _has_route(test_app, "/openapi.json")


def test_docs_disabled_outside_local(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-local replica must not publish its API map: docs/redoc/openapi off.

    Swagger/ReDoc/openapi.json expose the entire internal API surface (admin
    endpoints, params, error codes) to anyone who can reach the replica, so a
    production build disables all three — the routes are never registered.
    """
    from src.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("JANUS_ENVIRONMENT", "production")
    try:
        test_app = create_app()
    finally:
        get_settings.cache_clear()
    assert test_app.docs_url is None
    assert test_app.redoc_url is None
    assert test_app.openapi_url is None
    # The schema route itself must be absent (not merely hidden).
    assert not _has_route(test_app, "/openapi.json")
