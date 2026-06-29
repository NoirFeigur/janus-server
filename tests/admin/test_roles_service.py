"""Direct unit tests for ``RoleService`` (service layer, no HTTP).

Drives the service with a plain ``await`` against an in-memory SQLite session.
Route tests (``test_roles.py``) already prove the behaviour end-to-end; these
exist to (a) give honest, measurable coverage of the service body — the
``ASGITransport`` route path corrupts coverage.py's C tracer on CPython 3.11 —
and (b) pin branch-level invariants (the unique-code guard, the full delete
cascade) at the unit level.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.admin.roles.schemas import RoleCreate, RoleUpdate
from src.admin.roles.service import RoleService
from src.auth.constants import SUPERADMIN_ROLE_CODE
from src.auth.service import AuthenticatedUser
from src.db.models.identity import Menu, Role, RoleMenu, UserRole
from src.exceptions import AppError

pytestmark = pytest.mark.asyncio

ACTOR_ID = 1000


def _actor(
    *,
    user_id: int = ACTOR_ID,
    department_id: int | None = 10,
    permissions: set[str] | None = None,
) -> AuthenticatedUser:
    perms = permissions or {"*:*:*"}
    role_codes = (
        frozenset({SUPERADMIN_ROLE_CODE}) if "*:*:*" in perms else frozenset()
    )
    return AuthenticatedUser(
        user_id=user_id,
        username="admin",
        department_id=department_id,
        permissions=frozenset(perms),
        role_codes=role_codes,
    )


async def _seed_menu(session: AsyncSession, perm: str) -> int:
    menu = Menu(name=f"m.{perm}", menu_type="button", perms=perm, status="active")
    session.add(menu)
    await session.commit()
    return menu.id


async def test_create_role_with_menus(admin_session: AsyncSession) -> None:
    menu_id = await _seed_menu(admin_session, "system:user:list")
    svc = RoleService(admin_session)
    role, menu_ids = await svc.create_role(
        RoleCreate(name="Viewer", code="viewer", menu_ids=[menu_id]), actor=_actor()
    )
    assert role.code == "viewer"
    assert role.created_by == ACTOR_ID
    assert role.create_dept == 10
    assert menu_ids == [menu_id]


async def test_create_role_duplicate_code_rejected(admin_session: AsyncSession) -> None:
    svc = RoleService(admin_session)
    await svc.create_role(RoleCreate(name="R", code="dup"), actor=_actor())
    with pytest.raises(AppError) as exc:
        await svc.create_role(RoleCreate(name="R2", code="dup"), actor=_actor())
    assert exc.value.status_code == 400


async def test_create_role_unknown_menu_rejected(admin_session: AsyncSession) -> None:
    svc = RoleService(admin_session)
    with pytest.raises(AppError) as exc:
        await svc.create_role(
            RoleCreate(name="Bad", code="bad", menu_ids=[88888]), actor=_actor()
        )
    assert exc.value.status_code == 400


async def test_get_role_not_found_raises(admin_session: AsyncSession) -> None:
    svc = RoleService(admin_session)
    with pytest.raises(AppError) as exc:
        await svc.get_role(123456, _actor())
    assert exc.value.status_code == 404


async def test_update_role_replaces_menus(admin_session: AsyncSession) -> None:
    m1 = await _seed_menu(admin_session, "system:user:list")
    m2 = await _seed_menu(admin_session, "system:user:add")
    svc = RoleService(admin_session)
    role, _ = await svc.create_role(
        RoleCreate(name="R", code="rr", menu_ids=[m1]), actor=_actor()
    )
    _, menu_ids = await svc.update_role(
        role.id, RoleUpdate(menu_ids=[m2]), actor=_actor()
    )
    assert menu_ids == [m2]


async def test_update_role_unknown_menu_rejected(admin_session: AsyncSession) -> None:
    svc = RoleService(admin_session)
    role, _ = await svc.create_role(
        RoleCreate(name="R", code="rmenu"), actor=_actor()
    )
    with pytest.raises(AppError) as exc:
        await svc.update_role(role.id, RoleUpdate(menu_ids=[55555]), actor=_actor())
    assert exc.value.status_code == 400


async def test_delete_role_cascades_links(admin_session: AsyncSession) -> None:
    from src.db.models.identity import User

    m1 = await _seed_menu(admin_session, "system:role:list")
    svc = RoleService(admin_session)
    role, _ = await svc.create_role(
        RoleCreate(name="Doomed", code="doomed", menu_ids=[m1]), actor=_actor()
    )
    # Assign the role to a user so the delete cascade has something to clear.
    admin_session.add(
        User(id=5555, username="member", employee_no="E-5555", status="active")
    )
    admin_session.add(UserRole(user_id=5555, role_id=role.id))
    await admin_session.commit()

    await svc.delete_role(role.id, actor=_actor())

    # Soft-deleted role no longer fetchable; all link rows physically gone.
    with pytest.raises(AppError):
        await svc.get_role(role.id, _actor())
    menus = await admin_session.scalars(
        select(RoleMenu.menu_id).where(RoleMenu.role_id == role.id)
    )
    assert menus.all() == []
    user_links = await admin_session.scalars(
        select(UserRole.id).where(UserRole.role_id == role.id)
    )
    assert user_links.all() == []


async def test_delete_role_not_found_raises(admin_session: AsyncSession) -> None:
    svc = RoleService(admin_session)
    with pytest.raises(AppError) as exc:
        await svc.delete_role(999999, actor=_actor())
    assert exc.value.status_code == 404


async def test_list_roles_bulk_grants(admin_session: AsyncSession) -> None:
    m1 = await _seed_menu(admin_session, "a:b:c")
    svc = RoleService(admin_session)
    await svc.create_role(
        RoleCreate(name="R1", code="r1", menu_ids=[m1]), actor=_actor()
    )
    await svc.create_role(RoleCreate(name="R2", code="r2"), actor=_actor())
    listing = await svc.list_roles(_actor())
    by_code = {r.code: menus for r, menus in listing.items}
    assert listing.total == 2
    assert by_code["r1"] == [m1]
    assert by_code["r2"] == []  # role with no menus defaults to empty list


# ---- privilege-escalation guards (role create/update) ----------------------


async def test_create_role_with_menu_beyond_actor_perms_rejected(
    admin_session: AsyncSession,
) -> None:
    """A scoped actor cannot mint a role granting a permission it lacks."""
    menu_id = await _seed_menu(admin_session, "system:user:delete")
    actor = _actor(user_id=2000, permissions={"system:role:list"})
    svc = RoleService(admin_session)
    with pytest.raises(AppError) as exc:
        await svc.create_role(
            RoleCreate(name="Esc", code="esc", menu_ids=[menu_id]), actor=actor
        )
    assert exc.value.status_code == 403


async def test_create_role_with_subset_menu_allowed(
    admin_session: AsyncSession,
) -> None:
    """An actor may grant a menu whose perm it already holds."""
    menu_id = await _seed_menu(admin_session, "system:user:list")
    actor = _actor(user_id=2000, permissions={"system:user:list", "system:role:add"})
    svc = RoleService(admin_session)
    role, menu_ids = await svc.create_role(
        RoleCreate(name="Ok", code="okrole", menu_ids=[menu_id]), actor=actor
    )
    assert menu_ids == [menu_id]


async def test_superadmin_may_grant_any_menu(admin_session: AsyncSession) -> None:
    menu_id = await _seed_menu(admin_session, "anything:goes:here")
    svc = RoleService(admin_session)
    # default _actor() is super-admin (perms {"*:*:*"} + superadmin role code).
    _, menu_ids = await svc.create_role(
        RoleCreate(name="Su", code="su", menu_ids=[menu_id]), actor=_actor()
    )
    assert menu_ids == [menu_id]


async def test_non_superuser_cannot_create_superadmin_role(
    admin_session: AsyncSession,
) -> None:
    """A scoped actor cannot mint a ``superadmin``-coded role.

    The code IS the super-admin grant (is_superuser is code-based), so a no-menu
    superadmin role would pass the menu-subset guard (zero conferred perms) yet
    self-mint full super-admin once assigned. The code guard blocks it up front.
    """
    actor = _actor(user_id=2000, department_id=10, permissions={"system:role:add"})
    svc = RoleService(admin_session)
    with pytest.raises(AppError) as exc:
        await svc.create_role(
            RoleCreate(name="Sneaky", code=SUPERADMIN_ROLE_CODE), actor=actor
        )
    assert exc.value.status_code == 403


async def test_superuser_may_create_superadmin_role(
    admin_session: AsyncSession,
) -> None:
    """An actual super-admin may create a super-admin-coded role."""
    svc = RoleService(admin_session)
    role, _ = await svc.create_role(
        RoleCreate(name="Su", code=SUPERADMIN_ROLE_CODE), actor=_actor()
    )
    assert role.code == SUPERADMIN_ROLE_CODE


async def test_update_role_escalating_menu_rejected(
    admin_session: AsyncSession,
) -> None:
    """A scoped actor cannot edit a role to add a permission it lacks."""
    menu_id = await _seed_menu(admin_session, "system:user:delete")
    actor = _actor(user_id=2000, permissions={"system:role:list"})
    role = Role(
        name="Vis",
        code="visrole",
        status="active",
        created_by=2000,
    )
    admin_session.add(role)
    await admin_session.commit()
    svc = RoleService(admin_session)
    with pytest.raises(AppError) as exc:
        await svc.update_role(role.id, RoleUpdate(menu_ids=[menu_id]), actor=actor)
    assert exc.value.status_code == 403


async def test_update_role_escalation_guard_runs_before_mutation(
    admin_session: AsyncSession,
    sqlite_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A rejected escalating edit must not partially apply (no scalar flush)."""
    menu_id = await _seed_menu(admin_session, "system:user:delete")
    actor = _actor(user_id=2000, permissions={"system:role:list"})
    role = Role(
        name="Before",
        code="beforerole",
        status="active",
        sort_order=5,
        created_by=2000,
    )
    admin_session.add(role)
    await admin_session.commit()
    svc = RoleService(admin_session)
    with pytest.raises(AppError):
        await svc.update_role(
            role.id,
            RoleUpdate(name="Renamed", sort_order=99, menu_ids=[menu_id]),
            actor=actor,
        )
    # The name/sort_order change must NOT have been committed. Read back through
    # a fresh session — the AppError-poisoned one cannot be safely re-queried.
    async with sqlite_session_factory() as verify_session:
        reloaded = await verify_session.scalar(
            select(Role).where(Role.id == role.id)
        )
        assert reloaded is not None
        assert reloaded.name == "Before"
        assert reloaded.sort_order == 5


