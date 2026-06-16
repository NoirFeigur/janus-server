"""Tests for the unified response envelope (src/responses.py).

方案 B 契约:``success`` 布尔字段做成败判别;成功体携带 ``data`` 不含 ``code``,
错误体携带 ``code`` 不含 ``data``;两者都不含人类可读 ``message``(i18n 纯 code 路线)。
"""

from __future__ import annotations

from src.enums import ErrorCode
from src.responses import SuccessEnvelope, error_body, success


def test_success_envelope_shape() -> None:
    env = success({"id": 123, "name": "alice"}, trace_id="t-1")
    assert isinstance(env, SuccessEnvelope)
    assert env.success is True
    assert env.trace_id == "t-1"
    assert env.data == {"id": 123, "name": "alice"}


def test_success_envelope_has_no_code_or_message() -> None:
    dumped = success("payload", trace_id="t-2").model_dump()
    assert dumped["success"] is True
    assert "code" not in dumped  # 成功体不含错误码字段
    assert "message" not in dumped  # i18n 纯 code 路线
    assert {"success", "data", "trace_id"} == dumped.keys()


def test_error_body_shape() -> None:
    body = error_body(
        code=ErrorCode.auth_invalid_token,
        trace_id="t-3",
        params={"reason": "expired"},
    )
    assert body["success"] is False
    assert body["code"] == ErrorCode.auth_invalid_token.value
    assert body["trace_id"] == "t-3"
    assert body["params"] == {"reason": "expired"}
    assert "errors" not in body  # 未传 errors 时不应出现该键
    assert "message" not in body
    assert "data" not in body  # 错误体不含业务负载


def test_error_body_includes_errors_when_provided() -> None:
    errors = [{"field": "age", "type": "missing", "msg": "field required"}]
    body = error_body(code=ErrorCode.request_invalid, trace_id="t-4", errors=errors)
    assert body["success"] is False
    assert body["errors"] == errors
    assert body["params"] == {}


def test_success_field_discriminates_success_from_error() -> None:
    """success 字段是成败判别的唯一来源(TS discriminated union 的判别键)。"""
    ok = success("data", trace_id="x").model_dump()
    err = error_body(code=ErrorCode.internal_error, trace_id="x")
    assert ok["success"] is True
    assert err["success"] is False
    # 共享元数据键:两者都带 trace_id。
    assert ok["trace_id"] == err["trace_id"] == "x"
