from collections.abc import Mapping
from http import HTTPStatus
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette import status

from src.enums import ErrorCode


class JanusError(Exception):
    def __init__(
        self,
        code: ErrorCode,
        status_code: int = status.HTTP_400_BAD_REQUEST,
        params: Mapping[str, Any] | None = None,
        detail: str | None = None,
    ) -> None:
        self.code = code
        self.status_code = status_code
        self.params = dict(params or {})
        self.detail = detail or code.value
        super().__init__(self.detail)


def problem_response(
    request: Request,
    *,
    code: ErrorCode,
    status_code: int,
    detail: str,
    params: Mapping[str, Any] | None = None,
    errors: list[dict[str, Any]] | None = None,
) -> JSONResponse:
    trace_id = getattr(request.state, "trace_id", str(uuid4()))
    phrase = HTTPStatus(status_code).phrase
    body: dict[str, Any] = {
        "type": f"urn:janus:error:{code.value}",
        "title": phrase,
        "status": status_code,
        "code": code.value,
        "detail": detail,
        "params": dict(params or {}),
        "trace_id": trace_id,
    }
    if errors is not None:
        body["errors"] = errors
    return JSONResponse(
        status_code=status_code,
        content=body,
        media_type="application/problem+json",
    )


async def janus_error_handler(request: Request, exc: Exception) -> JSONResponse:
    # Starlette types the handler's second arg as ``Exception``; narrow to the
    # registered type (the dispatcher only routes JanusError here).
    assert isinstance(exc, JanusError)
    return problem_response(
        request,
        code=exc.code,
        status_code=exc.status_code,
        detail=exc.detail,
        params=exc.params,
    )


async def validation_error_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)
    errors = [
        {
            "field": ".".join(str(part) for part in error["loc"][1:]),
            "type": error["type"],
            "msg": error["msg"],
        }
        for error in exc.errors()
    ]
    return problem_response(
        request,
        code=ErrorCode.request_invalid,
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        detail="Request validation failed",
        errors=errors,
    )


def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(JanusError, janus_error_handler)
    app.add_exception_handler(RequestValidationError, validation_error_handler)
