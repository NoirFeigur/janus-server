"""Tests for ``AuthService`` (login / sk-key / RBAC / data-scope)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from src.admin.departments.schemas import DepartmentUpdate
from src.admin.departments.service import DepartmentService
from src.auth.constants import SUPERADMIN_ROLE_CODE
from src.auth.service import (
    AuthenticatedUser,
    AuthService,
    invalidate_department_tree,
)
from src.core.security import decode_access_token, generate_api_key, verify_password_async
from src.db.models.credential import ApiKey
from src.db.models.identity import Department, Role, RoleDept, UserRole
from src.enums import DataScope, ErrorCode
from src.exceptions import AppError
from tests.auth.conftest import grant_permission, seed_user

pytestmark = pytest.mark.asyncio


async def test_authenticate_password_success_issues_token(
    auth_session: AsyncSession,
) -> None:
    user = await seed_user(auth_session, password="hunter2")
    service = AuthService(auth_session)
    token, ttl, refresh = await service.authenticate_password("alice", "hunter2")
    assert ttl > 0
    assert refresh  # login also mints an opaque refresh token
    claims = decode_access_token(token)
    assert claims.sub == str(user.id)
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
    user.real_name = "Alice"
    user.email = "alice@example.com"
    user.mobile = "13800000000"
    user.preferred_locale = "en-US"
    await auth_session.flush()
    await grant_permission(auth_session, user=user, perm="system:user:list")
    service = AuthService(auth_session)
    token, _, _ = await service.authenticate_password("alice", "secret123")
    current_user = await service.resolve_access_token(token)
    assert current_user.user_id == user.id
    assert current_user.real_name == "Alice"
    assert current_user.email == "alice@example.com"
    assert current_user.mobile == "13800000000"
    assert current_user.preferred_locale == "en-US"
    assert current_user.has_permission("system:user:list")
    assert not current_user.has_permission("system:user:remove")


async def test_update_current_user_changes_profile(
    auth_session: AsyncSession,
) -> None:
    user = await seed_user(auth_session)
    service = AuthService(auth_session)
    current_user = AuthenticatedUser(
        user_id=user.id,
        username="alice",
        department_id=None,
        permissions=frozenset(),
    )
    updated = await service.update_current_user(
        current_user,
        {
            "real_name": "Alice R.",
            "email": "alice@example.com",
            "mobile": None,
            "preferred_locale": "en-US",
        },
    )
    assert updated.real_name == "Alice R."
    assert updated.email == "alice@example.com"
    assert updated.mobile is None
    assert updated.preferred_locale == "en-US"
    assert user.updated_by == user.id


async def test_update_current_user_rejects_null_locale(
    auth_session: AsyncSession,
) -> None:
    user = await seed_user(auth_session)
    service = AuthService(auth_session)
    current_user = AuthenticatedUser(
        user_id=user.id,
        username="alice",
        department_id=None,
        permissions=frozenset(),
    )
    with pytest.raises(AppError) as exc:
        await service.update_current_user(current_user, {"preferred_locale": None})
    assert exc.value.status_code == 400


async def test_change_current_password_rehashes_password(
    auth_session: AsyncSession,
) -> None:
    user = await seed_user(auth_session, password="old-secret")
    service = AuthService(auth_session)
    current_user = AuthenticatedUser(
        user_id=user.id,
        username="alice",
        department_id=None,
        permissions=frozenset(),
    )
    await service.change_current_password(
        current_user, old_password="old-secret", new_password="new-secret1"
    )
    assert user.password is not None
    assert await verify_password_async(user.password, "new-secret1")
    assert not await verify_password_async(user.password, "old-secret")
    assert user.updated_by == user.id


async def test_change_current_password_wrong_old_password_raises(
    auth_session: AsyncSession,
) -> None:
    user = await seed_user(auth_session, password="old-secret")
    service = AuthService(auth_session)
    current_user = AuthenticatedUser(
        user_id=user.id,
        username="alice",
        department_id=None,
        permissions=frozenset(),
    )
    with pytest.raises(AppError) as exc:
        await service.change_current_password(
            current_user, old_password="bad", new_password="new-secret1"
        )
    assert exc.value.status_code == 401


async def test_change_current_password_sso_only_user_raises(
    auth_session: AsyncSession,
) -> None:
    user = await seed_user(auth_session, password=None)
    service = AuthService(auth_session)
    current_user = AuthenticatedUser(
        user_id=user.id,
        username="alice",
        department_id=None,
        permissions=frozenset(),
    )
    with pytest.raises(AppError) as exc:
        await service.change_current_password(
            current_user, old_password="old", new_password="new-secret1"
        )
    assert exc.value.status_code == 401


async def test_resolve_access_token_invalid_raises(auth_session: AsyncSession) -> None:
    service = AuthService(auth_session)
    with pytest.raises(AppError) as exc:
        await service.resolve_access_token("not-a-jwt")
    assert exc.value.status_code == 401


async def test_login_registers_active_session(auth_session: AsyncSession) -> None:
    """A freshly logged-in token's jti is in the allowlist, so resolve succeeds."""
    user = await seed_user(auth_session, password="secret123")
    service = AuthService(auth_session)
    token, _, _ = await service.authenticate_password("alice", "secret123")
    claims = decode_access_token(token)
    assert await service.sessions.is_access_active(claims.jti) is True
    resolved = await service.resolve_access_token(token)
    assert resolved.user_id == user.id


