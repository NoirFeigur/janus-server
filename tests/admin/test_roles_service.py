"""Direct unit tests for ``RoleService`` (service layer, no HTTP).

Drives the service with a plain ``await`` against an in-memory SQLite session.
Route tests (``test_roles.py``) already prove the behaviour end-to-end; these
exist to (a) give honest, measurable coverage of the service body — the
``ASGITransport`` route path corrupts coverage.py's C tracer on CPython 3.11 —
and (b) pin branch-level invariants (custom-scope dept clearing on update, the
unique-code guard, the full delete cascade) at the unit level.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.admin.roles.schemas import RoleCreate, RoleUpdate
from src.admin.roles.service import RoleService
from src.auth.service import AuthenticatedUser
from src.db.models.identity import Menu, Role, RoleDept, RoleMenu, UserRole
from src.enums import DataScope
from src.exceptions import AppError

pytestmark = pytest.mark.asyncio

ACTOR_ID = 1000


def _actor(
    *,
    user_id: int = ACTOR_ID,
    department_id: int | None = 10,
    permissions: set[str] | None = None,
) -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id=user_id,
        username="admin",
        department_id=department_id,
        permissions=frozenset(permissions or {"*:*:*"}),
    )


async def _seed_menu(session: AsyncSession, perm: str) -> int:
    menu = Menu(name=f"m.{perm}", menu_type="button", perms=perm, status="active")
    session.add(menu)
    await session.commit()
    return menu.id


async def test_create_role_with_menus(admin_session: AsyncSession) -> None:
    menu_id = await _seed_menu(admin_session, "system:user:list")
    svc = RoleService(admin_session)
    role, menu_ids, dept_ids = await svc.create_role(
        RoleCreate(name="Viewer", code="viewer", menu_ids=[menu_id]), actor=_actor()
    )
    assert role.code == "viewer"
    assert role.created_by == ACTOR_ID
    assert role.create_dept == 10
    assert menu_ids == [menu_id]
    assert dept_ids == []


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


async def test_create_custom_scope_role_with_depts(
    admin_session: AsyncSession,
) -> None:
    from src.db.models.identity import Department

    admin_session.add_all(
        [Department(id=d, name=f"d{d}", parent_id=None) for d in (111, 222)]
    )
    await admin_session.commit()
    svc = RoleService(admin_session)
    role, _, dept_ids = await svc.create_role(
        RoleCreate(
            name="Custom",
            code="custom",
            data_scope=DataScope.custom,
            dept_ids=[111, 222],
        ),
        actor=_actor(),
    )
    assert set(dept_ids) == {111, 222}


async def test_create_custom_scope_unknown_dept_rejected(
    admin_session: AsyncSession,
) -> None:
    svc = RoleService(admin_session)
    with pytest.raises(AppError) as exc:
        await svc.create_role(
            RoleCreate(
                name="Bad",
                code="baddept",
                data_scope=DataScope.custom,
                dept_ids=[99999],
            ),
            actor=_actor(),
        )
    assert exc.value.status_code == 400


async def test_create_custom_scope_role_empty_depts(
    admin_session: AsyncSession,
) -> None:
    # custom scope + no dept grants: _validate_depts short-circuits (no rows to check).
    svc = RoleService(admin_session)
    role, _, dept_ids = await svc.create_role(
        RoleCreate(
            name="EmptyCustom", code="emptycustom", data_scope=DataScope.custom
        ),
        actor=_actor(),
    )
    assert dept_ids == []


async def test_get_role_not_found_raises(admin_session: AsyncSession) -> None:
    svc = RoleService(admin_session)
    with pytest.raises(AppError) as exc:
        await svc.get_role(123456, _actor())
    assert exc.value.status_code == 404


async def test_update_role_replaces_menus(admin_session: AsyncSession) -> None:
    m1 = await _seed_menu(admin_session, "system:user:list")
    m2 = await _seed_menu(admin_session, "system:user:add")
    svc = RoleService(admin_session)
    role, _, _ = await svc.create_role(
        RoleCreate(name="R", code="rr", menu_ids=[m1]), actor=_actor()
    )
    _, menu_ids, _ = await svc.update_role(
        role.id, RoleUpdate(menu_ids=[m2]), actor=_actor()
    )
    assert menu_ids == [m2]


async def test_update_role_unknown_menu_rejected(admin_session: AsyncSession) -> None:
    svc = RoleService(admin_session)
    role, _, _ = await svc.create_role(
        RoleCreate(name="R", code="rmenu"), actor=_actor()
    )
    with pytest.raises(AppError) as exc:
        await svc.update_role(role.id, RoleUpdate(menu_ids=[55555]), actor=_actor())
    assert exc.value.status_code == 400


async def test_leaving_custom_scope_clears_depts(admin_session: AsyncSession) -> None:
    from src.db.models.identity import Department

    admin_session.add(Department(id=111, name="d111", parent_id=None))
    await admin_session.commit()
    svc = RoleService(admin_session)
    role, _, dept_ids = await svc.create_role(
        RoleCreate(
            name="C", code="cc", data_scope=DataScope.custom, dept_ids=[111]
        ),
        actor=_actor(),
    )
    assert dept_ids == [111]
    # Switch away from custom → dept grants must be cleared.
    _, _, after = await svc.update_role(
        role.id, RoleUpdate(data_scope=DataScope.dept_only), actor=_actor()
    )
    assert after == []
    remaining = await admin_session.scalars(
        select(RoleDept.dept_id).where(RoleDept.role_id == role.id)
    )
    assert remaining.all() == []


async def test_update_custom_scope_replaces_depts(admin_session: AsyncSession) -> None:
    from src.db.models.identity import Department

    admin_session.add_all(
        [Department(id=d, name=f"d{d}", parent_id=None) for d in (300, 301)]
    )
    await admin_session.commit()
    svc = RoleService(admin_session)
    role, _, _ = await svc.create_role(
        RoleCreate(
            name="Cust", code="custupd", data_scope=DataScope.custom, dept_ids=[300]
        ),
        actor=_actor(),
    )
    _, _, dept_ids = await svc.update_role(
        role.id, RoleUpdate(dept_ids=[301]), actor=_actor()
    )
    assert dept_ids == [301]


async def test_delete_role_cascades_links(admin_session: AsyncSession) -> None:
    from src.db.models.identity import User

    m1 = await _seed_menu(admin_session, "system:role:list")
    svc = RoleService(admin_session)
    role, _, _ = await svc.create_role(
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
    by_code = {r.code: (menus, depts) for r, menus, depts in listing.items}
    assert listing.total == 2
    assert by_code["r1"][0] == [m1]
    assert by_code["r2"][0] == []  # role with no menus defaults to empty list


async def test_scoped_actor_only_sees_roles_in_scope(
    admin_session: AsyncSession,
) -> None:
    actor = _actor(user_id=2000, department_id=10, permissions={"system:role:list"})
    visible = Role(
        name="visible",
        code="visible",
        data_scope="self",
        status="active",
        created_by=123,
        create_dept=10,
    )
    own = Role(
        name="own",
        code="own",
        data_scope="self",
        status="active",
        created_by=2000,
        create_dept=None,
    )
    hidden = Role(
        name="hidden",
        code="hidden",
        data_scope="self",
        status="active",
        created_by=9999,
        create_dept=99,
    )
    scope_role = Role(name="scope", code="scope", data_scope="dept", status="active")
    admin_session.add_all([visible, own, hidden, scope_role])
    await admin_session.flush()
    admin_session.add(UserRole(user_id=2000, role_id=scope_role.id))
    await admin_session.commit()

    svc = RoleService(admin_session)
    listing = await svc.list_roles(actor)
    assert {role.code for role, _, _ in listing.items} == {"visible"}
    with pytest.raises(AppError) as exc:
        await svc.delete_role(hidden.id, actor=actor)
    assert exc.value.status_code == 403
