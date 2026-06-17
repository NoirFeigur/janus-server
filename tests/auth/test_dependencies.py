"""Tests for auth FastAPI dependencies (credential extraction + perm gate)."""

from __future__ import annotations

import pytest

from src.auth.dependencies import RequiredPerms, _extract_credential
from src.auth.service import AuthenticatedAccount
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
