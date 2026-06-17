"""Route-level tests for admin menu CRUD + current-user menu listing."""

from __future__ import annotations

import pytest

from src.db.models.identity import Menu, Role, RoleMenu, UserRole
from tests.admin.conftest import ADMIN_ID, AdminCtx

pytestmark = pytest.mark.asyncio


async def test_create_update_and_list_menu(admin_ctx: AdminCtx) -> None:
    created = await admin_ctx.client.post(
        "/admin/menus",
        json={
            "name": "menu.system",
            "menu_type": "catalog",
            "path": "/system",
            "icon": "settings",
        },
    )
    assert created.status_code == 200, created.text
    menu_id = created.json()["data"]["id"]

    updated = await admin_ctx.client.put(
        f"/admin/menus/{menu_id}",
        json={"name": "menu.system.updated", "sort_order": 10},
    )
    assert updated.status_code == 200
    assert updated.json()["data"]["name"] == "menu.system.updated"

    listing = await admin_ctx.client.get("/admin/menus")
    names = [item["name"] for item in listing.json()["data"]]
    assert "menu.system.updated" in names


async def test_button_requires_permission_code(admin_ctx: AdminCtx) -> None:
    resp = await admin_ctx.client.post(
        "/admin/menus", json={"name": "button.bad", "menu_type": "button"}
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == "request.invalid"


async def test_non_button_rejects_permission_code(admin_ctx: AdminCtx) -> None:
    resp = await admin_ctx.client.post(
        "/admin/menus",
        json={
            "name": "menu.bad",
            "menu_type": "menu",
            "perms": "system:bad:list",
        },
    )
    assert resp.status_code == 400


async def test_reparent_cycle_rejected(admin_ctx: AdminCtx) -> None:
    parent = await admin_ctx.client.post(
        "/admin/menus", json={"name": "menu.a", "menu_type": "catalog"}
    )
    parent_id = parent.json()["data"]["id"]
    child = await admin_ctx.client.post(
        "/admin/menus",
        json={"name": "menu.b", "menu_type": "menu", "parent_id": parent_id},
    )
    child_id = child.json()["data"]["id"]

    resp = await admin_ctx.client.put(
        f"/admin/menus/{parent_id}", json={"parent_id": child_id}
    )
    assert resp.status_code == 400


async def test_delete_menu_blocks_children_and_role_grants(admin_ctx: AdminCtx) -> None:
    parent = await admin_ctx.client.post(
        "/admin/menus", json={"name": "menu.parent", "menu_type": "catalog"}
    )
    parent_id = parent.json()["data"]["id"]
    await admin_ctx.client.post(
        "/admin/menus",
        json={"name": "menu.child", "menu_type": "menu", "parent_id": parent_id},
    )
    blocked = await admin_ctx.client.delete(f"/admin/menus/{parent_id}")
    assert blocked.status_code == 400

    granted = await admin_ctx.client.post(
        "/admin/menus",
        json={
            "name": "button.granted",
            "menu_type": "button",
            "perms": "system:test:list",
        },
    )
    granted_id = int(granted.json()["data"]["id"])
    role = Role(name="grant", code="grant", data_scope="self", status="active")
    admin_ctx.session.add(role)
    await admin_ctx.session.flush()
    admin_ctx.session.add(RoleMenu(role_id=role.id, menu_id=granted_id))
    await admin_ctx.session.commit()

    still_blocked = await admin_ctx.client.delete(f"/admin/menus/{granted_id}")
    assert still_blocked.status_code == 400


async def test_current_menus_returns_only_user_grants(admin_ctx: AdminCtx) -> None:
    admin_ctx.state.perms = {"system:user:list"}  # proves no menu-admin perm needed.
    visible = Menu(
        name="menu.visible",
        menu_type="menu",
        path="/visible",
        status="active",
        visible=True,
    )
    hidden = Menu(
        name="menu.hidden",
        menu_type="menu",
        path="/hidden",
        status="active",
        visible=True,
    )
    disabled = Menu(
        name="menu.disabled",
        menu_type="menu",
        path="/disabled",
        status="disabled",
        visible=True,
    )
    admin_ctx.session.add_all([visible, hidden, disabled])
    await admin_ctx.session.flush()
    role = Role(name="current", code="current", data_scope="self", status="active")
    admin_ctx.session.add(role)
    await admin_ctx.session.flush()
    admin_ctx.session.add(UserRole(user_id=ADMIN_ID, role_id=role.id))
    admin_ctx.session.add(RoleMenu(role_id=role.id, menu_id=visible.id))
    admin_ctx.session.add(RoleMenu(role_id=role.id, menu_id=disabled.id))
    await admin_ctx.session.commit()

    resp = await admin_ctx.client.get("/admin/menus/current")
    assert resp.status_code == 200, resp.text
    names = [item["name"] for item in resp.json()["data"]]
    assert names == ["menu.visible"]


async def test_menu_management_requires_permission(admin_ctx: AdminCtx) -> None:
    admin_ctx.state.perms = {"system:user:list"}
    resp = await admin_ctx.client.get("/admin/menus")
    assert resp.status_code == 403
    assert resp.json()["code"] == "auth.forbidden"
