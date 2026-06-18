"""Route-level tests for admin role CRUD + menu/dept assignment."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from src.db.models.identity import Department, Menu, Role, RoleDept, RoleMenu, User, UserRole
from tests.admin.conftest import ADMIN_ID, AdminCtx

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


async def _set_dept_scoped_actor(
    admin_ctx: AdminCtx, *, department_id: int, perms: set[str]
) -> None:
    await admin_ctx.session.execute(
        UserRole.__table__.delete().where(UserRole.user_id == ADMIN_ID)
    )
    scoped = Role(
        name=f"d{department_id}role",
        code=f"d{department_id}role",
        data_scope="dept",
        status="active",
    )
    admin_ctx.session.add(scoped)
    await admin_ctx.session.flush()
    admin_ctx.session.add(UserRole(user_id=ADMIN_ID, role_id=scoped.id))
    await admin_ctx.session.commit()
    admin_ctx.state.department_id = department_id
    admin_ctx.state.perms = perms


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
            Role(name="Alpha Match", code="role-alpha", data_scope="self", status="active"),
            Role(name="Beta Only", code="beta-only", data_scope="self", status="active"),
            Role(name="Code Hit", code="code-alpha-hit", data_scope="self", status="active"),
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
            Role(name="anna", code="role-anna", data_scope="self", status="active"),
            Role(name="zoe", code="role-zoe", data_scope="self", status="active"),
            Role(name="mike", code="role-mike", data_scope="self", status="active"),
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


async def test_batch_delete_roles_skips_out_of_scope(admin_ctx: AdminCtx) -> None:
    menu = Menu(name="m.batch", menu_type="button", perms="system:role:list", status="active")
    in_scope = Role(
        name="Batch In",
        code="batch-in",
        data_scope="custom",
        status="active",
        created_by=123,
        create_dept=3100,
    )
    out_scope = Role(
        name="Batch Out",
        code="batch-out",
        data_scope="custom",
        status="active",
        created_by=123,
        create_dept=3200,
    )
    assignee = User(id=3199, username="role-member", employee_no="E-3199", status="active")
    admin_ctx.session.add_all(
        [
            Department(id=3100, name="d3100", parent_id=None),
            Department(id=3200, name="d3200", parent_id=None),
            menu,
            in_scope,
            out_scope,
            assignee,
        ]
    )
    await admin_ctx.session.flush()
    admin_ctx.session.add_all(
        [
            RoleMenu(role_id=in_scope.id, menu_id=menu.id),
            RoleMenu(role_id=out_scope.id, menu_id=menu.id),
            RoleDept(role_id=in_scope.id, dept_id=3100),
            RoleDept(role_id=out_scope.id, dept_id=3200),
            UserRole(user_id=assignee.id, role_id=in_scope.id),
            UserRole(user_id=assignee.id, role_id=out_scope.id),
        ]
    )
    # Actor holds the menu's perm so it DOMINATES both roles (perm subset ok) —
    # isolating the skip to the data-scope axis: out_scope's custom dept (3200)
    # lies outside the actor's scope {3100}, so it is skipped while in_scope is
    # deleted. (Without the perm the dominance guard would skip BOTH, which is a
    # separate guarantee covered by the service-level dominance tests.)
    await _set_dept_scoped_actor(
        admin_ctx,
        department_id=3100,
        perms={"system:role:remove", "system:role:list"},
    )

    resp = await admin_ctx.client.post(
        "/admin/roles/batch-delete",
        json={"ids": [str(in_scope.id), str(out_scope.id)]},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data == {
        "requested": 2,
        "affected": 1,
        "skipped_ids": [str(out_scope.id)],
    }

    await admin_ctx.session.refresh(in_scope)
    await admin_ctx.session.refresh(out_scope)
    assert in_scope.is_deleted is True
    assert out_scope.is_deleted is False
    role_menus = await admin_ctx.session.scalars(
        select(RoleMenu.role_id)
        .where(RoleMenu.role_id.in_([in_scope.id, out_scope.id]))
        .order_by(RoleMenu.role_id)
    )
    role_depts = await admin_ctx.session.scalars(
        select(RoleDept.role_id)
        .where(RoleDept.role_id.in_([in_scope.id, out_scope.id]))
        .order_by(RoleDept.role_id)
    )
    user_roles = await admin_ctx.session.scalars(
        select(UserRole.role_id)
        .where(UserRole.role_id.in_([in_scope.id, out_scope.id]))
        .order_by(UserRole.role_id)
    )
    assert list(role_menus.all()) == [out_scope.id]
    assert list(role_depts.all()) == [out_scope.id]
    assert list(user_roles.all()) == [out_scope.id]


async def test_batch_delete_roles_clears_all_links_for_affected(
    admin_ctx: AdminCtx,
) -> None:
    menu = Menu(name="m.clear", menu_type="button", perms="system:role:edit", status="active")
    role = Role(
        name="Clear Links",
        code="clear-links",
        data_scope="custom",
        status="active",
    )
    assignee = User(id=3299, username="clear-member", employee_no="E-3299", status="active")
    admin_ctx.session.add_all(
        [Department(id=3300, name="d3300", parent_id=None), menu, role, assignee]
    )
    await admin_ctx.session.flush()
    admin_ctx.session.add_all(
        [
            RoleMenu(role_id=role.id, menu_id=menu.id),
            RoleDept(role_id=role.id, dept_id=3300),
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
    role_depts = await admin_ctx.session.scalars(
        select(RoleDept.id).where(RoleDept.role_id == role.id)
    )
    user_roles = await admin_ctx.session.scalars(
        select(UserRole.id).where(UserRole.role_id == role.id)
    )
    assert role_menus.all() == []
    assert role_depts.all() == []
    assert user_roles.all() == []


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
