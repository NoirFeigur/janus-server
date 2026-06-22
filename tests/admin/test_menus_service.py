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


async def test_list_menus_keyword_filter_by_name(admin_session) -> None:
    """Keyword search matches menu name (case-insensitive) and pulls ancestors."""
    svc = MenuService(admin_session)
    parent = await svc.create_menu(
        MenuCreate(name="System", menu_type="catalog"), actor=_actor()
    )
    await svc.create_menu(
        MenuCreate(name="User Management", menu_type="menu", parent_id=parent.id),
        actor=_actor(),
    )
    await svc.create_menu(
        MenuCreate(name="Unrelated", menu_type="menu"), actor=_actor()
    )
    result = await svc.list_menus(keyword="user")
    names = {m.name for m in result}
    # The keyword match ("User Management") + its ancestor ("System") are included.
    assert "User Management" in names
    assert "System" in names
    # Unrelated is excluded.
    assert "Unrelated" not in names


async def test_list_menus_keyword_filter_by_perms(admin_session) -> None:
    """Keyword search also matches the perms field."""
    svc = MenuService(admin_session)
    await svc.create_menu(
        MenuCreate(name="Add Btn", menu_type="button", perms="system:user:add"),
        actor=_actor(),
    )
    await svc.create_menu(
        MenuCreate(name="Other Btn", menu_type="button", perms="system:role:list"),
        actor=_actor(),
    )
    result = await svc.list_menus(keyword="user:add")
    assert len(result) == 1
    assert result[0].name == "Add Btn"


async def test_list_menus_empty_keyword_returns_all(admin_session) -> None:
    """A whitespace-only keyword is treated as no filter."""
    svc = MenuService(admin_session)
    await svc.create_menu(MenuCreate(name="A", menu_type="catalog"), actor=_actor())
    await svc.create_menu(MenuCreate(name="B", menu_type="catalog"), actor=_actor())
    result = await svc.list_menus(keyword="   ")
    assert len(result) == 2


async def test_kind_validation_button_requires_perms(admin_session) -> None:
    """A menu of type ``button`` MUST have a non-empty perms code."""
    svc = MenuService(admin_session)
    with pytest.raises(AppError) as exc:
        await svc.create_menu(
            MenuCreate(name="btn", menu_type="button", perms=None), actor=_actor()
        )
    assert exc.value.status_code == 400


async def test_kind_validation_non_button_rejects_perms(admin_session) -> None:
    """A ``catalog`` or ``menu`` node MUST NOT carry a perms code."""
    svc = MenuService(admin_session)
    with pytest.raises(AppError) as exc:
        await svc.create_menu(
            MenuCreate(name="cat", menu_type="catalog", perms="system:user:list"),
            actor=_actor(),
        )
    assert exc.value.status_code == 400


async def test_delete_menu_with_children_blocked(admin_session) -> None:
    """A menu with active children cannot be deleted."""
    svc = MenuService(admin_session)
    parent = await svc.create_menu(
        MenuCreate(name="Parent", menu_type="catalog"), actor=_actor()
    )
    await svc.create_menu(
        MenuCreate(name="Child", menu_type="menu", parent_id=parent.id),
        actor=_actor(),
    )
    with pytest.raises(AppError) as exc:
        await svc.delete_menu(parent.id, actor=_actor())
    assert exc.value.status_code == 400


async def test_delete_menu_with_role_grant_blocked(admin_session) -> None:
    """A menu still linked to a role cannot be deleted."""
    svc = MenuService(admin_session)
    menu = await svc.create_menu(
        MenuCreate(name="Granted", menu_type="menu"), actor=_actor()
    )
    role = Role(name="r", code="r", data_scope="all", status="active")
    admin_session.add(role)
    await admin_session.flush()
    admin_session.add(RoleMenu(role_id=role.id, menu_id=menu.id))
    await admin_session.commit()
    with pytest.raises(AppError) as exc:
        await svc.delete_menu(menu.id, actor=_actor())
    assert exc.value.status_code == 400


async def test_batch_delete_menus_mixed(admin_session) -> None:
    """Batch delete: deletable items succeed, blocked/missing items are skipped."""
    svc = MenuService(admin_session)
    deletable = await svc.create_menu(
        MenuCreate(name="Del", menu_type="menu"), actor=_actor()
    )
    parent_with_child = await svc.create_menu(
        MenuCreate(name="Blocked", menu_type="catalog"), actor=_actor()
    )
    await svc.create_menu(
        MenuCreate(name="Kid", menu_type="menu", parent_id=parent_with_child.id),
        actor=_actor(),
    )
    await admin_session.commit()

    result = await svc.batch_delete_menus(
        [deletable.id, parent_with_child.id, 999999], actor=_actor()
    )
    assert result.affected == 1
    assert int(result.skipped_ids[0]) == parent_with_child.id or 999999 in [
        int(s) for s in result.skipped_ids
    ]
    assert len(result.skipped_ids) == 2


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