async def test_revoked_token_raises_token_revoked(auth_session: AsyncSession) -> None:
    """A cryptographically valid token whose session was dropped fails with revoked."""
    await seed_user(auth_session, password="secret123")
    service = AuthService(auth_session)
    token, _, _ = await service.authenticate_password("alice", "secret123")
    claims = decode_access_token(token)
    await service.sessions.revoke_access(claims.jti)  # simulate logout/kick
    with pytest.raises(AppError) as exc:
        await service.resolve_access_token(token)
    assert exc.value.status_code == 401
    assert exc.value.code is ErrorCode.auth_token_revoked


async def test_logout_revokes_session(auth_session: AsyncSession) -> None:
    """logout drops the session so the same token no longer resolves."""
    await seed_user(auth_session, password="secret123")
    service = AuthService(auth_session)
    token, _, _ = await service.authenticate_password("alice", "secret123")
    await service.logout(token)
    with pytest.raises(AppError) as exc:
        await service.resolve_access_token(token)
    assert exc.value.code is ErrorCode.auth_token_revoked


async def test_logout_is_idempotent(auth_session: AsyncSession) -> None:
    """A second logout of the same token is a no-op (no raise)."""
    await seed_user(auth_session, password="secret123")
    service = AuthService(auth_session)
    token, _, _ = await service.authenticate_password("alice", "secret123")
    await service.logout(token)
    await service.logout(token)  # already revoked — must not raise


async def test_logout_undecodable_token_raises(auth_session: AsyncSession) -> None:
    service = AuthService(auth_session)
    with pytest.raises(AppError) as exc:
        await service.logout("not-a-jwt")
    assert exc.value.status_code == 401


async def test_refresh_rotates_into_new_pair(auth_session: AsyncSession) -> None:
    """A valid refresh yields a brand-new access+refresh, both usable."""
    user = await seed_user(auth_session, password="secret123")
    service = AuthService(auth_session)
    _, _, refresh = await service.authenticate_password("alice", "secret123")
    new_token, ttl, new_refresh = await service.refresh_session(refresh)
    assert ttl > 0
    assert new_refresh and new_refresh != refresh  # rotated
    resolved = await service.resolve_access_token(new_token)
    assert resolved.user_id == user.id


async def test_refresh_revokes_old_access(auth_session: AsyncSession) -> None:
    """After rotation the access token bound to the old refresh stops working."""
    await seed_user(auth_session, password="secret123")
    service = AuthService(auth_session)
    old_token, _, refresh = await service.authenticate_password("alice", "secret123")
    await service.refresh_session(refresh)
    with pytest.raises(AppError) as exc:
        await service.resolve_access_token(old_token)
    assert exc.value.code is ErrorCode.auth_token_revoked


