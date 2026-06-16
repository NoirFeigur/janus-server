"""Tests for exception handlers (src/exceptions.py).

Builds a minimal FastAPI app, registers all four handlers, and drives every
error source through TestClient to assert the unified envelope contract
(成功/错误同构,只含 code + trace_id,无 message) and the safety bottom-line
(未捕获异常不泄漏堆栈)。
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel

from src.enums import ErrorCode
from src.exceptions import AppError, register_exception_handlers


class _Payload(BaseModel):
    name: str
    age: int


def _build_app() -> FastAPI:
    app = FastAPI()
    register_exception_handlers(app)

    @app.get("/boom")
    async def boom() -> None:
        raise AppError(
            code=ErrorCode.auth_forbidden,
            status_code=409,
            params={"resource": "user"},
        )

    @app.post("/validate")
    async def validate(_: _Payload) -> dict[str, str]:
        return {"ok": "yes"}

    @app.get("/crash")
    async def crash() -> None:
        raise RuntimeError("super secret internal detail leak")

    return app


@pytest.fixture
def client() -> TestClient:
    return TestClient(_build_app())


def test_app_error_returns_envelope(client: TestClient) -> None:
    resp = client.get("/boom")
    assert resp.status_code == 409
    body = resp.json()
    assert body["code"] == ErrorCode.auth_forbidden.value
    assert body["params"] == {"resource": "user"}
    assert "trace_id" in body
    # i18n 纯 code 路线:绝不发人类可读 message。
    assert "message" not in body


def test_validation_error_returns_422_envelope_with_field_errors(client: TestClient) -> None:
    resp = client.post("/validate", json={"name": "x"})  # missing 'age'
    assert resp.status_code == 422
    body = resp.json()
    assert body["code"] == ErrorCode.request_invalid.value
    assert "message" not in body
    assert isinstance(body["errors"], list)
    fields = {e["field"] for e in body["errors"]}
    assert "age" in fields
    for err in body["errors"]:
        assert {"field", "type", "msg"} <= err.keys()


def test_404_routes_through_envelope(client: TestClient) -> None:
    resp = client.get("/this-route-does-not-exist")
    assert resp.status_code == 404
    body = resp.json()
    assert body["code"] == ErrorCode.request_invalid.value
    assert "trace_id" in body
    # FastAPI 默认会返回 {"detail":"Not Found"};确认已被信封接管。
    assert "detail" not in body


def test_405_method_not_allowed_through_envelope(client: TestClient) -> None:
    resp = client.post("/boom")  # /boom is GET-only
    assert resp.status_code == 405
    body = resp.json()
    assert body["code"] == ErrorCode.request_invalid.value
    assert "trace_id" in body


def test_unhandled_exception_returns_500_without_stack_leak() -> None:
    # raise_server_exceptions=False → 让兜底处理器真正产出 500 响应体。
    client = TestClient(_build_app(), raise_server_exceptions=False)
    resp = client.get("/crash")
    assert resp.status_code == 500
    body = resp.json()
    assert body["code"] == ErrorCode.internal_error.value
    assert "trace_id" in body
    # 关键安全断言:debug=False(默认)时,绝不把异常细节/堆栈塞进响应。
    serialized = resp.text
    assert "super secret internal detail leak" not in serialized
    assert "Traceback" not in serialized
    assert body["params"] == {}
