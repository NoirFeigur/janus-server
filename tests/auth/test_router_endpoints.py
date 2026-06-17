"""Direct-call coverage for the auth router endpoints (no HTTP transport).

The route tests drive these through ``ASGITransport`` (which corrupts the C
tracer on CPython 3.11, dropping the handler bodies from coverage). Calling the
endpoint coroutines directly with a stub service keeps the tracer armed and
covers the thin envelope-assembly bodies. Behaviour is proven by the service +
route tests; here we assert the wire shape (token passthrough, principal
serialization with stringified snowflake ids).
"""

from __future__ import annotations

from typing import cast

import pytest

from src.auth.router import login, me
from src.auth.schemas import LoginRequest
from src.auth.service import AuthenticatedUser, AuthService

pytestmark = pytest.mark.asyncio

TRACE = "trace-auth"


class _StubAuthService:
    async def authenticate_password(self, username: str, password: str) -> tuple[str, int]:
        assert username == "alice"
        assert password == "secret123"
        return "issued.jwt.token", 7200


async def test_login_endpoint_returns_token_envelope() -> None:
    envelope = await login(
        LoginRequest(username="alice", password="secret123"),
        cast(AuthService, _StubAuthService()),
        TRACE,
    )
    assert envelope.success is True
    assert envelope.trace_id == TRACE
    assert envelope.data is not None
    assert envelope.data.access_token == "issued.jwt.token"
    assert envelope.data.expires_in == 7200
    assert envelope.data.token_type == "Bearer"


async def test_me_endpoint_serializes_principal() -> None:
    current_user = AuthenticatedUser(
        user_id=123456789,
        username="alice",
        department_id=42,
        permissions=frozenset({"system:user:list", "*:*:*"}),
    )
    envelope = await me(current_user, TRACE)
    assert envelope.success is True
    assert envelope.data is not None
    # Snowflake ids serialize as strings on the wire.
    assert envelope.data.user_id == "123456789"
    assert envelope.data.department_id == "42"
    assert envelope.data.username == "alice"
    assert envelope.data.is_superuser is True
    assert envelope.data.permissions == sorted(["system:user:list", "*:*:*"])


async def test_me_endpoint_null_department() -> None:
    current_user = AuthenticatedUser(
        user_id=1,
        username="bob",
        department_id=None,
        permissions=frozenset(),
    )
    envelope = await me(current_user, TRACE)
    assert envelope.data is not None
    assert envelope.data.department_id is None
    assert envelope.data.is_superuser is False
