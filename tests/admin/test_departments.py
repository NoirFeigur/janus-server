"""Route-level tests for admin department CRUD."""

from __future__ import annotations

import pytest

from src.db.models.identity import Department, User
from tests.admin.conftest import AdminCtx

pytestmark = pytest.mark.asyncio


async def _create_department(
    admin_ctx: AdminCtx,
    name: str,
    *,
    parent_id: str | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {"name": name}
    if parent_id is not None:
        payload["parent_id"] = parent_id
    resp = await admin_ctx.client.post("/admin/departments", json=payload)
    assert resp.status_code == 200, resp.text
    data: dict[str, object] = resp.json()["data"]
    return data


async def _list_department_names(admin_ctx: AdminCtx, *, keyword: str | None = None) -> list[str]:
    params = {"keyword": keyword} if keyword is not None else None
    resp = await admin_ctx.client.get("/admin/departments", params=params)
    assert resp.status_code == 200, resp.text
    return [item["name"] for item in resp.json()["data"]]


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


async def test_list_departments_keyword_includes_matched_and_ancestors(
    admin_ctx: AdminCtx,
) -> None:
    root = await _create_department(admin_ctx, "Corporate")
    child = await _create_department(admin_ctx, "Security", parent_id=str(root["id"]))
    await _create_department(admin_ctx, "Needle Response", parent_id=str(child["id"]))
    unrelated = await _create_department(admin_ctx, "Operations")
    await _create_department(admin_ctx, "Fulfillment", parent_id=str(unrelated["id"]))

    assert await _list_department_names(admin_ctx, keyword="needle") == [
        "Corporate",
        "Security",
        "Needle Response",
    ]


async def test_list_departments_no_keyword_returns_full_tree(admin_ctx: AdminCtx) -> None:
    root = await _create_department(admin_ctx, "Root")
    await _create_department(admin_ctx, "Child", parent_id=str(root["id"]))
    await _create_department(admin_ctx, "Sibling")

    assert await _list_department_names(admin_ctx) == ["Root", "Child", "Sibling"]


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


async def test_batch_delete_departments_skips_with_active_children(
    admin_ctx: AdminCtx,
) -> None:
    parent = Department(id=5100, name="Batch Parent", parent_id=None)
    child = Department(id=5101, name="Batch Child", parent_id=parent.id)
    empty = Department(id=5102, name="Batch Empty", parent_id=None)
    admin_ctx.session.add_all([parent, child, empty])
    await admin_ctx.session.commit()

    resp = await admin_ctx.client.post(
        "/admin/departments/batch-delete",
        json={"ids": [str(parent.id), str(empty.id)]},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"] == {
        "requested": 2,
        "affected": 1,
        "skipped_ids": [str(parent.id)],
    }

    await admin_ctx.session.refresh(parent)
    await admin_ctx.session.refresh(child)
    await admin_ctx.session.refresh(empty)
    assert parent.is_deleted is False
    assert child.is_deleted is False
    assert empty.is_deleted is True


async def test_batch_delete_departments_skips_with_active_members(
    admin_ctx: AdminCtx,
) -> None:
    dept = Department(id=5200, name="Staffed Batch", parent_id=None)
    member = User(
        id=5201,
        username="dept-member",
        employee_no="E-5201",
        department_id=dept.id,
        status="active",
    )
    admin_ctx.session.add_all([dept, member])
    await admin_ctx.session.commit()

    resp = await admin_ctx.client.post(
        "/admin/departments/batch-delete", json={"ids": [str(dept.id)]}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"] == {
        "requested": 1,
        "affected": 0,
        "skipped_ids": [str(dept.id)],
    }

    await admin_ctx.session.refresh(dept)
    assert dept.is_deleted is False


async def test_batch_delete_departments_skips_nonexistent(admin_ctx: AdminCtx) -> None:
    resp = await admin_ctx.client.post(
        "/admin/departments/batch-delete", json={"ids": ["999999"]}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"] == {
        "requested": 1,
        "affected": 0,
        "skipped_ids": ["999999"],
    }


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