async def test_refresh_new_token_can_rotate_again(auth_session: AsyncSession) -> None:
    """The rotated-out refresh chains: the new refresh rotates a second time."""
    await seed_user(auth_session, password="secret123")
    service = AuthService(auth_session)
    _, _, refresh1 = await service.authenticate_password("alice", "secret123")
    _, _, refresh2 = await service.refresh_session(refresh1)
    token3, _, _ = await service.refresh_session(refresh2)
    assert (await service.resolve_access_token(token3)) is not None


async def test_refresh_unknown_token_raises_refresh_invalid(
    auth_session: AsyncSession,
) -> None:
    service = AuthService(auth_session)
    with pytest.raises(AppError) as exc:
        await service.refresh_session("never-issued-refresh")
    assert exc.value.status_code == 401
    assert exc.value.code is ErrorCode.auth_refresh_invalid


async def test_refresh_reuse_revokes_session_and_raises(
    auth_session: AsyncSession,
) -> None:
    """Replaying an already-rotated refresh is rejected AND revokes the session.

    The first rotation consumes ``refresh``; presenting it again is the classic
    stolen-token signal — the store revokes the whole user session (kicking the
    live rotated session too) and the call fails with ``auth_refresh_invalid``.
    """
    await seed_user(auth_session, password="secret123")
    service = AuthService(auth_session)
    _, _, refresh = await service.authenticate_password("alice", "secret123")
    live_token, _, _ = await service.refresh_session(refresh)  # first rotation
    # the rotated session works right now
    assert (await service.resolve_access_token(live_token)) is not None
    with pytest.raises(AppError) as exc:
        await service.refresh_session(refresh)  # replay the consumed refresh
    assert exc.value.code is ErrorCode.auth_refresh_invalid
    # reuse detection nuked the whole family — the live rotated session is dead too
    with pytest.raises(AppError) as kicked:
        await service.resolve_access_token(live_token)
    assert kicked.value.code is ErrorCode.auth_token_revoked


async def test_superuser_by_role_code_grants_everything(
    auth_session: AsyncSession,
) -> None:
    user = await seed_user(auth_session)
    # A role whose code is the super-admin marker — no wildcard perm involved.
    await grant_permission(
        auth_session, user=user, perm="some:narrow:perm", role_code=SUPERADMIN_ROLE_CODE
    )
    service = AuthService(auth_session)
    token, _, _ = await service.authenticate_password("alice", "secret123")
    current_user = await service.resolve_access_token(token)
    assert current_user.is_superuser
    assert current_user.has_permission("anything:at:all")


async def test_wildcard_perm_is_not_superuser(auth_session: AsyncSession) -> None:
    """A plain ``*:*:*`` permission no longer confers super-admin (code-only)."""
    user = await seed_user(auth_session)
    await grant_permission(auth_session, user=user, perm="*:*:*", role_code="ops")
    service = AuthService(auth_session)
    token, _, _ = await service.authenticate_password("alice", "secret123")
    current_user = await service.resolve_access_token(token)
    assert current_user.is_superuser is False
    # The literal code still matches itself, but it is not a god-mode wildcard.
    assert current_user.has_permission("*:*:*")
    assert current_user.has_permission("other:perm") is False


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
    current_user = await service.resolve_api_key(plaintext)
    assert current_user.user_id == user.id


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
    token, _, _ = await service.authenticate_password("alice", "secret123")
    current_user = await service.resolve_access_token(token)
    service.require_permission(current_user, "system:role:add")  # no raise
    with pytest.raises(AppError) as exc:
        service.require_permission(current_user, "system:role:remove")
    assert exc.value.status_code == 403


# ---- data scope ------------------------------------------------------------


async def _principal_with_role(
    session: AsyncSession, *, data_scope: str, department_id: int | None
) -> tuple[AuthService, int]:
    """Seed a user with one role of the given scope; return (service, user_id)."""
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
    current_user = AuthenticatedUser(
        user_id=user.id,
        username="alice",
        department_id=None,
        permissions=frozenset({"system:user:list"}),  # not superuser
    )
    scope = await service.resolve_data_scope(current_user)
    assert not scope.unrestricted
    assert scope.include_self
    assert scope.department_ids == frozenset()


