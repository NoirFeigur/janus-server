"""Tests for the operation-audit middleware (admin write 留痕).

Two layers:
- Pure unit tests for the inclusion predicate + (module, action, target) rule
  table — no DB, fast, branch-focused.
- End-to-end tests through the real app (``admin_ctx``) proving a write produces
  exactly one ``oper_log`` row with the right actor/result, that reads are not
  audited, and that an audit-append failure is swallowed (non-blocking).
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from src.admin.audit import middleware as audit_mw
from src.admin.audit.middleware import classify, should_audit
from src.db.models.audit import OperLog
from tests.admin.conftest import ADMIN_ID, AdminCtx

# --- pure: inclusion predicate ------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path", "expected"),
    [
        ("POST", "/admin/roles", True),
        ("PUT", "/admin/menus/55", True),
        ("PATCH", "/admin/users/7", True),
        ("DELETE", "/admin/departments/9", True),
        ("POST", "/admin/roles/batch-delete", True),
        ("GET", "/admin/roles", False),  # reads are never audited
        ("HEAD", "/admin/roles", False),
        ("OPTIONS", "/admin/roles", False),
        ("POST", "/auth/login", False),  # login handled by C5 login-log
        ("POST", "/auth/logout", False),
        ("DELETE", "/health/ready", False),
    ],
)
def test_should_audit(method: str, path: str, expected: bool) -> None:
    assert should_audit(method, path) is expected


# --- pure: module / action / target derivation --------------------------------


@pytest.mark.parametrize(
    ("method", "path", "expected"),
    [
        ("POST", "/admin/roles", ("role", "create", None)),
        ("POST", "/admin/roles/batch-delete", ("role", "batch_delete", None)),
        ("PUT", "/admin/menus/55", ("menu", "update", "55")),
        ("PATCH", "/admin/users/7", ("user", "update", "7")),
        ("DELETE", "/admin/departments/9", ("dept", "delete", "9")),
        ("DELETE", "/admin/users/42", ("user", "delete", "42")),
        # named item action: the verb wins over the method (a POST reset-password
        # must NOT be mislabelled "create"); hyphens normalize to underscores.
        ("POST", "/admin/users/42/reset-password", ("user", "reset_password", "42")),
        # sub-resource with an opaque (non-numeric) trailing id: method-classified,
        # target_id stays NULL (a session jti is not a numeric row id).
        ("DELETE", "/admin/online/sessions/abc123", ("online", "delete", None)),
        # unknown resource falls back to the raw segment (stays visible, not dropped)
        ("POST", "/admin/widgets", ("widgets", "create", None)),
    ],
)
def test_classify(
    method: str, path: str, expected: tuple[str, str, str | None]
) -> None:
    assert classify(method, path) == expected


# --- end-to-end ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_successful_write_records_oper_log(admin_ctx: AdminCtx) -> None:
    resp = await admin_ctx.client.post(
        "/admin/roles", json={"name": "Audited", "code": "audited"}
    )
    assert resp.status_code == 200, resp.text

    rows = (await admin_ctx.session.scalars(select(OperLog))).all()
    assert len(rows) == 1
    row = rows[0]
    assert row.module == "role"
    assert row.action == "create"
    assert row.status == "success"
    assert row.error_code is None
    assert row.actor_id == ADMIN_ID
    assert row.actor_name == "admin"
    assert row.method == "POST"
    assert row.path.endswith("/admin/roles")
    assert row.before_value is None
    assert row.after_value is None
    assert row.latency_ms is not None and row.latency_ms >= 0


@pytest.mark.asyncio
async def test_forbidden_write_records_failure(admin_ctx: AdminCtx) -> None:
    admin_ctx.state.perms = {"system:user:list"}  # lacks system:role:add
    resp = await admin_ctx.client.post(
        "/admin/roles", json={"name": "X", "code": "x"}
    )
    assert resp.status_code == 403

    rows = (await admin_ctx.session.scalars(select(OperLog))).all()
    assert len(rows) == 1
    row = rows[0]
    assert row.module == "role"
    assert row.action == "create"
    assert row.status == "failure"
    assert row.error_code == "auth.forbidden"


@pytest.mark.asyncio
async def test_validation_failure_records_failure(admin_ctx: AdminCtx) -> None:
    resp = await admin_ctx.client.post(
        "/admin/roles", json={"name": "X", "code": "x", "data_scope": "bogus"}
    )
    assert resp.status_code == 422

    rows = (await admin_ctx.session.scalars(select(OperLog))).all()
    assert len(rows) == 1
    assert rows[0].status == "failure"
    assert rows[0].error_code == "request.invalid"


@pytest.mark.asyncio
async def test_read_is_not_audited(admin_ctx: AdminCtx) -> None:
    resp = await admin_ctx.client.get("/admin/roles")
    assert resp.status_code == 200

    rows = (await admin_ctx.session.scalars(select(OperLog))).all()
    assert rows == []


@pytest.mark.asyncio
async def test_audit_append_failure_is_swallowed(
    admin_ctx: AdminCtx, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _boom(self: object, row: object) -> object:  # noqa: ANN401
        raise RuntimeError("audit backend down")

    monkeypatch.setattr(audit_mw.AuditRepository, "append_oper_log", _boom)

    # The user's write must still succeed even though audit append blows up.
    resp = await admin_ctx.client.post(
        "/admin/roles", json={"name": "Resilient", "code": "resilient"}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["code"] == "resilient"
