"""Tests for auth FastAPI dependencies (credential extraction + perm gate)."""

from __future__ import annotations

from typing import cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import Request

from src.auth.constants import SUPERADMIN_ROLE_CODE
from src.auth.credentials import CredentialKind, extract_credential
from src.auth.dependencies import (
    RequiredPerms,
    get_auth_service,
    get_current_jwt_user,
    get_current_user,
)
from src.auth.service import AuthenticatedUser, AuthService
from src.exceptions import AppError


def _user(perms: set[str], role_codes: set[str] | None = None) -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id=1,
        username="alice",
        department_id=None,
        permissions=frozenset(perms),
        role_codes=frozenset(role_codes or set()),
    )


def test_extract_bearer_jwt() -> None:
    cred = extract_credential("Bearer abc.def.ghi", None, allow_api_key=True)
    assert cred.value == "abc.def.ghi"
    assert cred.kind == CredentialKind.jwt


def test_extract_bearer_sk_key_is_api_key() -> None:
    cred = extract_credential("Bearer sk-12345", None, allow_api_key=True)
    assert cred.value == "sk-12345"
    assert cred.kind == CredentialKind.api_key


def test_extract_x_api_key_header_takes_precedence() -> None:
    cred = extract_credential("Bearer jwt-token", "sk-from-header", allow_api_key=True)
    assert cred.value == "sk-from-header"
    assert cred.kind == CredentialKind.api_key


def test_extract_api_key_disallowed_raises_401() -> None:
    with pytest.raises(AppError) as exc:
        extract_credential("Bearer sk-12345", None, allow_api_key=False)
    assert exc.value.status_code == 401


def test_extract_missing_credential_raises_401() -> None:
    with pytest.raises(AppError) as exc:
        extract_credential(None, None, allow_api_key=True)
    assert exc.value.status_code == 401


def test_extract_malformed_authorization_raises_401() -> None:
    with pytest.raises(AppError) as exc:
        extract_credential("Basic foo", None, allow_api_key=True)
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_required_perms_allows_holder() -> None:
    gate = RequiredPerms("system:user:add")
    current_user = _user({"system:user:add"})
    assert await gate(current_user) is current_user


@pytest.mark.asyncio
async def test_required_perms_allows_superuser() -> None:
    gate = RequiredPerms("system:user:add")
    current_user = _user(set(), role_codes={SUPERADMIN_ROLE_CODE})
    assert await gate(current_user) is current_user


@pytest.mark.asyncio
async def test_required_perms_denies_missing_403() -> None:
    gate = RequiredPerms("system:user:remove")
    current_user = _user({"system:user:add"})
    with pytest.raises(AppError) as exc:
        await gate(current_user)
    assert exc.value.status_code == 403


def test_get_auth_service_binds_session() -> None:
    """The factory constructs an AuthService bound to the request session."""
    sentinel = object()
    service = get_auth_service(cast(AsyncSession, sentinel))
    assert isinstance(service, AuthService)
    assert service.repo.session is sentinel


class _StubAuthService:
    """Records which resolution path ``get_current_user`` dispatched to."""

    def __init__(self) -> None:
        self.api_key_calls: list[str] = []
        self.token_calls: list[str] = []

    async def resolve_api_key(self, plaintext: str) -> AuthenticatedUser:
        self.api_key_calls.append(plaintext)
        return _user({"via:api_key"})

    async def resolve_access_token(self, token: str) -> AuthenticatedUser:
        self.token_calls.append(token)
        return _user({"via:jwt"})


@pytest.mark.asyncio
async def test_get_current_user_routes_sk_key_to_api_key_path() -> None:
    stub = _StubAuthService()
    request = Request({"type": "http", "headers": []})
    current_user = await get_current_user(
        request, cast(AuthService, stub), authorization="Bearer sk-live-123"
    )
    assert stub.api_key_calls == ["sk-live-123"]
    assert stub.token_calls == []
    assert current_user.has_permission("via:api_key")


@pytest.mark.asyncio
async def test_get_current_user_routes_jwt_to_access_token_path() -> None:
    stub = _StubAuthService()
    request = Request({"type": "http", "headers": []})
    current_user = await get_current_user(
        request, cast(AuthService, stub), authorization="Bearer header.payload.sig"
    )
    assert stub.token_calls == ["header.payload.sig"]
    assert stub.api_key_calls == []
    assert current_user.has_permission("via:jwt")


@pytest.mark.asyncio
async def test_get_current_user_missing_credential_raises_401() -> None:
    stub = _StubAuthService()
    request = Request({"type": "http", "headers": []})
    with pytest.raises(AppError) as exc:
        await get_current_user(request, cast(AuthService, stub))
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_get_current_jwt_user_rejects_sk_key() -> None:
    stub = _StubAuthService()
    request = Request({"type": "http", "headers": []})
    with pytest.raises(AppError) as exc:
        await get_current_jwt_user(
            request,
            cast(AuthService, stub),
            authorization="Bearer sk-live-123",
        )
    assert exc.value.status_code == 401
    assert stub.api_key_calls == []


@pytest.mark.asyncio
async def test_get_current_jwt_user_uses_request_state_user() -> None:
    stub = _StubAuthService()
    request = Request({"type": "http", "headers": []})
    user = _user({"via:state"})
    request.state.user = user
    resolved = await get_current_jwt_user(request, cast(AuthService, stub))
    assert resolved is user
    assert stub.api_key_calls == []
    assert stub.token_calls == []
