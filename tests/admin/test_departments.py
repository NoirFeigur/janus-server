"""Route-level tests for admin department CRUD."""

from __future__ import annotations

import pytest

from tests.admin.conftest import AdminCtx

pytestmark = pytest.mark.asyncio


async def test_create_and_list_department(admin_ctx: AdminCtx) -> None:
    resp = await admin_ctx.client.post(
        "/admin/departments", json={"name": "Engineering"}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["success"] is True
    dept_id = body["data"]["id"]
    assert isinstance(dept_id, str)  # snowflake serialized as string

    listing = await admin_ctx.client.get("/admin/departments")
    names = [d["name"] for d in listing.json()["data"]]
    assert "Engineering" in names


async def test_create_child_with_bad_parent_rejected(admin_ctx: AdminCtx) -> None:
    resp = await admin_ctx.client.post(
        "/admin/departments", json={"name": "Sub", "parent_id": "99999"}
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == "request.invalid"


async def test_update_department(admin_ctx: AdminCtx) -> None:
    created = await admin_ctx.client.post(
        "/admin/departments", json={"name": "Old"}
    )
    dept_id = created.json()["data"]["id"]
    resp = await admin_ctx.client.put(
        f"/admin/departments/{dept_id}", json={"name": "New"}
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["name"] == "New"


async def test_delete_department_with_child_blocked(admin_ctx: AdminCtx) -> None:
    parent = await admin_ctx.client.post(
        "/admin/departments", json={"name": "Parent"}
    )
    parent_id = parent.json()["data"]["id"]
    await admin_ctx.client.post(
        "/admin/departments", json={"name": "Child", "parent_id": parent_id}
    )
    resp = await admin_ctx.client.delete(f"/admin/departments/{parent_id}")
    assert resp.status_code == 400
    assert resp.json()["code"] == "request.invalid"


async def test_delete_empty_department_succeeds(admin_ctx: AdminCtx) -> None:
    created = await admin_ctx.client.post(
        "/admin/departments", json={"name": "Temp"}
    )
    dept_id = created.json()["data"]["id"]
    resp = await admin_ctx.client.delete(f"/admin/departments/{dept_id}")
    assert resp.status_code == 200


async def test_reparent_cycle_rejected(admin_ctx: AdminCtx) -> None:
    a = await admin_ctx.client.post("/admin/departments", json={"name": "A"})
    a_id = a.json()["data"]["id"]
    b = await admin_ctx.client.post(
        "/admin/departments", json={"name": "B", "parent_id": a_id}
    )
    b_id = b.json()["data"]["id"]
    # Make A a child of B → cycle.
    resp = await admin_ctx.client.put(
        f"/admin/departments/{a_id}", json={"parent_id": b_id}
    )
    assert resp.status_code == 400


async def test_dept_endpoints_require_permission(admin_ctx: AdminCtx) -> None:
    admin_ctx.state.perms = {"system:role:list"}  # lacks any dept perm
    resp = await admin_ctx.client.get("/admin/departments")
    assert resp.status_code == 403
    assert resp.json()["code"] == "auth.forbidden"
