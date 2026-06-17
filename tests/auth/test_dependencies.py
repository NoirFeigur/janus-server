"""Tests for auth FastAPI dependencies (credential extraction + perm gate)."""

from __future__ import annotations

from typing import cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth.dependencies import (
    RequiredPerms,
    _extract_credential,
    get_auth_service,
    get_current_account,
)
from src.auth.service import AuthenticatedAccount, AuthService
from src.exceptions import AppError


def _account(perms: set[str]) -> AuthenticatedAccount:
    return AuthenticatedAccount(
        account_id=1,
        username="alice",
        department_id=None,
        permissions=frozenset(perms),
    )


def test_extract_bearer_jwt() -> None:
    cred, is_api_key = _extract_credential("Bearer abc.def.ghi", None)
    assert cred == "abc.def.ghi"
    assert is_api_key is False


def test_extract_bearer_sk_key_is_api_key() -> None:
    cred, is_api_key = _extract_credential("Bearer sk-12345", None)
    assert cred == "sk-12345"
    assert is_api_key is True


def test_extract_x_api_key_header_takes_precedence() -> None:
    cred, is_api_key = _extract_credential("Bearer jwt-token", "sk-from-header")
    assert cred == "sk-from-header"
    assert is_api_key is True


def test_extract_missing_credential_raises_401() -> None:
    with pytest.raises(AppError) as exc:
        _extract_credential(None, None)
    assert exc.value.status_code == 401


def test_extract_malformed_authorization_raises_401() -> None:
    with pytest.raises(AppError) as exc:
        _extract_credential("Basic foo", None)
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_required_perms_allows_holder() -> None:
    gate = RequiredPerms("system:user:add")
    account = _account({"system:user:add"})
    assert await gate(account) is account


@pytest.mark.asyncio
async def test_required_perms_allows_superuser() -> None:
    gate = RequiredPerms("system:user:add")
    account = _account({"*:*:*"})
    assert await gate(account) is account


@pytest.mark.asyncio
async def test_required_perms_denies_missing_403() -> None:
    gate = RequiredPerms("system:user:remove")
    account = _account({"system:user:add"})
    with pytest.raises(AppError) as exc:
        await gate(account)
    assert exc.value.status_code == 403


def test_get_auth_service_binds_session() -> None:
    """The factory constructs an AuthService bound to the request session."""
    sentinel = object()
    service = get_auth_service(cast(AsyncSession, sentinel))
    assert isinstance(service, AuthService)
    assert service.repo.session is sentinel


class _StubAuthService:
    """Records which resolution path ``get_current_account`` dispatched to."""

    def __init__(self) -> None:
        self.api_key_calls: list[str] = []
        self.token_calls: list[str] = []

    async def resolve_api_key(self, plaintext: str) -> AuthenticatedAccount:
        self.api_key_calls.append(plaintext)
        return _account({"via:api_key"})

    async def resolve_access_token(self, token: str) -> AuthenticatedAccount:
        self.token_calls.append(token)
        return _account({"via:jwt"})


@pytest.mark.asyncio
async def test_get_current_account_routes_sk_key_to_api_key_path() -> None:
    stub = _StubAuthService()
    account = await get_current_account(
        cast(AuthService, stub), authorization="Bearer sk-live-123"
    )
    assert stub.api_key_calls == ["sk-live-123"]
    assert stub.token_calls == []
    assert account.has_permission("via:api_key")


@pytest.mark.asyncio
async def test_get_current_account_routes_jwt_to_access_token_path() -> None:
    stub = _StubAuthService()
    account = await get_current_account(
        cast(AuthService, stub), authorization="Bearer header.payload.sig"
    )
    assert stub.token_calls == ["header.payload.sig"]
    assert stub.api_key_calls == []
    assert account.has_permission("via:jwt")


@pytest.mark.asyncio
async def test_get_current_account_missing_credential_raises_401() -> None:
    stub = _StubAuthService()
    with pytest.raises(AppError) as exc:
        await get_current_account(cast(AuthService, stub))
    assert exc.value.status_code == 401