async def test_data_scope_all_is_unrestricted(auth_session: AsyncSession) -> None:
    service, user_id = await _principal_with_role(
        auth_session, data_scope="all", department_id=1
    )
    token, _, _ = await service.authenticate_password("alice", "secret123")
    current_user = await service.resolve_access_token(token)
    scope = await service.resolve_data_scope(current_user)
    assert scope.unrestricted


async def test_data_scope_dept_only_includes_own_dept(
    auth_session: AsyncSession,
) -> None:
    service, user_id = await _principal_with_role(
        auth_session, data_scope="dept", department_id=42
    )
    token, _, _ = await service.authenticate_password("alice", "secret123")
    current_user = await service.resolve_access_token(token)
    scope = await service.resolve_data_scope(current_user)
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
    token, _, _ = await service.authenticate_password("alice", "secret123")
    current_user = await service.resolve_access_token(token)
    scope = await service.resolve_data_scope(current_user)
    assert scope.department_ids == frozenset({1, 2, 3})
    assert 9 not in scope.department_ids


async def test_data_scope_caches_dept_tree_until_invalidated(
    auth_session: AsyncSession,
) -> None:
    """The dept tree is cached: a new descendant added behind the cache is NOT
    seen until the cache is invalidated (the contract dept mutations rely on)."""
    auth_session.add_all(
        [
            Department(id=1, name="root", parent_id=None),
            Department(id=2, name="child", parent_id=1),
        ]
    )
    await auth_session.flush()
    service, _ = await _principal_with_role(
        auth_session, data_scope="dept_and_child", department_id=1
    )
    token, _, _ = await service.authenticate_password("alice", "secret123")
    current_user = await service.resolve_access_token(token)

    # Prime the cache with the {1, 2} tree.
    first = await service.resolve_data_scope(current_user)
    assert first.department_ids == frozenset({1, 2})

    # Add a grandchild straight into the DB, bypassing DepartmentService (so no
    # invalidation fires). The cached tree must still be served — proves caching.
    auth_session.add(Department(id=3, name="grandchild", parent_id=2))
    await auth_session.flush()
    stale = await service.resolve_data_scope(current_user)
    assert stale.department_ids == frozenset({1, 2})  # 3 not visible: cache hit

    # Invalidate (what every dept mutation does after commit) → fresh read.
    await invalidate_department_tree()
    fresh = await service.resolve_data_scope(current_user)
    assert fresh.department_ids == frozenset({1, 2, 3})


async def test_department_mutation_invalidates_scope_cache(
    auth_session: AsyncSession,
) -> None:
    """End-to-end: a DepartmentService mutation drops the cached tree, so the
    next data-scope resolution reflects the structural change immediately."""
    auth_session.add_all(
        [
            Department(id=1, name="root", parent_id=None),
            Department(id=2, name="child", parent_id=1),
        ]
    )
    await auth_session.flush()
    service, _ = await _principal_with_role(
        auth_session, data_scope="dept_and_child", department_id=1
    )
    token, _, _ = await service.authenticate_password("alice", "secret123")
    current_user = await service.resolve_access_token(token)

    primed = await service.resolve_data_scope(current_user)
    assert primed.department_ids == frozenset({1, 2})

    # Reparent dept 2 to be a sibling of root via the real service path.
    dept_service = DepartmentService(auth_session)
    await dept_service.update_department(
        2,
        DepartmentUpdate(parent_id=None),
        actor=AuthenticatedUser(
            user_id=1,
            username="admin",
            department_id=None,
            permissions=frozenset(),
            role_codes=frozenset({SUPERADMIN_ROLE_CODE}),
        ),
    )

    # Cache was invalidated on commit → dept 2 no longer in dept 1's subtree.
    after = await service.resolve_data_scope(current_user)
    assert after.department_ids == frozenset({1})


# ---- _build_user: credential valid but user gone ----------------------------


