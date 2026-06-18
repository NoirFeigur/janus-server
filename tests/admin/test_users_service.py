"""Direct unit tests for ``UserService`` (service layer, no HTTP).

Drives the service with a plain ``await`` against an in-memory SQLite session.
Route tests (``test_users.py``) prove behaviour end-to-end; these give honest,
measurable coverage of the service body (the ``ASGITransport`` route path
corrupts coverage.py's C tracer on CPython 3.11) and pin branch invariants:
the uniqueness guards, data-scope visibility on mutation, the
department-in-scope rule, and the privilege-escalation guard on role assignment.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.admin.users.schemas import UserCreate, UserUpdate
from src.admin.users.service import UserService
from src.auth.constants import SUPERADMIN_ROLE_CODE
from src.auth.service import AuthenticatedUser
from src.core.query import ListQuery
from src.core.security import verify_password_async
from src.db.models.identity import Department, Menu, Role, RoleMenu, User, UserRole
from src.db.session import commit_session
from src.enums import ErrorCode, UserStatus
from src.exceptions import AppError

pytestmark = pytest.mark.asyncio

ADMIN_ID = 1000


def _superuser() -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id=ADMIN_ID,
        username="admin",
        department_id=None,
        permissions=frozenset({"*:*:*"}),
        role_codes=frozenset({SUPERADMIN_ROLE_CODE}),
    )


def _scoped_actor(user_id: int, *, dept: int | None, perms: set[str]) -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id=user_id,
        username="scoped",
        department_id=dept,
        permissions=frozenset(perms),
    )


async def _seed_dept_scoped_role(
    session: AsyncSession, actor_id: int, *, code: str
) -> None:
    """Give ``actor_id`` a ``dept``-scope role so resolve_data_scope restricts them."""
    role = Role(name=code, code=code, data_scope="dept", status="active")
    session.add(role)
    await session.flush()
    session.add(UserRole(user_id=actor_id, role_id=role.id))
    await session.commit()


async def test_create_user_hashes_password(admin_session: AsyncSession) -> None:
    svc = UserService(admin_session)
    user, role_ids = await svc.create_user(
        UserCreate(username="carol", employee_no="E-100", password="pw12345"),
        _superuser(),
    )
    assert user.username == "carol"
    assert user.password is not None
    assert user.password != "pw12345"  # argon2 hash, not plaintext
    assert role_ids == []


async def test_create_user_without_password(admin_session: AsyncSession) -> None:
    # SSO-only user: password is None → hash branch skipped.
    svc = UserService(admin_session)
    user, _ = await svc.create_user(
        UserCreate(username="sso", employee_no="E-sso"), _superuser()
    )
    assert user.password is None


async def test_create_user_duplicate_username_rejected(
    admin_session: AsyncSession,
) -> None:
    svc = UserService(admin_session)
    await svc.create_user(
        UserCreate(username="dave", employee_no="E-1"), _superuser()
    )
    with pytest.raises(AppError) as exc:
        await svc.create_user(
            UserCreate(username="dave", employee_no="E-2"), _superuser()
        )
    assert exc.value.status_code == 400


async def test_create_user_duplicate_employee_no_rejected(
    admin_session: AsyncSession,
) -> None:
    svc = UserService(admin_session)
    await svc.create_user(
        UserCreate(username="u1", employee_no="E-DUP"), _superuser()
    )
    with pytest.raises(AppError) as exc:
        await svc.create_user(
            UserCreate(username="u2", employee_no="E-DUP"), _superuser()
        )
    assert exc.value.status_code == 400


async def test_create_user_unknown_department_rejected(
    admin_session: AsyncSession,
) -> None:
    svc = UserService(admin_session)
    with pytest.raises(AppError) as exc:
        await svc.create_user(
            UserCreate(username="x", employee_no="E-x", department_id=424242),
            _superuser(),
        )
    assert exc.value.status_code == 400


async def test_create_user_unknown_role_rejected(admin_session: AsyncSession) -> None:
    svc = UserService(admin_session)
    with pytest.raises(AppError) as exc:
        await svc.create_user(
            UserCreate(username="y", employee_no="E-y", role_ids=[77777]),
            _superuser(),
        )
    assert exc.value.status_code == 400


async def test_create_user_with_roles(admin_session: AsyncSession) -> None:
    role = Role(name="member", code="member", data_scope="self", status="active")
    admin_session.add(role)
    await admin_session.commit()
    svc = UserService(admin_session)
    _, role_ids = await svc.create_user(
        UserCreate(username="erin", employee_no="E-3", role_ids=[role.id]),
        _superuser(),
    )
    assert role_ids == [role.id]


async def test_get_user_not_visible_raises(admin_session: AsyncSession) -> None:
    svc = UserService(admin_session)
    with pytest.raises(AppError) as exc:
        await svc.get_user(999999, _superuser())
    assert exc.value.status_code == 403  # opaque: no exists-but-hidden oracle


async def test_update_user_status_and_password(admin_session: AsyncSession) -> None:
    svc = UserService(admin_session)
    user, _ = await svc.create_user(
        UserCreate(username="gina", employee_no="E-5"), _superuser()
    )
    updated, _ = await svc.update_user(
        user.id,
        UserUpdate(status=UserStatus.disabled, password="newpw123"),
        _superuser(),
    )
    assert updated.status == UserStatus.disabled.value
    assert updated.password is not None and updated.password != "newpw123"


async def test_update_user_replaces_roles(admin_session: AsyncSession) -> None:
    r1 = Role(name="r1", code="ru1", data_scope="self", status="active")
    r2 = Role(name="r2", code="ru2", data_scope="self", status="active")
    admin_session.add_all([r1, r2])
    await admin_session.commit()
    svc = UserService(admin_session)
    user, _ = await svc.create_user(
        UserCreate(username="roled", employee_no="E-rr", role_ids=[r1.id]),
        _superuser(),
    )
    _, role_ids = await svc.update_user(
        user.id, UserUpdate(role_ids=[r2.id]), _superuser()
    )
    assert role_ids == [r2.id]


async def test_update_user_change_department(admin_session: AsyncSession) -> None:
    admin_session.add(Department(id=900, name="d900", parent_id=None))
    await admin_session.commit()
    svc = UserService(admin_session)
    user, _ = await svc.create_user(
        UserCreate(username="movable", employee_no="E-mv"), _superuser()
    )
    updated, _ = await svc.update_user(
        user.id, UserUpdate(department_id=900), _superuser()
    )
    assert updated.department_id == 900


async def test_delete_user_soft_deletes_and_clears_roles(
    admin_session: AsyncSession,
) -> None:
    role = Role(name="d", code="del-role", data_scope="self", status="active")
    admin_session.add(role)
    await admin_session.commit()
    svc = UserService(admin_session)
    user, _ = await svc.create_user(
        UserCreate(username="harry", employee_no="E-6", role_ids=[role.id]),
        _superuser(),
    )
    await svc.delete_user(user.id, _superuser())

    # Soft-deleted → no longer visible.
    with pytest.raises(AppError):
        await svc.get_user(user.id, _superuser())
    # Role links cleared.
    links = await admin_session.scalars(
        select(UserRole.id).where(UserRole.user_id == user.id)
    )
    assert links.all() == []


async def test_scoped_actor_cannot_see_out_of_scope_user(
    admin_session: AsyncSession,
) -> None:
    admin_session.add_all(
        [
            Department(id=500, name="d500", parent_id=None),
            User(id=51, username="in500", employee_no="E-51", department_id=500),
        ]
    )
    await admin_session.commit()
    # Actor scoped to dept 999 (not 500) → target invisible → opaque 403.
    await _seed_dept_scoped_role(admin_session, 42, code="scope999")
    actor = _scoped_actor(42, dept=999, perms={"system:user:edit"})
    svc = UserService(admin_session)
    with pytest.raises(AppError) as exc:
        await svc.update_user(51, UserUpdate(status=UserStatus.disabled), actor)
    assert exc.value.status_code == 403


async def test_scoped_actor_cannot_create_out_of_scope_user(
    admin_session: AsyncSession,
) -> None:
    admin_session.add_all(
        [
            Department(id=800, name="d800", parent_id=None),
            Department(id=801, name="d801", parent_id=None),
        ]
    )
    await admin_session.commit()
    await _seed_dept_scoped_role(admin_session, 43, code="scope800")
    actor = _scoped_actor(43, dept=800, perms={"system:user:add"})
    svc = UserService(admin_session)

    # Out-of-scope department → forbidden.
    with pytest.raises(AppError) as exc:
        await svc.create_user(
            UserCreate(username="x1", employee_no="E-x1", department_id=801), actor
        )
    assert exc.value.status_code == 403

    # No department under a restricted scope → forbidden.
    with pytest.raises(AppError):
        await svc.create_user(
            UserCreate(username="x2", employee_no="E-x2"), actor
        )

    # In-scope department → allowed.
    user, _ = await svc.create_user(
        UserCreate(username="x3", employee_no="E-x3", department_id=800), actor
    )
    assert user.department_id == 800


async def test_non_superuser_cannot_assign_unheld_perm_role(
    admin_session: AsyncSession,
) -> None:
    admin_session.add(Department(id=820, name="d820", parent_id=None))
    powerful = Role(name="power", code="power", data_scope="all", status="active")
    menu = Menu(name="m.super", menu_type="button", perms="*:*:*", status="active")
    admin_session.add_all([powerful, menu])
    await admin_session.flush()
    admin_session.add(RoleMenu(role_id=powerful.id, menu_id=menu.id))
    await _seed_dept_scoped_role(admin_session, 44, code="weak")
    # Actor is scoped to dept 820 and creates an in-scope user, so the flow
    # reaches the role-assignment escalation guard (not the dept-scope guard).
    actor = _scoped_actor(44, dept=820, perms={"system:user:add"})
    svc = UserService(admin_session)
    with pytest.raises(AppError) as exc:
        await svc.create_user(
            UserCreate(
                username="escalate",
                employee_no="E-esc",
                department_id=820,
                role_ids=[powerful.id],
            ),
            actor,
        )
    assert exc.value.status_code == 403


async def test_list_users_bulk_roles(admin_session: AsyncSession) -> None:
    role = Role(name="m", code="lrole", data_scope="self", status="active")
    admin_session.add(role)
    await admin_session.commit()
    svc = UserService(admin_session)
    await svc.create_user(
        UserCreate(username="a", employee_no="E-a", role_ids=[role.id]), _superuser()
    )
    await svc.create_user(UserCreate(username="b", employee_no="E-b"), _superuser())
    listing = await svc.list_users(_superuser(), query=ListQuery(limit=50, offset=0))
    by_name = {u.username: roles for u, roles in listing.items}
    assert listing.total == 2
    assert by_name["a"] == [role.id]
    assert by_name["b"] == []  # user with no roles defaults to empty list


async def test_reset_password_sets_new_hash(admin_session: AsyncSession) -> None:
    svc = UserService(admin_session)
    target, _ = await svc.create_user(
        UserCreate(username="reset-me", employee_no="E-rst", password="old12345"),
        _superuser(),
    )

    await svc.reset_password(target.id, "new12345", _superuser())

    assert target.password is not None
    assert await verify_password_async(target.password, "new12345")
    assert not await verify_password_async(target.password, "old12345")
    assert target.updated_by == ADMIN_ID


async def test_reset_password_weak_rejected_with_code(
    admin_session: AsyncSession,
) -> None:
    svc = UserService(admin_session)
    target, _ = await svc.create_user(
        UserCreate(username="weak-rst", employee_no="E-wk", password="old12345"),
        _superuser(),
    )
    old_hash = target.password

    with pytest.raises(AppError) as exc:
        await svc.reset_password(target.id, "short", _superuser())
    assert exc.value.code is ErrorCode.auth_password_too_weak
    assert exc.value.status_code == 400
    # Password unchanged on a rejected reset.
    assert target.password == old_hash


async def test_reset_password_revokes_target_sessions(
    admin_session: AsyncSession,
) -> None:
    """Admin reset forces the target off every device (B7)."""
    from src.auth.service import AuthService

    svc = UserService(admin_session)
    target, _ = await svc.create_user(
        UserCreate(username="kickme", employee_no="E-kick", password="old12345"),
        _superuser(),
    )
    # The target logs in → a real session lands in fake_redis.
    auth = AuthService(admin_session)
    token, _, _ = await auth.authenticate_password("kickme", "old12345")
    current = await auth.resolve_access_token(token)
    assert current.user_id == target.id

    await svc.reset_password(target.id, "new12345", _superuser())

    # Session revocation is an after-commit hook now (it fires only once the
    # password write lands), so emulate the request edge: commit the unit of
    # work, which runs the queued revocation hook.
    await commit_session(admin_session)

    # The pre-reset access token's session is revoked → resolve fails.
    with pytest.raises(AppError) as exc:
        await auth.resolve_access_token(token)
    assert exc.value.code is ErrorCode.auth_token_revoked


async def test_reset_password_not_visible_raises(
    admin_session: AsyncSession,
) -> None:
    """A scoped actor who cannot see the target gets an opaque 403."""
    admin_session.add(Department(id=900, name="d900", parent_id=None))
    await admin_session.flush()
    svc = UserService(admin_session)
    # Target in a department the scoped actor cannot see.
    target, _ = await svc.create_user(
        UserCreate(
            username="hidden", employee_no="E-hid", department_id=900, password="pw123456"
        ),
        _superuser(),
    )
    await _seed_dept_scoped_role(admin_session, 55, code="weakr")
    actor = _scoped_actor(55, dept=None, perms={"system:user:resetPwd"})

    with pytest.raises(AppError) as exc:
        await svc.reset_password(target.id, "new12345", actor)
    assert exc.value.status_code == 403
