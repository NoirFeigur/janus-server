"""Tests for ``AuthService`` (login / sk-key / RBAC / data-scope)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from src.admin.departments.schemas import DepartmentUpdate
from src.admin.departments.service import DepartmentService
from src.auth.service import (
    AuthenticatedUser,
    AuthService,
    invalidate_department_tree,
)
from src.core.security import decode_access_token, generate_api_key, verify_password_async
from src.db.models.credential import ApiKey
from src.db.models.identity import Department, Role, RoleDept, UserRole
from src.enums import DataScope
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
    user.real_name = "Alice"
    user.email = "alice@example.com"
    user.mobile = "13800000000"
    user.preferred_locale = "en-US"
    await auth_session.flush()
    await grant_permission(auth_session, user=user, perm="system:user:list")
    service = AuthService(auth_session)
    token, _ = await service.authenticate_password("alice", "secret123")
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
        current_user, old_password="old-secret", new_password="new-secret"
    )
    assert user.password is not None
    assert await verify_password_async(user.password, "new-secret")
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
            current_user, old_password="bad", new_password="new-secret"
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
            current_user, old_password="old", new_password="new-secret"
        )
    assert exc.value.status_code == 401


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
    current_user = await service.resolve_access_token(token)
    assert current_user.is_superuser
    assert current_user.has_permission("anything:at:all")


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
    token, _ = await service.authenticate_password("alice", "secret123")
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
    token, _ = await service.authenticate_password("alice", "secret123")
    current_user = await service.resolve_access_token(token)
    scope = await service.resolve_data_scope(current_user)
    assert scope.unrestricted


async def test_data_scope_dept_only_includes_own_dept(
    auth_session: AsyncSession,
) -> None:
    service, user_id = await _principal_with_role(
        auth_session, data_scope="dept", department_id=42
    )
    token, _ = await service.authenticate_password("alice", "secret123")
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
    token, _ = await service.authenticate_password("alice", "secret123")
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
    token, _ = await service.authenticate_password("alice", "secret123")
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
    token, _ = await service.authenticate_password("alice", "secret123")
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
            permissions=frozenset({"*:*:*"}),
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
    token, _ = await service.authenticate_password("alice", "secret123")
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
    token, _ = await service.authenticate_password("alice", "secret123")
    current_user = await service.resolve_access_token(token)
    scope = await service.resolve_data_scope(current_user)
    assert scope.department_ids == frozenset({1, 2})
    assert scope.include_self
    assert not scope.unrestricted
