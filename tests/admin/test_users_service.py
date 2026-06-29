"""Direct unit tests for ``UserService`` (service layer, no HTTP).

Drives the service with a plain ``await`` against an in-memory SQLite session.
Route tests (``test_users.py``) prove behaviour end-to-end; these give honest,
measurable coverage of the service body (the ``ASGITransport`` route path
corrupts coverage.py's C tracer on CPython 3.11) and pin branch invariants:
the uniqueness guards, the active-role assignment gate, and the two-axis
privilege-escalation / dominance guards on role assignment.
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


async def test_create_user_hashes_password(admin_session: AsyncSession) -> None:
    svc = UserService(admin_session)
    user, role_ids = await svc.create_user(
        UserCreate(username="carol", employee_no="E-100", password="pw123456"),
        _superuser(),
    )
    assert user.username == "carol"
    assert user.password is not None
    assert user.password != "pw123456"  # argon2 hash, not plaintext
    assert role_ids == []


async def test_create_user_without_password(admin_session: AsyncSession) -> None:
    # SSO-only user: password is None → hash branch skipped.
    svc = UserService(admin_session)
    user, _ = await svc.create_user(
        UserCreate(username="sso", employee_no="E-sso"), _superuser()
    )
    assert user.password is None


async def test_create_user_weak_password_rejected(
    admin_session: AsyncSession,
) -> None:
    # Admin-set passwords obey the same strength floor as reset/self-service:
    # "pw12345" is 7 chars → too_short, refused before any row is written.
    svc = UserService(admin_session)
    with pytest.raises(AppError) as exc:
        await svc.create_user(
            UserCreate(username="weak", employee_no="E-weak", password="pw12345"),
            _superuser(),
        )
    assert exc.value.code is ErrorCode.auth_password_too_weak
    assert exc.value.status_code == 400
    # Nothing persisted.
    assert (
        await admin_session.scalar(select(User).where(User.username == "weak"))
    ) is None


async def test_update_user_weak_password_rejected(
    admin_session: AsyncSession,
) -> None:
    svc = UserService(admin_session)
    user, _ = await svc.create_user(
        UserCreate(username="updtgt", employee_no="E-upd", password="old12345"),
        _superuser(),
    )
    old_hash = user.password
    with pytest.raises(AppError) as exc:
        await svc.update_user(user.id, UserUpdate(password="weak"), _superuser())
    assert exc.value.code is ErrorCode.auth_password_too_weak
    # Password unchanged on a rejected update.
    assert user.password == old_hash


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
    role = Role(name="member", code="member", status="active")
    admin_session.add(role)
    await admin_session.commit()
    svc = UserService(admin_session)
    _, role_ids = await svc.create_user(
        UserCreate(username="erin", employee_no="E-3", role_ids=[role.id]),
        _superuser(),
    )
    assert role_ids == [role.id]


async def test_create_user_disabled_role_rejected(
    admin_session: AsyncSession,
) -> None:
    """M3-6: assigning a *disabled* role is rejected (400). A disabled role
    confers nothing now but would silently re-activate its grants the moment an
    admin re-enables it — only an active role is a valid assignment target."""
    role = Role(name="dis", code="dis-role", status="disabled")
    admin_session.add(role)
    await admin_session.commit()
    svc = UserService(admin_session)
    with pytest.raises(AppError) as exc:
        await svc.create_user(
            UserCreate(username="z", employee_no="E-z", role_ids=[role.id]),
            _superuser(),
        )
    assert exc.value.code is ErrorCode.request_invalid
    assert exc.value.status_code == 400
    # Nothing persisted.
    assert (
        await admin_session.scalar(select(User).where(User.username == "z"))
    ) is None


async def test_update_user_assign_disabled_role_rejected(
    admin_session: AsyncSession,
) -> None:
    """M3-6: the same active-role gate applies on update_user role replacement."""
    active = Role(name="act", code="act-role", status="active")
    disabled = Role(name="dis2", code="dis-role2", status="disabled")
    admin_session.add_all([active, disabled])
    await admin_session.commit()
    svc = UserService(admin_session)
    user, _ = await svc.create_user(
        UserCreate(username="upd-dis", employee_no="E-ud", role_ids=[active.id]),
        _superuser(),
    )
    with pytest.raises(AppError) as exc:
        await svc.update_user(user.id, UserUpdate(role_ids=[disabled.id]), _superuser())
    assert exc.value.status_code == 400


async def test_get_user_not_found_raises(admin_session: AsyncSession) -> None:
    svc = UserService(admin_session)
    with pytest.raises(AppError) as exc:
        await svc.get_user(999999, _superuser())
    assert exc.value.status_code == 404


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
    r1 = Role(name="r1", code="ru1", status="active")
    r2 = Role(name="r2", code="ru2", status="active")
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
    role = Role(name="d", code="del-role", status="active")
    admin_session.add(role)
    await admin_session.commit()
    svc = UserService(admin_session)
    user, _ = await svc.create_user(
        UserCreate(username="harry", employee_no="E-6", role_ids=[role.id]),
        _superuser(),
    )
    await svc.delete_user(user.id, _superuser())

    # Soft-deleted → no longer fetchable.
    with pytest.raises(AppError):
        await svc.get_user(user.id, _superuser())
    # Role links cleared.
    links = await admin_session.scalars(
        select(UserRole.id).where(UserRole.user_id == user.id)
    )
    assert links.all() == []


# ---- privilege-escalation guard (role assignment) --------------------------


async def test_non_superuser_cannot_assign_unheld_perm_role(
    admin_session: AsyncSession,
) -> None:
    """A scoped actor cannot assign a role conferring a permission it lacks."""
    admin_session.add(Department(id=820, name="d820", parent_id=None))
    powerful = Role(name="power", code="power", status="active")
    menu = Menu(name="m.super", menu_type="button", perms="*:*:*", status="active")
    admin_session.add_all([powerful, menu])
    await admin_session.flush()
    admin_session.add(RoleMenu(role_id=powerful.id, menu_id=menu.id))
    await admin_session.commit()
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


async def test_non_superuser_cannot_assign_superadmin_role(
    admin_session: AsyncSession,
) -> None:
    """A scoped actor cannot assign a ``superadmin``-coded role — even one with
    NO menus (zero conferred perms), which the perms-subset check waves through.

    This is the escalation the perms-only guard is blind to: ``is_superuser`` is
    code-based, so a no-menu ``superadmin`` role confers an empty perm set that
    trivially passes ``issubset``, yet assigning it hands out full super-admin.
    """
    admin_session.add(Department(id=830, name="d830", parent_id=None))
    # A superadmin-coded role with NO menus → confers zero perms.
    su_role = Role(name="su", code=SUPERADMIN_ROLE_CODE, status="active")
    admin_session.add(su_role)
    await admin_session.commit()
    actor = _scoped_actor(60, dept=830, perms={"system:user:add"})
    svc = UserService(admin_session)
    with pytest.raises(AppError) as exc:
        await svc.create_user(
            UserCreate(
                username="escalate-su",
                employee_no="E-esu",
                department_id=830,
                role_ids=[su_role.id],
            ),
            actor,
        )
    assert exc.value.status_code == 403


async def test_superuser_may_assign_superadmin_role(
    admin_session: AsyncSession,
) -> None:
    """The superadmin-code guard must not block an actual super-admin."""
    su_role = Role(name="su2", code=SUPERADMIN_ROLE_CODE, status="active")
    admin_session.add(su_role)
    await admin_session.commit()
    svc = UserService(admin_session)
    _, role_ids = await svc.create_user(
        UserCreate(username="newsu", employee_no="E-nsu", role_ids=[su_role.id]),
        _superuser(),
    )
    assert role_ids == [su_role.id]


# ---- session revocation on credential / status change ----------------------


async def test_update_user_disable_revokes_sessions(
    admin_session: AsyncSession,
) -> None:
    """Disabling a user kills every live session (no resurrection on re-enable)."""
    from src.auth.service import AuthService

    svc = UserService(admin_session)
    target, _ = await svc.create_user(
        UserCreate(username="disableme", employee_no="E-dis", password="pw123456"),
        _superuser(),
    )
    auth = AuthService(admin_session)
    token, _, _ = await auth.authenticate_password("disableme", "pw123456")
    current = await auth.resolve_access_token(token)
    assert current.user_id == target.id

    await svc.update_user(
        target.id, UserUpdate(status=UserStatus.disabled), _superuser()
    )
    # Revocation is synchronous before commit now — it already ran inside
    # update_user (flush + Redis revoke), so no commit_session() is needed to
    # trigger it.

    with pytest.raises(AppError) as exc:
        await auth.resolve_access_token(token)
    assert exc.value.code is ErrorCode.auth_token_revoked


async def test_delete_user_revokes_sessions(
    admin_session: AsyncSession,
) -> None:
    """Soft-deleting a user kills every live session too (same resurrection risk)."""
    from src.auth.service import AuthService

    svc = UserService(admin_session)
    target, _ = await svc.create_user(
        UserCreate(username="deleteme", employee_no="E-del2", password="pw123456"),
        _superuser(),
    )
    auth = AuthService(admin_session)
    token, _, _ = await auth.authenticate_password("deleteme", "pw123456")
    assert (await auth.resolve_access_token(token)).user_id == target.id

    await svc.delete_user(target.id, _superuser())
    # Synchronous revocation already ran inside delete_user.

    with pytest.raises(AppError) as exc:
        await auth.resolve_access_token(token)
    assert exc.value.code is ErrorCode.auth_token_revoked


async def test_list_users_bulk_roles(admin_session: AsyncSession) -> None:
    role = Role(name="m", code="lrole", status="active")
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

    # Revocation is synchronous before commit now (flush + Redis revoke run
    # inside reset_password), so the pre-reset session is already gone — no
    # commit_session() needed to trigger it.

    # The pre-reset access token's session is revoked → resolve fails.
    with pytest.raises(AppError) as exc:
        await auth.resolve_access_token(token)
    assert exc.value.code is ErrorCode.auth_token_revoked


# ---- dominance guards (VISIBLE ≠ MANAGEABLE) -------------------------------


async def _seed_target_with_role(
    session: AsyncSession,
    *,
    dept_id: int,
    role: Role,
    username: str,
    employee_no: str,
) -> User:
    """A user in ``dept_id`` holding ``role``."""
    session.add(role)
    await session.flush()
    user = User(
        username=username,
        employee_no=employee_no,
        department_id=dept_id,
        password="x",
    )
    session.add(user)
    await session.flush()
    session.add(UserRole(user_id=user.id, role_id=role.id))
    await session.commit()
    return user


async def test_scoped_actor_cannot_reset_password_of_superadmin_target(
    admin_session: AsyncSession,
) -> None:
    """Dominance guard: a non-super actor with ``system:user:resetPwd`` cannot
    reset a super-admin's password — that would be a full account takeover. The
    target's ``superadmin`` role makes it un-dominatable along the marker axis."""
    su_role = Role(name="su-tgt", code=SUPERADMIN_ROLE_CODE, status="active")
    target = await _seed_target_with_role(
        admin_session, dept_id=700, role=su_role, username="su-victim", employee_no="E-suv"
    )
    actor = _scoped_actor(70, dept=700, perms={"system:user:resetPwd"})
    svc = UserService(admin_session)

    with pytest.raises(AppError) as exc:
        await svc.reset_password(target.id, "new12345", actor)
    assert exc.value.status_code == 403