# ---- dominance guards (manage an EXISTING role) ----------------------------
# A scoped actor that can SEE a role more powerful than itself must still be
# refused on update/delete (else it could delete the superadmin role, or rename
# a role carrying a permission it could never have minted).


async def test_scoped_actor_cannot_delete_superadmin_role(
    admin_session: AsyncSession,
) -> None:
    """A scoped actor cannot delete the ``superadmin`` role — dominance blocks
    the reserved code regardless of who created it."""
    su_role = Role(
        name="su-del",
        code=SUPERADMIN_ROLE_CODE,
        status="active",
        created_by=2000,
    )
    admin_session.add(su_role)
    await admin_session.commit()
    actor = _actor(user_id=2000, department_id=10, permissions={"system:role:remove"})
    svc = RoleService(admin_session)

    with pytest.raises(AppError) as exc:
        await svc.delete_role(su_role.id, actor=actor)
    assert exc.value.status_code == 403
    assert (await admin_session.get(Role, su_role.id)).is_deleted is False


async def test_scoped_actor_cannot_rename_role_with_menu_beyond_perms(
    admin_session: AsyncSession,
) -> None:
    """Dominance on update: even a benign rename is refused when the EXISTING
    role grants a permission the actor lacks (it could never have minted it)."""
    menu_id = await _seed_menu(admin_session, "system:user:delete")
    role = Role(
        name="Power",
        code="powerrole",
        status="active",
        created_by=2000,
    )
    admin_session.add(role)
    await admin_session.flush()
    admin_session.add(RoleMenu(role_id=role.id, menu_id=menu_id))
    await admin_session.commit()
    actor = _actor(user_id=2000, permissions={"system:role:edit"})  # lacks user:delete
    svc = RoleService(admin_session)
    with pytest.raises(AppError) as exc:
        await svc.update_role(role.id, RoleUpdate(name="Renamed"), actor=actor)
    assert exc.value.status_code == 403