async def test_build_user_missing_user_raises_401(
    auth_session: AsyncSession,
) -> None:
    service = AuthService(auth_session)
    with pytest.raises(AppError) as exc:
        await service._build_user(999999)  # no such user
    assert exc.value.status_code == 401


# ---- accumulate-scope branch coverage ---------------------------------------


async def test_data_scope_custom_includes_granted_depts(
    auth_session: AsyncSession,
) -> None:
    """custom-scope role contributes its sys_role_dept grants to the scope."""
    auth_session.add_all(
        [Department(id=d, name=f"d{d}", parent_id=None) for d in (71, 72)]
    )
    user = await seed_user(auth_session, department_id=None)
    role = Role(name="cu", code="cu", data_scope=DataScope.custom.value, status="active")
    auth_session.add(role)
    await auth_session.flush()
    auth_session.add(UserRole(user_id=user.id, role_id=role.id))
    auth_session.add_all(
        [RoleDept(role_id=role.id, dept_id=71), RoleDept(role_id=role.id, dept_id=72)]
    )
    await auth_session.flush()
    service = AuthService(auth_session)
    current_user = AuthenticatedUser(
        user_id=user.id,
        username="alice",
        department_id=None,
        permissions=frozenset({"x:y:z"}),  # not superuser
    )
    scope = await service.resolve_data_scope(current_user)
    assert scope.department_ids == frozenset({71, 72})
    assert not scope.unrestricted


async def test_data_scope_self_only_role_sets_include_self(
    auth_session: AsyncSession,
) -> None:
    """A self-scope role (with other roles present) sets include_self via accumulate."""
    service, _ = await _principal_with_role(
        auth_session, data_scope=DataScope.self_only.value, department_id=5
    )
    token, _, _ = await service.authenticate_password("alice", "secret123")
    current_user = await service.resolve_access_token(token)
    scope = await service.resolve_data_scope(current_user)
    assert scope.include_self
    assert scope.department_ids == frozenset()
    assert not scope.unrestricted


async def test_data_scope_dept_and_child_or_self_combines(
    auth_session: AsyncSession,
) -> None:
    """dept_and_child_or_self adds the own-dept subtree AND sets include_self."""
    auth_session.add_all(
        [
            Department(id=1, name="root", parent_id=None),
            Department(id=2, name="child", parent_id=1),
        ]
    )
    await auth_session.flush()
    service, _ = await _principal_with_role(
        auth_session,
        data_scope=DataScope.dept_and_child_or_self.value,
        department_id=1,
    )
    token, _, _ = await service.authenticate_password("alice", "secret123")
    current_user = await service.resolve_access_token(token)
    scope = await service.resolve_data_scope(current_user)
    assert scope.department_ids == frozenset({1, 2})
    assert scope.include_self
    assert not scope.unrestricted


# ---- B6: 登录防爆破（账户锁定 / IP 限流） ----------------------------------


async def test_repeated_failures_lock_account(auth_session: AsyncSession) -> None:
    """连续失败达阈值(默认 5)后,即便密码正确也被锁定拒绝(429 + account_locked)。"""
    await seed_user(auth_session, password="secret123")
    service = AuthService(auth_session)
    for _ in range(5):
        with pytest.raises(AppError):
            await service.authenticate_password("alice", "wrong")
    # 锁定期内即便密码正确也拒绝
    with pytest.raises(AppError) as exc:
        await service.authenticate_password("alice", "secret123")
    assert exc.value.status_code == 429
    assert exc.value.code == ErrorCode.auth_account_locked


async def test_below_threshold_does_not_lock(auth_session: AsyncSession) -> None:
    """阈值以下的失败不锁定:补对密码仍可登录成功。"""
    await seed_user(auth_session, password="secret123")
    service = AuthService(auth_session)
    for _ in range(4):  # 4 < 5
        with pytest.raises(AppError):
            await service.authenticate_password("alice", "wrong")
    token, ttl, _ = await service.authenticate_password("alice", "secret123")
    assert ttl > 0
    assert token