async def test_scoped_actor_cannot_delete_higher_privileged_target(
    admin_session: AsyncSession,
) -> None:
    """Dominance guard on delete: a target holding a role that confers a
    permission the actor lacks is un-deletable (the actor could never have
    assigned that role itself)."""
    menu = Menu(
        name="m.del", menu_type="button", perms="system:user:delete", status="active"
    )
    power_role = Role(name="power", code="power-del", status="active")
    admin_session.add_all([menu, power_role])
    await admin_session.flush()
    admin_session.add(RoleMenu(role_id=power_role.id, menu_id=menu.id))
    target = await _seed_target_with_role(
        admin_session, dept_id=710, role=power_role, username="powtgt", employee_no="E-pow"
    )
    actor = _scoped_actor(71, dept=710, perms={"system:user:remove"})
    svc = UserService(admin_session)

    with pytest.raises(AppError) as exc:
        await svc.delete_user(target.id, actor)
    assert exc.value.status_code == 403
    # Still present (not soft-deleted) after the refused delete.
    assert (await admin_session.get(User, target.id)).is_deleted is False


async def test_scoped_actor_can_manage_role_less_target(
    admin_session: AsyncSession,
) -> None:
    """Dominance allows managing a target whose roles the actor COULD assign —
    a role-less (zero-privilege) target is freely manageable."""
    target = User(
        username="peer", employee_no="E-peer", department_id=720, password="x"
    )
    admin_session.add(target)
    await admin_session.flush()
    target_id = target.id
    await admin_session.commit()
    actor = _scoped_actor(72, dept=720, perms={"system:user:resetPwd"})
    svc = UserService(admin_session)

    await svc.reset_password(target_id, "new12345", actor)
    refreshed = await admin_session.get(User, target_id)
    assert await verify_password_async(refreshed.password, "new12345")


