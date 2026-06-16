"""Unified response envelope (管理面 auth/admin 专用).

成功与错误**同构**:两者都携带 ``code`` 与 ``trace_id``。遵循 i18n 纯 code 路线
——后端只发机器可读的 ``code`` 与可选 ``params``(供前端 i18n 插值),**绝不**在
信封里放面向人类的 ``message``。

- 成功:``{"code": "ok", "data": <T>, "trace_id": "..."}``
- 错误:``{"code": "auth.invalid_token", "params": {...}, "trace_id": "...", "errors": [...]?}``

网关面(OpenAI/Anthropic 兼容)**不使用**本信封——它透传上游原生协议。
"""

from __future__ import annotations

from typing import Any, Generic, TypeVar

from pydantic import BaseModel, Field

from src.enums import ErrorCode

T = TypeVar("T")

OK_CODE = "ok"  # 成功响应的固定 code,与 ErrorCode 命名空间互不重叠。


class Envelope(BaseModel, Generic[T]):
    """统一返回信封。成功/错误同构,字段按场景填充。"""

    code: str  # "ok" 表示成功;否则为 ErrorCode 值(如 "auth.invalid_token")。
    trace_id: str  # 请求链路 ID,与 X-Request-ID 响应头一致。
    data: T | None = None  # 成功时的业务负载;错误时为 None。
    params: dict[str, Any] = Field(default_factory=dict)  # 前端 i18n 插值参数(错误时常用)。
    errors: list[dict[str, Any]] | None = None  # 字段级校验错误明细(仅入参校验失败时)。


def success(data: T, *, trace_id: str) -> Envelope[T]:
    """构造成功信封。供管理面端点作为 response_model 返回。"""
    return Envelope[T](code=OK_CODE, trace_id=trace_id, data=data)


def error_body(
    *,
    code: ErrorCode,
    trace_id: str,
    params: dict[str, Any] | None = None,
    errors: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """构造错误信封 dict。供异常处理器塞进 JSONResponse(状态码单独传)。"""
    body: dict[str, Any] = {
        "code": code.value,
        "trace_id": trace_id,
        "params": dict(params or {}),
    }
    if errors is not None:
        body["errors"] = errors
    return body
