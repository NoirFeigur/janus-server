"""异常体系与全局拦截器。

所有管理面错误响应统一走 :func:`error_envelope`,与成功响应(:mod:`src.responses`)
**同构**——都携带 ``code`` 与 ``trace_id``,遵循 i18n 纯 code 路线(不发 message)。

四个拦截器(覆盖全部错误来源):
- :class:`AppError`            —— 应用级异常,显式抛出,带 ErrorCode + HTTP 状态码。
- ``RequestValidationError``   —— 入参校验失败(FastAPI),附 ``errors`` 字段明细。
- ``StarletteHTTPException``   —— 框架 HTTP 异常(404/405 等),按状态码映射 code。
- ``Exception``                —— 兜底,防未捕获异常泄漏堆栈(仅 debug 附 detail)。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError
from starlette import status
from starlette.exceptions import HTTPException as StarletteHTTPException

from src.config import get_settings
from src.core.i18n import I18n, get_i18n, get_locale
from src.core.logging import get_logger
from src.enums import ErrorCode
from src.responses import error_body

_log = get_logger(__name__)

# HTTP 状态码 → ErrorCode 映射(框架 HTTP 异常用,无精确匹配时回落)。
_STATUS_TO_CODE: dict[int, ErrorCode] = {
    status.HTTP_401_UNAUTHORIZED: ErrorCode.auth_invalid_token,  # 缺失/无效凭据。
    status.HTTP_403_FORBIDDEN: ErrorCode.auth_forbidden,  # 已认证但无权限。
    status.HTTP_404_NOT_FOUND: ErrorCode.request_invalid,  # 路由/资源不存在。
    status.HTTP_405_METHOD_NOT_ALLOWED: ErrorCode.request_invalid,  # 方法不允许。
}


class AppError(Exception):
    """应用级异常:由 service/router 显式抛出,携带机器可读 code 与 HTTP 状态码。"""

    def __init__(
        self,
        code: ErrorCode,
        status_code: int = status.HTTP_400_BAD_REQUEST,
        params: Mapping[str, Any] | None = None,
    ) -> None:
        self.code = code
        self.status_code = status_code
        self.params = dict(params or {})
        super().__init__(code.value)


def _trace_id(request: Request) -> str:
    """取 TraceIdMiddleware 注入的 trace_id;缺失则即兴生成(防御性)。"""
    return getattr(request.state, "trace_id", str(uuid4()))


def error_envelope(
    request: Request,
    *,
    code: ErrorCode,
    status_code: int,
    params: Mapping[str, Any] | None = None,
    errors: list[dict[str, Any]] | None = None,
) -> JSONResponse:
    """构造统一错误信封响应(与成功响应同构)。"""
    # 暂存 code 供操作审计中间件读取(失败留痕的 error_code 来源);中间件在
    # call_next 返回后从 request.state 取,避免回读已流式化的响应体。
    request.state.error_code = code.value
    body = error_body(
        code=code,
        trace_id=_trace_id(request),
        params=dict(params or {}),
        errors=errors,
    )
    return JSONResponse(status_code=status_code, content=body)


async def app_error_handler(request: Request, exc: Exception) -> JSONResponse:
    # Starlette types the handler's second arg as ``Exception``; narrow to the
    # registered type (the dispatcher only routes AppError here).
    assert isinstance(exc, AppError)
    return error_envelope(
        request,
        code=exc.code,
        status_code=exc.status_code,
        params=exc.params,
    )


async def validation_error_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)
    # 入参校验文案后端翻译(6.12.3):Pydantic 原始 ``msg`` 恒为英文,zh-CN 用户
    # 会看到生硬英文字段错误。按 ``validation.{type}`` 取本地化模板并用 Pydantic
    # 的 ``ctx``(如 min_length/gt)插值;词条缺失则回退原始 ``msg``——保证新出现
    # 的错误类型不会变成裸 key,而是仍有可读(英文)文案。
    i18n = get_i18n()
    locale = get_locale()
    errors = [
        {
            "field": ".".join(str(part) for part in error["loc"][1:]),
            "type": error["type"],
            "msg": _translate_validation(i18n, locale, error),
        }
        for error in exc.errors()
    ]
    return error_envelope(
        request,
        code=ErrorCode.request_invalid,
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        errors=errors,
    )


def _translate_validation(i18n: I18n, locale: str, error: Mapping[str, Any]) -> str:
    """取 ``validation.{type}`` 本地化模板并用 ``ctx`` 插值;缺词条回退原 ``msg``。"""
    key = f"validation.{error['type']}"
    ctx = error.get("ctx") or {}
    translated = i18n.t(key, locale, **ctx)
    # 未命中:``t`` 原样返回 key —— 退回 Pydantic 原始(英文)msg,绝不外泄裸 key。
    if translated == key:
        return str(error["msg"])
    return translated


async def http_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """接管框架 HTTP 异常(404/405/401/403 等),统一走信封。"""
    assert isinstance(exc, StarletteHTTPException)
    code = _STATUS_TO_CODE.get(exc.status_code, ErrorCode.request_invalid)
    if exc.status_code >= status.HTTP_500_INTERNAL_SERVER_ERROR:
        code = ErrorCode.internal_error
    return error_envelope(request, code=code, status_code=exc.status_code)


async def integrity_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """DB 完整性约束冲突 → 稳定 409,而非裸 500。

    并发下「先查重再写入」的非原子模式会让唯一/外键约束在 flush/commit 时抛
    ``IntegrityError``;若不接管,它会冒到兜底处理器成 500(把一个客户端可纠正的
    冲突误报成服务端故障)。统一映射为 ``request.conflict`` / 409,前端按 code 提示
    「已存在/冲突」。不解析底层 DBAPI 文案(各驱动不一、可能含表/列名,不外泄)——
    只发稳定 code,具体哪个约束由后端日志(含 trace_id)排查。
    """
    assert isinstance(exc, IntegrityError)
    _log.warning(
        "db.integrity_error",
        trace_id=getattr(request.state, "trace_id", None),
        orig=str(getattr(exc, "orig", exc)),
    )
    return error_envelope(
        request,
        code=ErrorCode.request_conflict,
        status_code=status.HTTP_409_CONFLICT,
    )


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """兜底:任何未捕获异常 → 500 信封。绝不泄漏堆栈(仅 debug 附 detail 供排查)。"""
    params: dict[str, Any] = {}
    if get_settings().debug:
        params["detail"] = f"{type(exc).__name__}: {exc}"
    return error_envelope(
        request,
        code=ErrorCode.internal_error,
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        params=params,
    )


def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AppError, app_error_handler)
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    # IntegrityError before the Exception catch-all: a constraint violation is a
    # client-correctable 409, not a server 500.
    app.add_exception_handler(IntegrityError, integrity_error_handler)
    app.add_exception_handler(Exception, unhandled_exception_handler)
