"""Tests for SecurityHeadersMiddleware (src/core/security_headers).

Asserts the baseline header floor lands on every response, that the strict API
CSP is applied to API paths but skipped for the docs pages, and that HSTS is
emitted only over HTTPS (direct scheme or X-Forwarded-Proto from the proxy).
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from src.core.security_headers import SecurityHeadersMiddleware

pytestmark = pytest.mark.asyncio


def _app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(SecurityHeadersMiddleware)

    @app.get("/api/thing")
    async def thing() -> dict[str, str]:
        return {"ok": "yes"}

    @app.get("/docs")
    async def docs() -> dict[str, str]:
        return {"docs": "page"}

    return app


@pytest_asyncio.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_baseline_headers_present_on_api(client: httpx.AsyncClient) -> None:
    resp = await client.get("/api/thing")
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert resp.headers["X-Frame-Options"] == "DENY"
    assert resp.headers["Referrer-Policy"] == "no-referrer"
    assert "frame-ancestors 'none'" in resp.headers["Content-Security-Policy"]


async def test_docs_path_exempt_from_csp(client: httpx.AsyncClient) -> None:
    # Swagger/ReDoc inline assets would break under default-src 'none'.
    resp = await client.get("/docs")
    assert "Content-Security-Policy" not in resp.headers
    # The other headers still apply.
    assert resp.headers["X-Content-Type-Options"] == "nosniff"


async def test_hsts_absent_over_http(client: httpx.AsyncClient) -> None:
    resp = await client.get("/api/thing")
    assert "Strict-Transport-Security" not in resp.headers


async def test_hsts_present_with_forwarded_https(client: httpx.AsyncClient) -> None:
    resp = await client.get("/api/thing", headers={"X-Forwarded-Proto": "https"})
    assert "max-age=" in resp.headers["Strict-Transport-Security"]
