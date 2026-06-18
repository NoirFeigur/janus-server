"""Route-level tests for admin platform-config CRUD (Wave D).

Drives the real app through ``admin_ctx`` (super-admin actor by default). Covers
the full CRUD lifecycle plus the rules the service enforces beyond the DB:
duplicate-key rejection, value-must-parse-under-type validation, builtin rows
being undeletable, and per-route ``system:config:*`` permission gating.
"""

from __future__ import annotations

import pytest

from tests.admin.conftest import AdminCtx

pytestmark = pytest.mark.asyncio


def _payload(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "config_key": "auth.login_max_failures",
        "config_value": "5",
        "value_type": "int",
        "config_name": "登录最大失败次数",
        "is_builtin": False,
    }
    base.update(overrides)
    return base


# ---- create ----------------------------------------------------------------


async def test_create_config_succeeds(admin_ctx: AdminCtx) -> None:
    resp = await admin_ctx.client.post("/admin/config", json=_payload())
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["config_key"] == "auth.login_max_failures"
    assert data["value_type"] == "int"
    assert isinstance(data["id"], str)  # snowflake serialized as string


async def test_create_rejects_duplicate_key(admin_ctx: AdminCtx) -> None:
    first = await admin_ctx.client.post("/admin/config", json=_payload())
    assert first.status_code == 200, first.text
    dup = await admin_ctx.client.post("/admin/config", json=_payload())
    assert dup.status_code == 400
    assert dup.json()["code"] == "request.invalid"


async def test_create_rejects_value_not_parsing_under_type(admin_ctx: AdminCtx) -> None:
    resp = await admin_ctx.client.post(
        "/admin/config",
        json=_payload(config_value="not-a-number", value_type="int"),
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == "request.invalid"


async def test_create_requires_add_perm(admin_ctx: AdminCtx) -> None:
    admin_ctx.state.perms = {"system:config:list"}  # 缺 system:config:add
    resp = await admin_ctx.client.post("/admin/config", json=_payload())
    assert resp.status_code == 403
    assert resp.json()["code"] == "auth.forbidden"


# ---- list / get ------------------------------------------------------------


async def test_list_configs_paged(admin_ctx: AdminCtx) -> None:
    await admin_ctx.client.post("/admin/config", json=_payload())
    await admin_ctx.client.post(
        "/admin/config",
        json=_payload(config_key="app.name", config_value="janus", value_type="string"),
    )
    resp = await admin_ctx.client.get("/admin/config")
    assert resp.status_code == 200, resp.text
    body = resp.json()["data"]
    assert body["total"] == 2
    keys = {row["config_key"] for row in body["items"]}
    assert keys == {"auth.login_max_failures", "app.name"}


async def test_list_keyword_filter(admin_ctx: AdminCtx) -> None:
    await admin_ctx.client.post("/admin/config", json=_payload())
    await admin_ctx.client.post(
        "/admin/config",
        json=_payload(config_key="app.name", config_value="janus", value_type="string"),
    )
    resp = await admin_ctx.client.get("/admin/config", params={"keyword": "app"})
    rows = resp.json()["data"]["items"]
    assert len(rows) == 1
    assert rows[0]["config_key"] == "app.name"


async def test_get_config_by_id(admin_ctx: AdminCtx) -> None:
    created = await admin_ctx.client.post("/admin/config", json=_payload())
    config_id = created.json()["data"]["id"]
    resp = await admin_ctx.client.get(f"/admin/config/{config_id}")
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["config_key"] == "auth.login_max_failures"


async def test_get_missing_returns_404(admin_ctx: AdminCtx) -> None:
    resp = await admin_ctx.client.get("/admin/config/999999")
    assert resp.status_code == 404
    assert resp.json()["code"] == "request.invalid"


async def test_list_requires_perm(admin_ctx: AdminCtx) -> None:
    admin_ctx.state.perms = {"system:user:list"}
    resp = await admin_ctx.client.get("/admin/config")
    assert resp.status_code == 403


# ---- update ----------------------------------------------------------------


async def test_update_value_succeeds(admin_ctx: AdminCtx) -> None:
    created = await admin_ctx.client.post("/admin/config", json=_payload())
    config_id = created.json()["data"]["id"]
    resp = await admin_ctx.client.put(
        f"/admin/config/{config_id}", json={"config_value": "10"}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["config_value"] == "10"


async def test_update_rejects_value_not_parsing(admin_ctx: AdminCtx) -> None:
    created = await admin_ctx.client.post("/admin/config", json=_payload())
    config_id = created.json()["data"]["id"]
    resp = await admin_ctx.client.put(
        f"/admin/config/{config_id}", json={"config_value": "abc"}
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == "request.invalid"


async def test_update_type_change_revalidates_existing_value(admin_ctx: AdminCtx) -> None:
    """Changing value_type alone must re-check the stored value against the new type."""
    created = await admin_ctx.client.post(
        "/admin/config",
        json=_payload(config_key="x.y", config_value="hello", value_type="string"),
    )
    config_id = created.json()["data"]["id"]
    # "hello" cannot parse as int → type-only change must be rejected.
    resp = await admin_ctx.client.put(
        f"/admin/config/{config_id}", json={"value_type": "int"}
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == "request.invalid"


async def test_update_requires_edit_perm(admin_ctx: AdminCtx) -> None:
    created = await admin_ctx.client.post("/admin/config", json=_payload())
    config_id = created.json()["data"]["id"]
    admin_ctx.state.perms = {"system:config:list"}
    resp = await admin_ctx.client.put(
        f"/admin/config/{config_id}", json={"config_value": "10"}
    )
    assert resp.status_code == 403


# ---- delete ----------------------------------------------------------------


async def test_delete_non_builtin_succeeds(admin_ctx: AdminCtx) -> None:
    created = await admin_ctx.client.post("/admin/config", json=_payload())
    config_id = created.json()["data"]["id"]
    resp = await admin_ctx.client.delete(f"/admin/config/{config_id}")
    assert resp.status_code == 200, resp.text
    # Soft-deleted → no longer fetchable.
    follow = await admin_ctx.client.get(f"/admin/config/{config_id}")
    assert follow.status_code == 404


async def test_delete_builtin_is_rejected(admin_ctx: AdminCtx) -> None:
    created = await admin_ctx.client.post(
        "/admin/config", json=_payload(is_builtin=True)
    )
    config_id = created.json()["data"]["id"]
    resp = await admin_ctx.client.delete(f"/admin/config/{config_id}")
    assert resp.status_code == 400
    assert resp.json()["code"] == "request.invalid"


async def test_delete_requires_remove_perm(admin_ctx: AdminCtx) -> None:
    created = await admin_ctx.client.post("/admin/config", json=_payload())
    config_id = created.json()["data"]["id"]
    admin_ctx.state.perms = {"system:config:list"}
    resp = await admin_ctx.client.delete(f"/admin/config/{config_id}")
    assert resp.status_code == 403
