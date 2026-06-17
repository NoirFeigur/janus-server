"""Route-level tests for admin user CRUD + role assignment + data scope."""

from __future__ import annotations

import pytest

from src.db.models.identity import Department, Menu, Role, RoleMenu, User, UserRole
from tests.admin.conftest import ADMIN_ID, AdminCtx

pytestmark = pytest.mark.asyncio


async def test_create_user_hashes_password_and_hides_it(admin_ctx: AdminCtx) -> None:
    resp = await admin_ctx.client.post(
        "/admin/users",
        json={"username": "carol", "employee_no": "E-100", "password": "pw12345"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["username"] == "carol"
    assert "password" not in data  # §0.8 — never exposed

    # Password is stored hashed, not plaintext.
    row = await admin_ctx.session.get(User, int(data["id"]))
    assert row is not None
    assert row.password is not None
    assert row.password != "pw12345"


async def test_create_user_duplicate_username_rejected(admin_ctx: AdminCtx) -> None:
    await admin_ctx.client.post(
        "/admin/users", json={"username": "dave", "employee_no": "E-1"}
    )
    resp = await admin_ctx.client.post(
        "/admin/users", json={"username": "dave", "employee_no": "E-2"}
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == "request.invalid"


async def test_create_user_duplicate_employee_no_rejected(admin_ctx: AdminCtx) -> None:
    await admin_ctx.client.post(
        "/admin/users", json={"username": "u1", "employee_no": "E-DUP"}
    )
    resp = await admin_ctx.client.post(
        "/admin/users", json={"username": "u2", "employee_no": "E-DUP"}
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == "request.invalid"


async def test_create_user_with_roles(admin_ctx: AdminCtx) -> None:
    role = Role(name="member", code="member", data_scope="self", status="active")
    admin_ctx.session.add(role)
    await admin_ctx.session.commit()
    resp = await admin_ctx.client.post(
        "/admin/users",
        json={"username": "erin", "employee_no": "E-3", "role_ids": [str(role.id)]},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["role_ids"] == [str(role.id)]


async def test_create_user_unknown_role_rejected(admin_ctx: AdminCtx) -> None:
    resp = await admin_ctx.client.post(
        "/admin/users",
        json={"username": "frank", "employee_no": "E-4", "role_ids": ["77777"]},
    )
    assert resp.status_code == 400


async def test_update_user_status_and_roles(admin_ctx: AdminCtx) -> None:
    created = await admin_ctx.client.post(
        "/admin/users", json={"username": "gina", "employee_no": "E-5"}
    )
    user_id = created.json()["data"]["id"]
    resp = await admin_ctx.client.put(
        f"/admin/users/{user_id}", json={"status": "disabled"}
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["status"] == "disabled"


async def test_delete_user(admin_ctx: AdminCtx) -> None:
    created = await admin_ctx.client.post(
        "/admin/users", json={"username": "harry", "employee_no": "E-6"}
    )
    user_id = created.json()["data"]["id"]
    resp = await admin_ctx.client.delete(f"/admin/users/{user_id}")
    assert resp.status_code == 200


async def test_user_endpoints_require_permission(admin_ctx: AdminCtx) -> None:
    admin_ctx.state.perms = {"system:dept:list"}  # lacks user perms
    resp = await admin_ctx.client.get("/admin/users")
    assert resp.status_code == 403


async def test_list_users_respects_data_scope(admin_ctx: AdminCtx) -> None:
    # Two departments; users in each. Actor scoped to dept 500 only.
    admin_ctx.session.add_all(
        [
            Department(id=500, name="d500", parent_id=None),
            Department(id=600, name="d600", parent_id=None),
            User(id=51, username="in500", employee_no="E-51", department_id=500),
            User(id=61, username="in600", employee_no="E-61", department_id=600),
        ]
    )
    # Replace the admin actor's role with a dept-scoped one (dept 500).
    await admin_ctx.session.execute(
        UserRole.__table__.delete().where(UserRole.user_id == ADMIN_ID)
    )
    scoped = Role(name="d500role", code="d500role", data_scope="dept", status="active")
    admin_ctx.session.add(scoped)
    await admin_ctx.session.flush()
    admin_ctx.session.add(UserRole(user_id=ADMIN_ID, role_id=scoped.id))
    await admin_ctx.session.commit()

    admin_ctx.state.department_id = 500  # actor's own department
    admin_ctx.state.perms = {"system:user:list"}  # non-superuser → scope applies

    resp = await admin_ctx.client.get("/admin/users")
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert "items" not in body
    assert "total" not in body
    page = body["data"]
    assert {"items", "total", "limit", "offset"} <= page.keys()
    usernames = {u["username"] for u in page["items"]}
    assert page["total"] >= 1
    assert "in500" in usernames
    assert "in600" not in usernames  # out of scope


async def test_mutation_out_of_scope_forbidden(admin_ctx: AdminCtx) -> None:
    admin_ctx.session.add_all(
        [
            Department(id=700, name="d700", parent_id=None),
            User(id=71, username="in700", employee_no="E-71", department_id=700),
        ]
    )
    await admin_ctx.session.execute(
        UserRole.__table__.delete().where(UserRole.user_id == ADMIN_ID)
    )
    scoped = Role(name="d999role", code="d999role", data_scope="dept", status="active")
    admin_ctx.session.add(scoped)
    await admin_ctx.session.flush()
    admin_ctx.session.add(UserRole(user_id=ADMIN_ID, role_id=scoped.id))
    await admin_ctx.session.commit()

    admin_ctx.state.department_id = 999  # actor sees only dept 999, not 700
    admin_ctx.state.perms = {"system:user:edit"}  # non-superuser → scope applies

    resp = await admin_ctx.client.put(
        "/admin/users/71", json={"status": "disabled"}
    )
    assert resp.status_code == 403
    assert resp.json()["code"] == "auth.forbidden"


async def test_scoped_admin_cannot_create_out_of_scope_user(
    admin_ctx: AdminCtx,
) -> None:
    """CRITICAL-1: a dept-scoped admin must not create users outside their scope."""
    admin_ctx.session.add_all(
        [
            Department(id=800, name="d800", parent_id=None),
            Department(id=801, name="d801", parent_id=None),
        ]
    )
    await admin_ctx.session.execute(
        UserRole.__table__.delete().where(UserRole.user_id == ADMIN_ID)
    )
    scoped = Role(name="d800role", code="d800role", data_scope="dept", status="active")
    admin_ctx.session.add(scoped)
    await admin_ctx.session.flush()
    admin_ctx.session.add(UserRole(user_id=ADMIN_ID, role_id=scoped.id))
    await admin_ctx.session.commit()

    admin_ctx.state.department_id = 800
    admin_ctx.state.perms = {"system:user:add"}

    # Out-of-scope department → forbidden.
    out = await admin_ctx.client.post(
        "/admin/users",
        json={"username": "x1", "employee_no": "E-x1", "department_id": "801"},
    )
    assert out.status_code == 403

    # No department (null) under a restricted scope → forbidden.
    nodept = await admin_ctx.client.post(
        "/admin/users",
        json={"username": "x2", "employee_no": "E-x2"},
    )
    assert nodept.status_code == 403

    # In-scope department → allowed.
    ok = await admin_ctx.client.post(
        "/admin/users",
        json={"username": "x3", "employee_no": "E-x3", "department_id": "800"},
    )
    assert ok.status_code == 200, ok.text


async def test_non_superuser_cannot_assign_role_granting_unheld_perms(
    admin_ctx: AdminCtx,
) -> None:
    """CRITICAL-2: privilege-escalation guard on role assignment."""
    # A powerful role granting a permission the actor does NOT hold.
    powerful = Role(name="power", code="power", data_scope="all", status="active")
    menu = Menu(
        name="m.super", menu_type="button", perms="*:*:*", status="active"
    )
    admin_ctx.session.add_all([powerful, menu])
    await admin_ctx.session.flush()
    admin_ctx.session.add(RoleMenu(role_id=powerful.id, menu_id=menu.id))
    await admin_ctx.session.commit()

    # Actor can add users but is NOT a superuser.
    admin_ctx.state.perms = {"system:user:add"}

    resp = await admin_ctx.client.post(
        "/admin/users",
        json={
            "username": "escalate",
            "employee_no": "E-esc",
            "role_ids": [str(powerful.id)],
        },
    )
    assert resp.status_code == 403
    assert resp.json()["code"] == "auth.forbidden"


async def test_superuser_can_assign_any_role(admin_ctx: AdminCtx) -> None:
    """Counterpart: a super-admin (``*:*:*``) may assign a powerful role."""
    powerful = Role(name="power2", code="power2", data_scope="all", status="active")
    menu = Menu(name="m.s2", menu_type="button", perms="*:*:*", status="active")
    admin_ctx.session.add_all([powerful, menu])
    await admin_ctx.session.flush()
    admin_ctx.session.add(RoleMenu(role_id=powerful.id, menu_id=menu.id))
    await admin_ctx.session.commit()

    # Default actor has *:*:* (superuser).
    resp = await admin_ctx.client.post(
        "/admin/users",
        json={
            "username": "blessed",
            "employee_no": "E-bless",
            "role_ids": [str(powerful.id)],
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["role_ids"] == [str(powerful.id)]
