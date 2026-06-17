"""Direct unit tests for ``MenuService``."""

from __future__ import annotations

import pytest

from src.admin.menus.schemas import MenuCreate, MenuUpdate
from src.admin.menus.service import MenuService
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
