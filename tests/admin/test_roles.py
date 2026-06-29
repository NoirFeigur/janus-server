"""Route-level tests for admin role CRUD + menu assignment."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from src.db.models.identity import Menu, Role, RoleMenu, User, UserRole
from tests.admin.conftest import AdminCtx

pytestmark = pytest.mark.asyncio


async def _seed_menu(admin_ctx: AdminCtx, perm: str) -> int:
    menu = Menu(name=f"m.{perm}", menu_type="button", perms=perm, status="active")
    admin_ctx.session.add(menu)
    await admin_ctx.session.commit()
    return menu.id


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


async def test_delete_role(admin_ctx: AdminCtx) -> None:
    created = await admin_ctx.client.post(
        "/admin/roles", json={"name": "Tmp", "code": "tmp"}
    )
    role_id = created.json()["data"]["id"]
    resp = await admin_ctx.client.delete(f"/admin/roles/{role_id}")
    assert resp.status_code == 200
    listing = await admin_ctx.client.get("/admin/roles")
    body = listing.json()
    assert body["success"] is True
    assert "items" not in body
    assert "total" not in body
    page = body["data"]
    assert {"items", "total", "limit", "offset"} <= page.keys()
    codes = [r["code"] for r in page["items"]]
    assert "tmp" not in codes


async def test_delete_role_removes_user_assignments(admin_ctx: AdminCtx) -> None:
    """IMPORTANT-3: deleting a role must drop its UserRole links (no stale ids)."""
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
    remaining = await admin_ctx.session.scalars(
        select(UserRole.id).where(UserRole.role_id == role_id)
    )
    assert remaining.all() == []


async def test_list_roles_keyword_filters_by_name_or_code(admin_ctx: AdminCtx) -> None:
    admin_ctx.session.add_all(
        [
            Role(name="Alpha Match", code="role-alpha", status="active"),
            Role(name="Beta Only", code="beta-only", status="active"),
            Role(name="Code Hit", code="code-alpha-hit", status="active"),
        ]
    )
    await admin_ctx.session.commit()

    resp = await admin_ctx.client.get("/admin/roles?keyword=ALPHA")
    assert resp.status_code == 200, resp.text
    codes = {item["code"] for item in resp.json()["data"]["items"]}
    assert codes == {"role-alpha", "code-alpha-hit"}


async def test_list_roles_sort_by_name_desc(admin_ctx: AdminCtx) -> None:
    admin_ctx.session.add_all(
        [
            Role(name="anna", code="role-anna", status="active"),
            Role(name="zoe", code="role-zoe", status="active"),
            Role(name="mike", code="role-mike", status="active"),
        ]
    )
    await admin_ctx.session.commit()

    resp = await admin_ctx.client.get(
        "/admin/roles?sort_by=name&sort_order=desc&limit=3"
    )
    assert resp.status_code == 200, resp.text
    names = [item["name"] for item in resp.json()["data"]["items"]]
    assert names == ["zoe", "mike", "anna"]


async def test_list_roles_invalid_sort_by_returns_400(admin_ctx: AdminCtx) -> None:
    resp = await admin_ctx.client.get("/admin/roles?sort_by=evil")
    assert resp.status_code == 400
    assert resp.json()["code"] == "request.invalid"


async def test_batch_delete_roles_skips_undominated(admin_ctx: AdminCtx) -> None:
    """Batch delete skips roles the actor cannot dominate (one carrying a
    permission the actor lacks), deleting only the ones it could have minted,
    and clears links only for the affected role."""
    safe_menu = Menu(
        name="m.safe", menu_type="button", perms="system:role:list", status="active"
    )
    esc_menu = Menu(
        name="m.esc", menu_type="button", perms="system:user:delete", status="active"
    )
    dominated = Role(name="Batch In", code="batch-in", status="active", created_by=123)
    undominated = Role(
        name="Batch Out", code="batch-out", status="active", created_by=123
    )
    assignee = User(
        id=3199, username="role-member", employee_no="E-3199", status="active"
    )
    admin_ctx.session.add_all([safe_menu, esc_menu, dominated, undominated, assignee])
    await admin_ctx.session.flush()
    admin_ctx.session.add_all(
        [
            RoleMenu(role_id=dominated.id, menu_id=safe_menu.id),
            RoleMenu(role_id=undominated.id, menu_id=esc_menu.id),
            UserRole(user_id=assignee.id, role_id=dominated.id),
            UserRole(user_id=assignee.id, role_id=undominated.id),
        ]
    )
    await admin_ctx.session.commit()
    # Non-super actor holds role:remove + the safe perm but NOT user:delete, so it
    # dominates `dominated` (perm subset ok) but not `undominated` (carries a perm
    # the actor lacks) → undominated is skipped, its links left intact.
    admin_ctx.state.perms = {"system:role:remove", "system:role:list"}

    resp = await admin_ctx.client.post(
        "/admin/roles/batch-delete",
        json={"ids": [str(dominated.id), str(undominated.id)]},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data == {
        "requested": 2,
        "affected": 1,
        "skipped_ids": [str(undominated.id)],
    }

    await admin_ctx.session.refresh(dominated)
    await admin_ctx.session.refresh(undominated)
    assert dominated.is_deleted is True
    assert undominated.is_deleted is False
    role_menus = await admin_ctx.session.scalars(
        select(RoleMenu.role_id)
        .where(RoleMenu.role_id.in_([dominated.id, undominated.id]))
        .order_by(RoleMenu.role_id)
    )
    user_roles = await admin_ctx.session.scalars(
        select(UserRole.role_id)
        .where(UserRole.role_id.in_([dominated.id, undominated.id]))
        .order_by(UserRole.role_id)
    )
    assert list(role_menus.all()) == [undominated.id]
    assert list(user_roles.all()) == [undominated.id]


async def test_batch_delete_roles_clears_all_links_for_affected(
    admin_ctx: AdminCtx,
) -> None:
    menu = Menu(
        name="m.clear", menu_type="button", perms="system:role:edit", status="active"
    )
    role = Role(name="Clear Links", code="clear-links", status="active")
    assignee = User(
        id=3299, username="clear-member", employee_no="E-3299", status="active"
    )
    admin_ctx.session.add_all([menu, role, assignee])
    await admin_ctx.session.flush()
    admin_ctx.session.add_all(
        [
            RoleMenu(role_id=role.id, menu_id=menu.id),
            UserRole(user_id=assignee.id, role_id=role.id),
        ]
    )
    await admin_ctx.session.commit()

    resp = await admin_ctx.client.post(
        "/admin/roles/batch-delete", json={"ids": [str(role.id)]}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"] == {"requested": 1, "affected": 1, "skipped_ids": []}

    await admin_ctx.session.refresh(role)
    assert role.is_deleted is True
    role_menus = await admin_ctx.session.scalars(
        select(RoleMenu.id).where(RoleMenu.role_id == role.id)
    )
    user_roles = await admin_ctx.session.scalars(
        select(UserRole.id).where(UserRole.role_id == role.id)
    )
    assert role_menus.all() == []
    assert user_roles.all() == []


async def test_role_endpoints_require_permission(admin_ctx: AdminCtx) -> None:
    admin_ctx.state.perms = {"system:user:list"}  # lacks role perms
    resp = await admin_ctx.client.post(
        "/admin/roles", json={"name": "X", "code": "x"}
    )
    assert resp.status_code == 403
