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

from src.auth.router import change_password, login, me, update_me
from src.auth.schemas import ChangePasswordRequest, CurrentUserUpdate, LoginRequest
from src.auth.service import AuthenticatedUser, AuthService

pytestmark = pytest.mark.asyncio

TRACE = "trace-auth"


class _StubAuthService:
    async def authenticate_password(self, username: str, password: str) -> tuple[str, int]:
        assert username == "alice"
        assert password == "secret123"
        return "issued.jwt.token", 7200

    async def update_current_user(
        self, user: AuthenticatedUser, values: dict[str, str | None]
    ) -> AuthenticatedUser:
        assert values == {"real_name": "Alice R.", "email": None}
        return AuthenticatedUser(
            user_id=user.user_id,
            username=user.username,
            department_id=user.department_id,
            permissions=user.permissions,
            real_name="Alice R.",
            email=None,
            mobile=user.mobile,
            preferred_locale=user.preferred_locale,
        )

    async def change_current_password(
        self, user: AuthenticatedUser, *, old_password: str, new_password: str
    ) -> None:
        assert user.user_id == 123456789
        assert old_password == "old-secret"
        assert new_password == "new-secret"


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
        real_name="Alice",
        email="alice@example.com",
        mobile="13800000000",
        preferred_locale="zh-CN",
    )
    envelope = await me(current_user, TRACE)
    assert envelope.success is True
    assert envelope.data is not None
    # Snowflake ids serialize as strings on the wire.
    assert envelope.data.user_id == "123456789"
    assert envelope.data.department_id == "42"
    assert envelope.data.username == "alice"
    assert envelope.data.real_name == "Alice"
    assert envelope.data.email == "alice@example.com"
    assert envelope.data.mobile == "13800000000"
    assert envelope.data.preferred_locale == "zh-CN"
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


async def test_update_me_endpoint_returns_updated_profile() -> None:
    current_user = AuthenticatedUser(
        user_id=123456789,
        username="alice",
        department_id=42,
        permissions=frozenset({"system:user:list"}),
        real_name="Alice",
        email="old@example.com",
        mobile="13800000000",
        preferred_locale="zh-CN",
    )
    envelope = await update_me(
        CurrentUserUpdate(real_name="Alice R.", email=None),
        current_user,
        cast(AuthService, _StubAuthService()),
        TRACE,
    )
    assert envelope.success is True
    assert envelope.trace_id == TRACE
    assert envelope.data is not None
    assert envelope.data.real_name == "Alice R."
    assert envelope.data.email is None


async def test_change_password_endpoint_returns_empty_success() -> None:
    current_user = AuthenticatedUser(
        user_id=123456789,
        username="alice",
        department_id=42,
        permissions=frozenset({"system:user:list"}),
    )
    envelope = await change_password(
        ChangePasswordRequest(old_password="old-secret", new_password="new-secret"),
        current_user,
        cast(AuthService, _StubAuthService()),
        TRACE,
    )
    assert envelope.success is True
    assert envelope.data is None
    assert envelope.trace_id == TRACE
