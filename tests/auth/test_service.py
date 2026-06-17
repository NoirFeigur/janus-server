"""Tests for ``AuthService`` (login / sk-key / RBAC / data-scope)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth.service import AuthenticatedAccount, AuthService
from src.core.security import decode_access_token, generate_api_key
from src.db.models.credential import ApiKey
from src.db.models.identity import Department, Role, UserRole
from src.exceptions import AppError
from tests.auth.conftest import grant_permission, seed_user

pytestmark = pytest.mark.asyncio


async def test_authenticate_password_success_issues_token(
    auth_session: AsyncSession,
) -> None:
    user = await seed_user(auth_session, password="hunter2")
    service = AuthService(auth_session)
    token, ttl = await service.authenticate_password("alice", "hunter2")
    assert ttl > 0
    claims = decode_access_token(token)
    assert claims.sub == str(user.id)


async def test_authenticate_password_wrong_password_raises(
    auth_session: AsyncSession,
) -> None:
    await seed_user(auth_session, password="hunter2")
    service = AuthService(auth_session)
    with pytest.raises(AppError) as exc:
        await service.authenticate_password("alice", "wrong")
    assert exc.value.status_code == 401


async def test_authenticate_unknown_user_raises(auth_session: AsyncSession) -> None:
    service = AuthService(auth_session)
    with pytest.raises(AppError) as exc:
        await service.authenticate_password("ghost", "whatever")
    assert exc.value.status_code == 401


async def test_authenticate_sso_only_user_no_password_raises(
    auth_session: AsyncSession,
) -> None:
    await seed_user(auth_session, username="bob", password=None)
    service = AuthService(auth_session)
    with pytest.raises(AppError):
        await service.authenticate_password("bob", "anything")


async def test_resolve_access_token_builds_principal_with_perms(
    auth_session: AsyncSession,
) -> None:
    user = await seed_user(auth_session)
    await grant_permission(auth_session, user=user, perm="system:user:list")
    service = AuthService(auth_session)
    token, _ = await service.authenticate_password("alice", "secret123")
    account = await service.resolve_access_token(token)
    assert account.account_id == user.id
    assert account.has_permission("system:user:list")
    assert not account.has_permission("system:user:remove")


async def test_resolve_access_token_invalid_raises(auth_session: AsyncSession) -> None:
    service = AuthService(auth_session)
    with pytest.raises(AppError) as exc:
        await service.resolve_access_token("not-a-jwt")
    assert exc.value.status_code == 401


async def test_superuser_wildcard_grants_everything(
    auth_session: AsyncSession,
) -> None:
    user = await seed_user(auth_session)
    await grant_permission(auth_session, user=user, perm="*:*:*")
    service = AuthService(auth_session)
    token, _ = await service.authenticate_password("alice", "secret123")
    account = await service.resolve_access_token(token)
    assert account.is_superuser
    assert account.has_permission("anything:at:all")


async def test_resolve_api_key_success(auth_session: AsyncSession) -> None:
    user = await seed_user(auth_session)
    plaintext, key_hash, prefix = generate_api_key()
    auth_session.add(
        ApiKey(
            user_id=user.id,
            name="dev",
            key_hash=key_hash,
            key_prefix=prefix,
            status="active",
        )
    )
    await auth_session.flush()
    service = AuthService(auth_session)
    account = await service.resolve_api_key(plaintext)
    assert account.account_id == user.id


async def test_resolve_api_key_expired_raises(auth_session: AsyncSession) -> None:
    user = await seed_user(auth_session)
    plaintext, key_hash, prefix = generate_api_key()
    auth_session.add(
        ApiKey(
            user_id=user.id,
            name="dev",
            key_hash=key_hash,
            key_prefix=prefix,
            status="active",
            expires_at=datetime.now(tz=UTC) - timedelta(hours=1),
        )
    )
    await auth_session.flush()
    service = AuthService(auth_session)
    with pytest.raises(AppError) as exc:
        await service.resolve_api_key(plaintext)
    assert exc.value.status_code == 401


async def test_resolve_api_key_unknown_raises(auth_session: AsyncSession) -> None:
    service = AuthService(auth_session)
    with pytest.raises(AppError):
        await service.resolve_api_key("sk-does-not-exist")


async def test_require_permission_passes_and_fails(auth_session: AsyncSession) -> None:
    user = await seed_user(auth_session)
    await grant_permission(auth_session, user=user, perm="system:role:add")
    service = AuthService(auth_session)
    token, _ = await service.authenticate_password("alice", "secret123")
    account = await service.resolve_access_token(token)
    service.require_permission(account, "system:role:add")  # no raise
    with pytest.raises(AppError) as exc:
        service.require_permission(account, "system:role:remove")
    assert exc.value.status_code == 403


# ---- data scope ------------------------------------------------------------


async def _principal_with_role(
    session: AsyncSession, *, data_scope: str, department_id: int | None
) -> tuple[AuthService, int]:
    """Seed a user with one role of the given scope; return (service, account_id)."""
    user = await seed_user(session, department_id=department_id)
    role = Role(name="ds", code="ds", data_scope=data_scope, status="active")
    session.add(role)
    await session.flush()
    session.add(UserRole(user_id=user.id, role_id=role.id))
    await session.flush()
    return AuthService(session), user.id


async def test_data_scope_no_roles_is_self_only(auth_session: AsyncSession) -> None:
    user = await seed_user(auth_session)
    service = AuthService(auth_session)
    account = AuthenticatedAccount(
        account_id=user.id,
        username="alice",
        department_id=None,
        permissions=frozenset({"system:user:list"}),  # not superuser
    )
    scope = await service.resolve_data_scope(account)
    assert not scope.unrestricted
    assert scope.include_self
    assert scope.department_ids == frozenset()


async def test_data_scope_all_is_unrestricted(auth_session: AsyncSession) -> None:
    service, account_id = await _principal_with_role(
        auth_session, data_scope="all", department_id=1
    )
    token, _ = await service.authenticate_password("alice", "secret123")
    account = await service.resolve_access_token(token)
    scope = await service.resolve_data_scope(account)
    assert scope.unrestricted


async def test_data_scope_dept_only_includes_own_dept(
    auth_session: AsyncSession,
) -> None:
    service, account_id = await _principal_with_role(
        auth_session, data_scope="dept", department_id=42
    )
    token, _ = await service.authenticate_password("alice", "secret123")
    account = await service.resolve_access_token(token)
    scope = await service.resolve_data_scope(account)
    assert scope.department_ids == frozenset({42})
    assert not scope.unrestricted


async def test_data_scope_dept_and_child_includes_subtree(
    auth_session: AsyncSession,
) -> None:
    # Department tree: 1 -> 2 -> 3 ; user in dept 1, scope dept_and_child.
    auth_session.add_all(
        [
            Department(id=1, name="root", parent_id=None),
            Department(id=2, name="child", parent_id=1),
            Department(id=3, name="grandchild", parent_id=2),
            Department(id=9, name="other", parent_id=None),
        ]
    )
    await auth_session.flush()
    service, _ = await _principal_with_role(
        auth_session, data_scope="dept_and_child", department_id=1
    )
    token, _ = await service.authenticate_password("alice", "secret123")
    account = await service.resolve_access_token(token)
    scope = await service.resolve_data_scope(account)
    assert scope.department_ids == frozenset({1, 2, 3})
    assert 9 not in scope.department_ids
