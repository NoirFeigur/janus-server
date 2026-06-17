"""Route-level tests for admin role CRUD + menu/dept assignment."""

from __future__ import annotations

import pytest

from src.db.models.identity import Department, Menu
from tests.admin.conftest import AdminCtx

pytestmark = pytest.mark.asyncio


async def _seed_menu(admin_ctx: AdminCtx, perm: str) -> int:
    menu = Menu(name=f"m.{perm}", menu_type="button", perms=perm, status="active")
    admin_ctx.session.add(menu)
    await admin_ctx.session.commit()
    return menu.id


async def _seed_depts(admin_ctx: AdminCtx, *dept_ids: int) -> None:
    admin_ctx.session.add_all(
        [Department(id=d, name=f"d{d}", parent_id=None) for d in dept_ids]
    )
    await admin_ctx.session.commit()


async def test_create_role_with_menus(admin_ctx: AdminCtx) -> None:
    menu_id = await _seed_menu(admin_ctx, "system:user:list")
    resp = await admin_ctx.client.post(
        "/admin/roles",
        json={"name": "Viewer", "code": "viewer", "menu_ids": [str(menu_id)]},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["code"] == "viewer"
    assert data["menu_ids"] == [str(menu_id)]


async def test_create_role_duplicate_code_rejected(admin_ctx: AdminCtx) -> None:
    await admin_ctx.client.post(
        "/admin/roles", json={"name": "R", "code": "dup"}
    )
    resp = await admin_ctx.client.post(
        "/admin/roles", json={"name": "R2", "code": "dup"}
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == "request.invalid"


async def test_create_role_unknown_menu_rejected(admin_ctx: AdminCtx) -> None:
    resp = await admin_ctx.client.post(
        "/admin/roles",
        json={"name": "Bad", "code": "bad", "menu_ids": ["88888"]},
    )
    assert resp.status_code == 400


async def test_create_custom_scope_role_with_depts(admin_ctx: AdminCtx) -> None:
    await _seed_depts(admin_ctx, 111, 222)
    resp = await admin_ctx.client.post(
        "/admin/roles",
        json={
            "name": "Custom",
            "code": "custom",
            "data_scope": "custom",
            "dept_ids": ["111", "222"],
        },
    )
    assert resp.status_code == 200, resp.text
    assert set(resp.json()["data"]["dept_ids"]) == {"111", "222"}


async def test_create_custom_scope_unknown_dept_rejected(admin_ctx: AdminCtx) -> None:
    resp = await admin_ctx.client.post(
        "/admin/roles",
        json={
            "name": "Bad",
            "code": "baddept",
            "data_scope": "custom",
            "dept_ids": ["99999"],
        },
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == "request.invalid"


async def test_update_role_replaces_menus(admin_ctx: AdminCtx) -> None:
    m1 = await _seed_menu(admin_ctx, "system:user:list")
    m2 = await _seed_menu(admin_ctx, "system:user:add")
    created = await admin_ctx.client.post(
        "/admin/roles",
        json={"name": "R", "code": "rr", "menu_ids": [str(m1)]},
    )
    role_id = created.json()["data"]["id"]
    resp = await admin_ctx.client.put(
        f"/admin/roles/{role_id}", json={"menu_ids": [str(m2)]}
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["menu_ids"] == [str(m2)]


async def test_leaving_custom_scope_clears_depts(admin_ctx: AdminCtx) -> None:
    await _seed_depts(admin_ctx, 111)
    created = await admin_ctx.client.post(
        "/admin/roles",
        json={
            "name": "C",
            "code": "cc",
            "data_scope": "custom",
            "dept_ids": ["111"],
        },
    )
    role_id = created.json()["data"]["id"]
    resp = await admin_ctx.client.put(
        f"/admin/roles/{role_id}", json={"data_scope": "dept"}
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["dept_ids"] == []


async def test_delete_role(admin_ctx: AdminCtx) -> None:
    created = await admin_ctx.client.post(
        "/admin/roles", json={"name": "Tmp", "code": "tmp"}
    )
    role_id = created.json()["data"]["id"]
    resp = await admin_ctx.client.delete(f"/admin/roles/{role_id}")
    assert resp.status_code == 200
    listing = await admin_ctx.client.get("/admin/roles")
    codes = [r["code"] for r in listing.json()["data"]]
    assert "tmp" not in codes


async def test_delete_role_removes_user_assignments(admin_ctx: AdminCtx) -> None:
    """IMPORTANT-3: deleting a role must drop its UserRole links (no stale ids)."""
    from src.db.models.identity import User, UserRole

    created = await admin_ctx.client.post(
        "/admin/roles", json={"name": "Assigned", "code": "assigned"}
    )
    role_id = int(created.json()["data"]["id"])
    # Assign the role to a user directly.
    admin_ctx.session.add(
        User(id=5555, username="member", employee_no="E-5555", status="active")
    )
    admin_ctx.session.add(UserRole(user_id=5555, role_id=role_id))
    await admin_ctx.session.commit()

    resp = await admin_ctx.client.delete(f"/admin/roles/{role_id}")
    assert resp.status_code == 200

    # No UserRole row should remain for the deleted role.
    from sqlalchemy import select

    remaining = await admin_ctx.session.scalars(
        select(UserRole.id).where(UserRole.role_id == role_id)
    )
    assert remaining.all() == []


async def test_role_endpoints_require_permission(admin_ctx: AdminCtx) -> None:
    admin_ctx.state.perms = {"system:user:list"}  # lacks role perms
    resp = await admin_ctx.client.post(
        "/admin/roles", json={"name": "X", "code": "x"}
    )
    assert resp.status_code == 403


async def test_invalid_data_scope_value_422(admin_ctx: AdminCtx) -> None:
    resp = await admin_ctx.client.post(
        "/admin/roles",
        json={"name": "X", "code": "x", "data_scope": "bogus"},
    )
    assert resp.status_code == 422
    assert resp.json()["code"] == "request.invalid"
