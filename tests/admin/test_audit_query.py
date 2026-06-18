"""Route-level tests for the read-only audit query endpoints (C6).

Two paged, permission-gated, read-only endpoints over the append-only audit
tables. Audit reads are permission-gated ONLY (no data-scope) — auditors need
global visibility by design. Rows are seeded directly; the GET endpoints are
not write methods so the oper-audit middleware never appends extra rows.
"""

from __future__ import annotations

import pytest

from src.db.models.audit import LoginLog, OperLog
from tests.admin.conftest import AdminCtx

pytestmark = pytest.mark.asyncio


async def _seed_oper(admin_ctx: AdminCtx, **overrides: object) -> OperLog:
    defaults: dict[str, object] = {
        "module": "role",
        "action": "create",
        "method": "POST",
        "path": "/admin/roles",
        "status": "success",
    }
    defaults.update(overrides)
    row = OperLog(**defaults)
    admin_ctx.session.add(row)
    await admin_ctx.session.commit()
    return row


async def _seed_login(admin_ctx: AdminCtx, **overrides: object) -> LoginLog:
    defaults: dict[str, object] = {"username": "alice", "status": "success"}
    defaults.update(overrides)
    row = LoginLog(**defaults)
    admin_ctx.session.add(row)
    await admin_ctx.session.commit()
    return row


async def test_list_oper_logs_returns_paged(admin_ctx: AdminCtx) -> None:
    await _seed_oper(admin_ctx, module="role", actor_id=1000, actor_name="admin")
    await _seed_oper(admin_ctx, module="user", action="delete", method="DELETE")

    resp = await admin_ctx.client.get("/admin/audit/oper-logs")
    assert resp.status_code == 200, resp.text
    page = resp.json()["data"]
    assert {"items", "total", "limit", "offset"} <= page.keys()
    assert page["total"] == 2
    modules = {item["module"] for item in page["items"]}
    assert modules == {"role", "user"}
    # Snowflake ids serialize as strings on the wire.
    first = page["items"][0]
    assert isinstance(first["id"], str)


async def test_list_oper_logs_filter_by_module(admin_ctx: AdminCtx) -> None:
    await _seed_oper(admin_ctx, module="role")
    await _seed_oper(admin_ctx, module="user")

    resp = await admin_ctx.client.get("/admin/audit/oper-logs?module=user")
    assert resp.status_code == 200, resp.text
    page = resp.json()["data"]
    assert page["total"] == 1
    assert page["items"][0]["module"] == "user"


async def test_list_oper_logs_filter_by_status(admin_ctx: AdminCtx) -> None:
    await _seed_oper(admin_ctx, status="success")
    await _seed_oper(admin_ctx, status="failure", error_code="auth.forbidden")

    resp = await admin_ctx.client.get("/admin/audit/oper-logs?status=failure")
    assert resp.status_code == 200, resp.text
    page = resp.json()["data"]
    assert page["total"] == 1
    assert page["items"][0]["status"] == "failure"
    assert page["items"][0]["error_code"] == "auth.forbidden"


async def test_list_login_logs_returns_paged(admin_ctx: AdminCtx) -> None:
    await _seed_login(admin_ctx, username="alice", status="success", user_id=1000)
    await _seed_login(
        admin_ctx, username="ghost", status="failure", failure_reason="user_not_found"
    )

    resp = await admin_ctx.client.get("/admin/audit/login-logs")
    assert resp.status_code == 200, resp.text
    page = resp.json()["data"]
    assert page["total"] == 2
    usernames = {item["username"] for item in page["items"]}
    assert usernames == {"alice", "ghost"}


async def test_list_login_logs_filter_by_status(admin_ctx: AdminCtx) -> None:
    await _seed_login(admin_ctx, username="alice", status="success")
    await _seed_login(
        admin_ctx, username="ghost", status="failure", failure_reason="user_not_found"
    )

    resp = await admin_ctx.client.get("/admin/audit/login-logs?status=failure")
    assert resp.status_code == 200, resp.text
    page = resp.json()["data"]
    assert page["total"] == 1
    assert page["items"][0]["failure_reason"] == "user_not_found"


async def test_oper_logs_requires_permission(admin_ctx: AdminCtx) -> None:
    admin_ctx.state.perms = {"system:user:list"}  # lacks system:operlog:list
    resp = await admin_ctx.client.get("/admin/audit/oper-logs")
    assert resp.status_code == 403


async def test_login_logs_requires_permission(admin_ctx: AdminCtx) -> None:
    admin_ctx.state.perms = {"system:user:list"}  # lacks system:loginlog:list
    resp = await admin_ctx.client.get("/admin/audit/login-logs")
    assert resp.status_code == 403


async def test_oper_logs_invalid_sort_rejected(admin_ctx: AdminCtx) -> None:
    resp = await admin_ctx.client.get("/admin/audit/oper-logs?sort_by=evil")
    assert resp.status_code == 400
    assert resp.json()["code"] == "request.invalid"