async def test_successful_login_resets_failure_count(
    auth_session: AsyncSession,
) -> None:
    """登录成功清零失败计数:此后再失败需重新累计,不会立即触发旧锁。"""
    await seed_user(auth_session, password="secret123")
    service = AuthService(auth_session)
    for _ in range(4):
        with pytest.raises(AppError):
            await service.authenticate_password("alice", "wrong")
    await service.authenticate_password("alice", "secret123")  # reset
    # 重置后再失败一次,远未达阈值,仍可成功登录
    with pytest.raises(AppError):
        await service.authenticate_password("alice", "wrong")
    token, _, _ = await service.authenticate_password("alice", "secret123")
    assert token


async def test_lockout_applies_to_unknown_username(
    auth_session: AsyncSession,
) -> None:
    """锁定对不存在的用户名同样生效(避免 account_locked 成为用户枚举 oracle)。"""
    service = AuthService(auth_session)
    for _ in range(5):
        with pytest.raises(AppError):
            await service.authenticate_password("ghost", "whatever")
    with pytest.raises(AppError) as exc:
        await service.authenticate_password("ghost", "whatever")
    assert exc.value.status_code == 429
    assert exc.value.code == ErrorCode.auth_account_locked


async def test_lockout_check_precedes_credential_check(
    auth_session: AsyncSession,
) -> None:
    """锁定闸在 DB 查询/argon2 验签之前触发(省 CPU + 防 DoS):锁定后即便用户存在也 429。"""
    await seed_user(auth_session, password="secret123")
    service = AuthService(auth_session)
    for _ in range(5):
        with pytest.raises(AppError):
            await service.authenticate_password("alice", "wrong")
    with pytest.raises(AppError) as exc:
        await service.authenticate_password("alice", "wrong")
    assert exc.value.code == ErrorCode.auth_account_locked


# ---- B7: 改密强度策略 + 强制全端下线 ---------------------------------------


async def test_change_password_weak_rejected_with_code(
    auth_session: AsyncSession,
) -> None:
    """弱口令(无数字)被拒,发 auth_password_too_weak(400)并在 params 带违规标签。"""
    user = await seed_user(auth_session, password="old-secret1")
    service = AuthService(auth_session)
    current_user = AuthenticatedUser(
        user_id=user.id,
        username="alice",
        department_id=None,
        permissions=frozenset(),
    )
    with pytest.raises(AppError) as exc:
        await service.change_current_password(
            current_user, old_password="old-secret1", new_password="onlyletters"
        )
    assert exc.value.status_code == 400
    assert exc.value.code == ErrorCode.auth_password_too_weak
    assert "no_digit" in exc.value.params["violations"]


async def test_change_password_weak_does_not_mutate_password(
    auth_session: AsyncSession,
) -> None:
    """弱口令被拒时,旧密码必须保持不变(强度闸在哈希/落库之前)。"""
    user = await seed_user(auth_session, password="old-secret1")
    service = AuthService(auth_session)
    current_user = AuthenticatedUser(
        user_id=user.id,
        username="alice",
        department_id=None,
        permissions=frozenset(),
    )
    with pytest.raises(AppError):
        await service.change_current_password(
            current_user, old_password="old-secret1", new_password="short1"
        )
    assert user.password is not None
    assert await verify_password_async(user.password, "old-secret1")


async def test_change_password_revokes_all_sessions(
    auth_session: AsyncSession,
) -> None:
    """改密成功后强制全端下线:该用户在册的全部 access 会话被吊销(B7)。"""
    await seed_user(auth_session, password="old-secret1")
    service = AuthService(auth_session)
    # 先登录建一个真实会话(走 SessionStore,落 fake_redis)
    token, _, _ = await service.authenticate_password("alice", "old-secret1")
    current = await service.resolve_access_token(token)  # 会话在册,可解析
    await service.change_current_password(
        current, old_password="old-secret1", new_password="new-secret2"
    )
    # 改密后旧 access token 的会话已被吊销 → 解析报 token_revoked
    with pytest.raises(AppError) as exc:
        await service.resolve_access_token(token)
    assert exc.value.code == ErrorCode.auth_token_revoked