async def test_superuser_can_delete_superadmin_role(
    admin_session: AsyncSession,
) -> None:
    """Regression: the dominance guard must not block an actual super-admin."""
    su_role = Role(
        name="su-ok", code=SUPERADMIN_ROLE_CODE, status="active"
    )
    admin_session.add(su_role)
    await admin_session.commit()
    svc = RoleService(admin_session)
    await svc.delete_role(su_role.id, actor=_actor())  # default actor is super-admin
    assert (await admin_session.get(Role, su_role.id)).is_deleted is True


async def test_batch_delete_roles_skips_undominated(
    admin_session: AsyncSession,
) -> None:
    """Batch delete skips roles the actor cannot dominate (one carrying a
    permission the actor lacks), deleting only the ones it could have minted."""
    menu_id = await _seed_menu(admin_session, "system:user:delete")
    ok_role = Role(
        name="ok-bd", code="okbd", status="active",
        created_by=2000, create_dept=10,
    )
    blocked_role = Role(
        name="blocked-bd", code="blockedbd", status="active",
        created_by=2000, create_dept=10,
    )
    admin_session.add_all([ok_role, blocked_role])
    await admin_session.flush()
    # blocked_role carries a perm the actor lacks → undominated.
    admin_session.add(RoleMenu(role_id=blocked_role.id, menu_id=menu_id))
    await admin_session.commit()
    ok_id, blocked_id = ok_role.id, blocked_role.id
    actor = _actor(user_id=2000, department_id=10, permissions={"system:role:remove"})
    svc = RoleService(admin_session)

    result = await svc.batch_delete_roles([ok_id, blocked_id], actor=actor)
    assert result.affected == 1
    assert str(blocked_id) in result.skipped_ids
    assert (await admin_session.get(Role, ok_id)).is_deleted is True
    assert (await admin_session.get(Role, blocked_id)).is_deleted is False
