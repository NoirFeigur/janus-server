"""Route-level tests for the admin meta endpoints (i18n enum码表 + 422 翻译).

Drives the real app through ``admin_ctx``. Covers:
- ``GET /admin/meta/enums`` returns the success envelope with the enum码表 for
  the request locale (default zh-CN, and en-US via ``?lang``);
- the endpoint only requires authentication (no specific permission);
- the 422 validation handler now returns **backend-translated** ``msg`` per the
  request locale (架构决策 6.12.3), with ``ctx`` interpolated.
"""

from __future__ import annotations

import pytest

from tests.admin.conftest import AdminCtx

pytestmark = pytest.mark.asyncio


async def test_enums_default_locale_zh(admin_ctx: AdminCtx) -> None:
    resp = await admin_ctx.client.get("/admin/meta/enums")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["success"] is True
    data = body["data"]
    assert data["enum.userStatus.active"] == "启用"
    assert data["enum.apiKeyStatus.disabled"] == "禁用"
    # All keys are enum.* (prefix filter held).
    assert all(k.startswith("enum.") for k in data)


async def test_enums_english_via_lang_query(admin_ctx: AdminCtx) -> None:
    resp = await admin_ctx.client.get("/admin/meta/enums?lang=en-US")
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["enum.userStatus.active"] == "Active"
    assert data["enum.apiKeyStatus.disabled"] == "Disabled"


async def test_enums_only_requires_authentication(admin_ctx: AdminCtx) -> None:
    # Strip all perms: enum码表 is universal reference data, not perm-gated.
    admin_ctx.state.perms = set()
    resp = await admin_ctx.client.get("/admin/meta/enums")
    assert resp.status_code == 200, resp.text


async def test_validation_msg_translated_zh(admin_ctx: AdminCtx) -> None:
    # POST /admin/config with a missing required field → 422, zh-CN msg.
    resp = await admin_ctx.client.post("/admin/config", json={})
    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert body["success"] is False
    assert body["code"] == "request.invalid"
    msgs = {e["field"]: e["msg"] for e in body["errors"]}
    # config_key is required → translated "该字段为必填项", not Pydantic's English.
    assert msgs["config_key"] == "该字段为必填项"


async def test_validation_msg_translated_en(admin_ctx: AdminCtx) -> None:
    resp = await admin_ctx.client.post("/admin/config?lang=en-US", json={})
    assert resp.status_code == 422, resp.text
    msgs = {e["field"]: e["msg"] for e in resp.json()["errors"]}
    assert msgs["config_key"] == "This field is required"
