"""Direct unit tests for ``MenuService``."""

from __future__ import annotations

import pytest

from src.admin.menus.schemas import MenuCreate, MenuUpdate
from src.admin.menus.service import MenuService
from src.auth.constants import SUPERADMIN_ROLE_CODE
from src.auth.service import AuthenticatedUser
from src.db.models.identity import Menu, Role, RoleMenu, UserRole
from src.exceptions import AppError

pytestmark = pytest.mark.asyncio


def _actor() -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id=1000,
        username="admin",
        department_id=10,
        permissions=frozenset({"*:*:*"}),
        role_codes=frozenset({SUPERADMIN_ROLE_CODE}),
    )


async def test_create_update_delete_menu(admin_session) -> None:
    svc = MenuService(admin_session)
    menu = await svc.create_menu(
        MenuCreate(name="menu.root", menu_type="catalog"), actor=_actor()
    )
    assert menu.created_by == 1000
    assert menu.create_dept == 10

    updated = await svc.update_menu(
        menu.id, MenuUpdate(name="menu.root2"), actor=_actor()
    )
    assert updated.name == "menu.root2"

    await svc.delete_menu(menu.id, actor=_actor())
    with pytest.raises(AppError):
        await svc.update_menu(menu.id, MenuUpdate(name="gone"), actor=_actor())


async def test_parent_validation_and_cycle_guard(admin_session) -> None:
    svc = MenuService(admin_session)
    with pytest.raises(AppError):
        await svc.create_menu(
            MenuCreate(name="bad", menu_type="menu", parent_id=999), actor=_actor()
        )

    parent = await svc.create_menu(
        MenuCreate(name="parent", menu_type="catalog"), actor=_actor()
    )
    child = await svc.create_menu(
        MenuCreate(name="child", menu_type="menu", parent_id=parent.id), actor=_actor()
    )
    with pytest.raises(AppError):
        await svc.update_menu(parent.id, MenuUpdate(parent_id=child.id), actor=_actor())


def _scoped_actor(perms: set[str]) -> AuthenticatedUser:
    """A non-super-admin actor holding exactly ``perms`` (no superadmin code)."""
    return AuthenticatedUser(
        user_id=2000,
        username="scoped",
        department_id=10,
        permissions=frozenset(perms),
    )


async def test_non_superuser_cannot_create_menu_with_unheld_perm(
    admin_session,
) -> None:
    """A scoped actor cannot mint a menu granting a permission code it lacks.

    A menu's ``perms`` IS a permission grant (every role linking it confers the
    code), so without this guard ``system:menu:add`` would let an actor fabricate
    a ``*:*:*`` menu and escalate via any role they can link it to.
    """
    svc = MenuService(admin_session)
    actor = _scoped_actor({"system:menu:add"})
    with pytest.raises(AppError) as exc:
        await svc.create_menu(
            MenuCreate(name="esc", menu_type="button", perms="*:*:*"), actor=actor
        )
    assert exc.value.status_code == 403


async def test_non_superuser_cannot_edit_menu_perms_to_unheld_code(
    admin_session,
) -> None:
    """The CRITICAL escalation: editing an existing menu's ``perms`` to a code the
    actor lacks. The role→menu link is unchanged, so the role-edit guard never
    re-runs — this guard on ``update_menu`` is the only thing stopping it.
    """
    # A menu the actor's role already links, with a benign code.
    menu = Menu(
        name="m.benign", menu_type="button", perms="system:user:list", status="active"
    )
    admin_session.add(menu)
    await admin_session.commit()
    svc = MenuService(admin_session)
    actor = _scoped_actor({"system:menu:edit", "system:user:list"})
    with pytest.raises(AppError) as exc:
        await svc.update_menu(menu.id, MenuUpdate(perms="*:*:*"), actor=actor)
    assert exc.value.status_code == 403
    # The perms must NOT have changed on the rejected edit.
    await admin_session.refresh(menu)
    assert menu.perms == "system:user:list"


async def test_non_superuser_may_edit_unrelated_field_without_holding_perm(
    admin_session,
) -> None:
    """Editing a non-perms field must NOT require the actor to hold the menu's
    existing code (that was vetted when it was set)."""
    menu = Menu(
        name="m.x", menu_type="button", perms="system:user:delete", status="active"
    )
    admin_session.add(menu)
    await admin_session.commit()
    svc = MenuService(admin_session)
    # Actor does NOT hold system:user:delete, but only renames the menu.
    actor = _scoped_actor({"system:menu:edit"})
    updated = await svc.update_menu(menu.id, MenuUpdate(name="m.renamed"), actor=actor)
    assert updated.name == "m.renamed"
    assert updated.perms == "system:user:delete"  # unchanged


async def test_non_superuser_may_set_perm_it_holds(admin_session) -> None:
    """An actor may set a menu's perms to a code already within its own set."""
    svc = MenuService(admin_session)
    actor = _scoped_actor({"system:menu:add", "system:user:list"})
    menu = await svc.create_menu(
        MenuCreate(name="ok", menu_type="button", perms="system:user:list"),
        actor=actor,
    )
    assert menu.perms == "system:user:list"


async def test_superuser_may_set_any_menu_perm(admin_session) -> None:
    """Super-admin bypasses the perms-escalation guard entirely."""
    svc = MenuService(admin_session)
    menu = await svc.create_menu(
        MenuCreate(name="su", menu_type="button", perms="any:thing:here"),
        actor=_actor(),
    )
    assert menu.perms == "any:thing:here"


async def test_current_user_menu_filter(admin_session) -> None:
    visible = Menu(name="visible", menu_type="menu", status="active", visible=True)
    invisible = Menu(name="invisible", menu_type="menu", status="active", visible=False)
    admin_session.add_all([visible, invisible])
    await admin_session.flush()
    role = Role(name="r", code="r", data_scope="self", status="active")
    admin_session.add(role)
    await admin_session.flush()
    admin_session.add(UserRole(user_id=42, role_id=role.id))
    admin_session.add(RoleMenu(role_id=role.id, menu_id=visible.id))
    admin_session.add(RoleMenu(role_id=role.id, menu_id=invisible.id))
    await admin_session.commit()

    svc = MenuService(admin_session)
    menus = await svc.list_current_user_menus(
        AuthenticatedUser(
            user_id=42,
            username="u",
            department_id=None,
            permissions=frozenset({"system:user:list"}),
        )
    )
    assert [menu.name for menu in menus] == ["visible"]
