"""Unified response envelope (管理面 auth/admin 专用).

方案 B:用独立的 ``success`` 布尔字段做成败判别,成功/错误**结构分支**:

- 成功 (2xx):``{"success": true, "data": <T>, "trace_id": "..."}``
- 错误 (4xx/5xx):``{"success": false, "code": "auth.invalid_token", "params": {...},
  "trace_id": "...", "errors": [...]?}``

设计依据:
- ``success`` 判别字段让前端 TS discriminated union 收窄干净,不依赖解析 code。
- 错误码字段名为 ``code``(非 ``error_code``)——对齐 JSON:API/Stripe/GitHub
  错误对象惯例;``success: false`` 已提供"这是错误"的语境,``error_`` 前缀冗余。
- 遵循 i18n 纯 code 路线:只发机器可读 ``code`` + ``params``(前端插值),**不发 message**。
- HTTP 状态码语义保留(成功 2xx / 错误 4xx/5xx),前端按状态码分流,body 仅承载细节。

网关面(OpenAI/Anthropic 兼容)**不使用**本信封——它透传上游原生协议。
"""

from __future__ import annotations

from typing import Any, Generic, TypeVar

from pydantic import BaseModel

from src.enums import ErrorCode

T = TypeVar("T")


class SuccessEnvelope(BaseModel, Generic[T]):
    """成功响应信封。供管理面端点作为 response_model 返回。"""

    success: bool = True  # 判别字段,成功恒为 True。
    data: T | None = None  # 业务负载。
    trace_id: str  # 请求链路 ID,与 X-Request-ID 响应头一致。


def success(data: T, *, trace_id: str) -> SuccessEnvelope[T]:
    """构造成功信封。"""
    return SuccessEnvelope[T](success=True, data=data, trace_id=trace_id)


def error_body(
    *,
    code: ErrorCode,
    trace_id: str,
    params: dict[str, Any] | None = None,
    errors: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """构造错误信封 dict。供异常处理器塞进 JSONResponse(HTTP 状态码单独传)。"""
    body: dict[str, Any] = {
        "success": False,
        "code": code.value,
        "trace_id": trace_id,
        "params": dict(params or {}),
    }
    if errors is not None:
        body["errors"] = errors
    return body
