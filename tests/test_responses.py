"""Tests for the unified response envelope (src/responses.py).

断言成功/错误信封的**同构不变式**:两者都带 ``code`` 与 ``trace_id``,且都
不含人类可读 ``message``(i18n 纯 code 路线)。
"""

from __future__ import annotations

from src.enums import ErrorCode
from src.responses import OK_CODE, Envelope, error_body, success


def test_success_envelope_shape() -> None:
    env = success({"id": 123, "name": "alice"}, trace_id="t-1")
    assert isinstance(env, Envelope)
    assert env.code == OK_CODE
    assert env.trace_id == "t-1"
    assert env.data == {"id": 123, "name": "alice"}
    assert env.params == {}
    assert env.errors is None


def test_success_envelope_has_no_message_field() -> None:
    env = success("payload", trace_id="t-2")
    dumped = env.model_dump()
    assert "message" not in dumped
    assert {"code", "trace_id", "data"} <= dumped.keys()


def test_error_body_shape() -> None:
    body = error_body(
        code=ErrorCode.auth_invalid_token,
        trace_id="t-3",
        params={"reason": "expired"},
    )
    assert body["code"] == ErrorCode.auth_invalid_token.value
    assert body["trace_id"] == "t-3"
    assert body["params"] == {"reason": "expired"}
    assert "errors" not in body  # 未传 errors 时不应出现该键
    assert "message" not in body


def test_error_body_includes_errors_when_provided() -> None:
    errors = [{"field": "age", "type": "missing", "msg": "field required"}]
    body = error_body(
        code=ErrorCode.request_invalid,
        trace_id="t-4",
        errors=errors,
    )
    assert body["errors"] == errors
    assert body["params"] == {}


def test_success_and_error_are_isomorphic_on_shared_keys() -> None:
    """成功与错误信封在共享键(code/trace_id/params)上结构一致。"""
    ok = success("data", trace_id="x").model_dump()
    err = error_body(code=ErrorCode.internal_error, trace_id="x")
    for shared in ("code", "trace_id", "params"):
        assert shared in ok
        assert shared in err
    # 区分点:成功 code == "ok",错误 code 属于 ErrorCode 命名空间。
    assert ok["code"] == OK_CODE
    assert err["code"] != OK_CODE