async def test_superuser_bypasses_dominance(admin_session: AsyncSession) -> None:
    """A super-admin may reset even another super-admin's password (regression:
    the dominance guard must not block the unrestricted actor)."""
    su_role = Role(name="su-b", code=SUPERADMIN_ROLE_CODE, status="active")
    target = await _seed_target_with_role(
        admin_session, dept_id=0, role=su_role, username="su-b-tgt", employee_no="E-sub"
    )
    svc = UserService(admin_session)
    await svc.reset_password(target.id, "new12345", _superuser())
    assert await verify_password_async(target.password, "new12345")


async def test_batch_delete_skips_undominated_targets(
    admin_session: AsyncSession,
) -> None:
    """Batch delete skips (does not sweep) targets the actor cannot dominate.

    A non-super actor batch-deleting [role-less peer, super-admin] removes only
    the peer; the super-admin is reported skipped, not deleted."""
    su_role = Role(name="su-bd", code=SUPERADMIN_ROLE_CODE, status="active")
    admin_session.add(su_role)
    await admin_session.flush()
    peer = User(username="peer-bd", employee_no="E-pbd", department_id=730, password="x")
    su_tgt = User(username="su-bd-t", employee_no="E-sbd", department_id=730, password="x")
    admin_session.add_all([peer, su_tgt])
    await admin_session.flush()
    admin_session.add(UserRole(user_id=su_tgt.id, role_id=su_role.id))
    peer_id, su_id = peer.id, su_tgt.id
    await admin_session.commit()
    actor = _scoped_actor(73, dept=730, perms={"system:user:remove"})
    svc = UserService(admin_session)

    result = await svc.batch_delete_users([peer_id, su_id], actor)
    assert result.affected == 1
    assert str(su_id) in result.skipped_ids
    assert (await admin_session.get(User, peer_id)).is_deleted is True
    assert (await admin_session.get(User, su_id)).is_deleted is False
