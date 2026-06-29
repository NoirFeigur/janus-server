"""Route-level tests for admin menu CRUD + current-user menu listing."""

from __future__ import annotations

import pytest
from sqlalchemy import select

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


async def test_list_menus_keyword_includes_matched_and_ancestors(
    admin_ctx: AdminCtx,
) -> None:
    root = Menu(name="menu.root", menu_type="catalog", sort_order=1)
    unrelated = Menu(name="menu.unrelated", menu_type="menu", sort_order=2)
    admin_ctx.session.add_all([root, unrelated])
    await admin_ctx.session.flush()
    parent = Menu(
        name="menu.parent",
        menu_type="menu",
        parent_id=root.id,
        sort_order=3,
    )
    sibling = Menu(
        name="menu.sibling",
        menu_type="menu",
        parent_id=root.id,
        sort_order=4,
    )
    admin_ctx.session.add_all([parent, sibling])
    await admin_ctx.session.flush()
    leaf = Menu(
        name="Leaf Keyword Only",
        menu_type="menu",
        parent_id=parent.id,
        sort_order=5,
    )
    admin_ctx.session.add(leaf)
    await admin_ctx.session.commit()

    resp = await admin_ctx.client.get("/admin/menus?keyword=keyword")
    assert resp.status_code == 200, resp.text
    names = [item["name"] for item in resp.json()["data"]]
    assert names == ["menu.root", "menu.parent", "Leaf Keyword Only"]


async def test_list_menus_keyword_matches_perms_or_path(admin_ctx: AdminCtx) -> None:
    root = Menu(name="menu.settings", menu_type="catalog", sort_order=1)
    admin_ctx.session.add(root)
    await admin_ctx.session.flush()
    button = Menu(
        name="button.audit",
        menu_type="button",
        parent_id=root.id,
        perms="system:audit:export",
        sort_order=2,
    )
    route = Menu(
        name="menu.logs",
        menu_type="menu",
        parent_id=root.id,
        path="/admin/logs/special-path",
        sort_order=3,
    )
    unrelated = Menu(name="menu.other", menu_type="menu", sort_order=4)
    admin_ctx.session.add_all([button, route, unrelated])
    await admin_ctx.session.commit()

    perms_resp = await admin_ctx.client.get("/admin/menus?keyword=AUDIT")
    assert perms_resp.status_code == 200, perms_resp.text
    assert [item["name"] for item in perms_resp.json()["data"]] == [
        "menu.settings",
        "button.audit",
    ]

    path_resp = await admin_ctx.client.get("/admin/menus?keyword=special-path")
    assert path_resp.status_code == 200, path_resp.text
    assert [item["name"] for item in path_resp.json()["data"]] == [
        "menu.settings",
        "menu.logs",
    ]


async def test_list_menus_no_keyword_returns_full_tree(admin_ctx: AdminCtx) -> None:
    root = Menu(name="menu.full.root", menu_type="catalog", sort_order=1)
    sibling = Menu(name="menu.full.sibling", menu_type="menu", sort_order=2)
    admin_ctx.session.add_all([root, sibling])
    await admin_ctx.session.flush()
    child = Menu(
        name="menu.full.child",
        menu_type="menu",
        parent_id=root.id,
        sort_order=3,
    )
    admin_ctx.session.add(child)
    await admin_ctx.session.commit()

    resp = await admin_ctx.client.get("/admin/menus")
    assert resp.status_code == 200, resp.text
    names = [item["name"] for item in resp.json()["data"]]
    assert names == ["menu.full.root", "menu.full.sibling", "menu.full.child"]


async def test_batch_delete_menus_skips_nodes_with_children(admin_ctx: AdminCtx) -> None:
    parent = Menu(name="menu.batch.parent", menu_type="catalog", sort_order=1)
    leaf = Menu(name="menu.batch.leaf", menu_type="menu", sort_order=2)
    admin_ctx.session.add_all([parent, leaf])
    await admin_ctx.session.flush()
    child = Menu(
        name="menu.batch.child",
        menu_type="menu",
        parent_id=parent.id,
        sort_order=3,
    )
    admin_ctx.session.add(child)
    await admin_ctx.session.commit()

    resp = await admin_ctx.client.post(
        "/admin/menus/batch-delete",
        json={"ids": [str(parent.id), str(leaf.id)]},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"] == {
        "requested": 2,
        "affected": 1,
        "skipped_ids": [str(parent.id)],
    }

    await admin_ctx.session.refresh(parent)
    await admin_ctx.session.refresh(leaf)
    assert parent.is_deleted is False
    assert leaf.is_deleted is True


async def test_batch_delete_menus_skips_role_granted(admin_ctx: AdminCtx) -> None:
    granted = Menu(
        name="button.batch.granted",
        menu_type="button",
        perms="system:batch:granted",
        sort_order=1,
    )
    role = Role(name="menu-batch-grant", code="menu-batch-grant")
    admin_ctx.session.add_all([granted, role])
    await admin_ctx.session.flush()
    admin_ctx.session.add(RoleMenu(role_id=role.id, menu_id=granted.id))
    await admin_ctx.session.commit()

    resp = await admin_ctx.client.post(
        "/admin/menus/batch-delete", json={"ids": [str(granted.id)]}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"] == {
        "requested": 1,
        "affected": 0,
        "skipped_ids": [str(granted.id)],
    }

    await admin_ctx.session.refresh(granted)
    assert granted.is_deleted is False


async def test_batch_delete_menus_skips_nonexistent(admin_ctx: AdminCtx) -> None:
    missing_id = 9_999_999

    resp = await admin_ctx.client.post(
        "/admin/menus/batch-delete", json={"ids": [str(missing_id)]}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"] == {
        "requested": 1,
        "affected": 0,
        "skipped_ids": [str(missing_id)],
    }

    rows = await admin_ctx.session.scalars(select(Menu.id))
    assert missing_id not in set(rows.all())


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
    role = Role(name="grant", code="grant", status="active")
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
    role = Role(name="current", code="current", status="active")
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
