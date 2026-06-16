"""Tests for exception handlers (src/exceptions.py).

Builds a minimal FastAPI app, registers the handlers, and drives both error
paths through TestClient to assert the RFC-7807 problem+json contract and the
runtime type-narrowing (the handlers widen their signature to ``Exception`` for
Starlette compatibility, then narrow internally).
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel

from src.enums import ErrorCode
from src.exceptions import JanusError, register_exception_handlers


class _Payload(BaseModel):
    name: str
    age: int


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    register_exception_handlers(app)

    @app.get("/boom")
    async def boom() -> None:
        raise JanusError(
            code=ErrorCode.request_invalid,
            status_code=409,
            params={"resource": "user"},
            detail="conflict happened",
        )

    @app.post("/validate")
    async def validate(_: _Payload) -> dict[str, str]:
        return {"ok": "yes"}

    return TestClient(app)


def test_janus_error_returns_problem_json(client: TestClient) -> None:
    resp = client.get("/boom")
    assert resp.status_code == 409
    assert resp.headers["content-type"].startswith("application/problem+json")
    body = resp.json()
    assert body["code"] == ErrorCode.request_invalid.value
    assert body["status"] == 409
    assert body["detail"] == "conflict happened"
    assert body["params"] == {"resource": "user"}
    assert body["type"] == f"urn:janus:error:{ErrorCode.request_invalid.value}"
    assert "trace_id" in body


def test_validation_error_returns_422_with_field_errors(client: TestClient) -> None:
    resp = client.post("/validate", json={"name": "x"})  # missing 'age'
    assert resp.status_code == 422
    assert resp.headers["content-type"].startswith("application/problem+json")
    body = resp.json()
    assert body["code"] == ErrorCode.request_invalid.value
    assert body["status"] == 422
    assert isinstance(body["errors"], list)
    fields = {e["field"] for e in body["errors"]}
    assert "age" in fields
    for err in body["errors"]:
        assert {"field", "type", "msg"} <= err.keys()
