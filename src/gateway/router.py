from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Mapping
from time import monotonic
from typing import Annotated, Any, TypeVar
from uuid import uuid4

import litellm
from fastapi import APIRouter, Depends, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ValidationError
from starlette import status

from src.auth.dependencies import CurrentUser
from src.auth.service import AuthenticatedUser
from src.db.models.model_catalog import LogicalModel
from src.enums import ErrorCode, UsageStatus
from src.exceptions import AppError
from src.gateway.dependencies import get_gateway_service
from src.gateway.quota import QuotaCheckResult
from src.gateway.router_manager import RouterManager
from src.gateway.schemas import AnthropicRequest, GeminiRequest, OpenAIRequest
from src.gateway.service import GatewayService
from src.gateway.usage import UsageData, record_usage

router = APIRouter(tags=["gateway"])

GatewayServiceDep = Annotated[GatewayService, Depends(get_gateway_service)]
RequestSchemaT = TypeVar("RequestSchemaT", bound=BaseModel)


@router.post("/v1/chat/completions", response_model=None)
async def openai_chat_completions(
    request: Request,
    user: CurrentUser,
    service: GatewayServiceDep,
) -> JSONResponse | StreamingResponse:
    payload = await _parse_body(request, OpenAIRequest)
    params = payload.model_dump(exclude={"model", "messages", "stream"})
    return await _execute_completion(
        request=request,
        user=user,
        service=service,
        requested_model=payload.model,
        messages=payload.messages,
        stream=payload.stream,
        params=params,
    )


@router.post("/v1/messages", response_model=None)
async def anthropic_messages(
    request: Request,
    user: CurrentUser,
    service: GatewayServiceDep,
) -> JSONResponse | StreamingResponse:
    payload = await _parse_body(request, AnthropicRequest)
    params = payload.model_dump(exclude={"model", "messages", "stream", "system"})
    messages = _anthropic_to_openai_messages(payload.messages, params.pop("system", None))
    response = await _execute_completion(
        request=request,
        user=user,
        service=service,
        requested_model=payload.model,
        messages=messages,
        stream=payload.stream,
        params=params,
    )
    if isinstance(response, StreamingResponse):
        return response
    return JSONResponse(content=_openai_to_anthropic_response(response.body))


@router.post("/v1beta/models/{model_name}:generateContent", response_model=None)
@router.post("/v1beta/models/{model_name}:streamGenerateContent", response_model=None)
async def gemini_generate_content(
    model_name: str,
    request: Request,
    user: CurrentUser,
    service: GatewayServiceDep,
) -> JSONResponse | StreamingResponse:
    payload = await _parse_body(request, GeminiRequest)
    params = payload.model_dump(exclude={"contents", "system_instruction"})
    messages = _gemini_to_openai_messages(
        payload.contents,
        payload.system_instruction,
    )
    stream = request.url.path.endswith(":streamGenerateContent")
    response = await _execute_completion(
        request=request,
        user=user,
        service=service,
        requested_model=model_name,
        messages=messages,
        stream=stream,
        params=params,
    )
    if isinstance(response, StreamingResponse):
        return response
    return JSONResponse(content=_openai_to_gemini_response(response.body))


async def _parse_body(request: Request, schema: type[RequestSchemaT]) -> RequestSchemaT:
    try:
        return schema.model_validate(await request.json())
    except (ValidationError, ValueError) as exc:
        raise AppError(
            ErrorCode.request_invalid,
            status.HTTP_422_UNPROCESSABLE_ENTITY,
        ) from exc


async def _execute_completion(
    *,
    request: Request,
    user: AuthenticatedUser,
    service: GatewayService,
    requested_model: str | None,
    messages: list[Any],
    stream: bool,
    params: dict[str, Any],
) -> JSONResponse | StreamingResponse:
    request_id = _request_id(request)
    logical_model = await service.resolve_model(user, requested_model)
    quota_result = await service.check_quota(user.user_id, logical_model.id)
    started_at = monotonic()
    llm_router = RouterManager.get_router()
    if stream:
        params.setdefault("stream_options", {"include_usage": True})
    try:
        response = await llm_router.acompletion(
            model=logical_model.name,
            messages=messages,
            stream=stream,
            **params,
        )
    except Exception as exc:
        await _compensate_quota(service, user.user_id, logical_model.id)
        _record_usage_task(
            user=user,
            logical_model=logical_model,
            usage=_empty_usage(),
            status_value=_usage_status_for_exception(exc),
            latency_ms=_latency_ms(started_at),
            request_id=request_id,
        )
        raise _upstream_error(exc) from exc

    if stream:
        return StreamingResponse(
            _stream_openai(
                response=response,
                user=user,
                logical_model=logical_model,
                service=service,
                started_at=started_at,
                request_id=request_id,
            ),
            media_type="text/event-stream",
        )

    usage = _extract_usage(response)
    _record_usage_task(
        user=user,
        logical_model=logical_model,
        usage=usage,
        status_value=UsageStatus.success.value,
        latency_ms=_latency_ms(started_at),
        request_id=request_id,
    )
    return JSONResponse(
        content=jsonable_encoder(_dump_model(response)),
        headers=_quota_headers(quota_result),
    )


async def _stream_openai(
    *,
    response: Any,
    user: AuthenticatedUser,
    logical_model: LogicalModel,
    service: GatewayService,
    started_at: float,
    request_id: str,
) -> AsyncIterator[str]:
    usage = _empty_usage()
    try:
        async for chunk in response:
            chunk_usage = _extract_usage(chunk)
            if chunk_usage["total_tokens"]:
                usage = chunk_usage
            yield f"data: {_dump_json(chunk)}\n\n"
        yield "data: [DONE]\n\n"
    except Exception as exc:
        await _compensate_quota(service, user.user_id, logical_model.id)
        _record_usage_task(
            user=user,
            logical_model=logical_model,
            usage=_empty_usage(),
            status_value=_usage_status_for_exception(exc),
            latency_ms=_latency_ms(started_at),
            request_id=request_id,
        )
        raise _upstream_error(exc) from exc
    if usage["total_tokens"]:
        _record_usage_task(
            user=user,
            logical_model=logical_model,
            usage=usage,
            status_value=UsageStatus.success.value,
            latency_ms=_latency_ms(started_at),
            request_id=request_id,
        )


async def _compensate_quota(service: GatewayService, user_id: int, logical_model_id: int) -> None:
    quotas = await service.repo.get_active_quotas(user_id, logical_model_id)
    await service.quota.compensate(user_id, logical_model_id, quotas)


def _record_usage_task(
    *,
    user: AuthenticatedUser,
    logical_model: LogicalModel,
    usage: dict[str, int],
    status_value: str,
    latency_ms: int | None,
    request_id: str,
) -> None:
    asyncio.create_task(
        record_usage(
            UsageData(
                user_id=user.user_id,
                api_key_id=user.api_key_id,
                logical_model=logical_model,
                channel_id=None,
                upstream_model=None,
                prompt_tokens=usage["prompt_tokens"],
                completion_tokens=usage["completion_tokens"],
                total_tokens=usage["total_tokens"],
                status=status_value,
                latency_ms=latency_ms,
                request_id=request_id,
                downgraded_features=None,
            )
        )
    )


def _extract_usage(response: Any) -> dict[str, int]:
    usage = getattr(response, "usage", None)
    if usage is None and isinstance(response, Mapping):
        usage = response.get("usage")
    if usage is None:
        return _empty_usage()
    return {
        "prompt_tokens": _usage_value(usage, "prompt_tokens"),
        "completion_tokens": _usage_value(usage, "completion_tokens"),
        "total_tokens": _usage_value(usage, "total_tokens"),
    }


def _usage_value(usage: Any, key: str) -> int:
    value = usage.get(key, 0) if isinstance(usage, Mapping) else getattr(usage, key, 0)
    return int(value or 0)


def _empty_usage() -> dict[str, int]:
    return {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}


def _upstream_error(exc: Exception) -> AppError:
    if _is_litellm_exception(exc, "RateLimitError"):
        return AppError(ErrorCode.upstream_rate_limited, status.HTTP_429_TOO_MANY_REQUESTS)
    if _is_litellm_exception(exc, "Timeout"):
        return AppError(ErrorCode.upstream_timeout, status.HTTP_504_GATEWAY_TIMEOUT)
    return AppError(ErrorCode.upstream_error, status.HTTP_502_BAD_GATEWAY)


def _usage_status_for_exception(exc: Exception) -> str:
    if _is_litellm_exception(exc, "Timeout"):
        return UsageStatus.timeout.value
    return UsageStatus.error.value


def _is_litellm_exception(exc: Exception, name: str) -> bool:
    exception_type = getattr(litellm, name, None)
    return isinstance(exception_type, type) and isinstance(exc, exception_type)


def _dump_model(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump()
    return value


def _dump_json(value: Any) -> str:
    if hasattr(value, "model_dump_json"):
        return str(value.model_dump_json())
    return json.dumps(jsonable_encoder(value), ensure_ascii=False)


def _latency_ms(started_at: float) -> int:
    return int((monotonic() - started_at) * 1000)


def _request_id(request: Request) -> str:
    return getattr(request.state, "trace_id", None) or str(uuid4())


def _quota_headers(quota_result: QuotaCheckResult) -> dict[str, str]:
    if not quota_result.warnings:
        return {}
    return {"X-Gateway-Quota-Warnings": str(len(quota_result.warnings))}


def _anthropic_to_openai_messages(
    messages: list[Any], system_message: Any | None
) -> list[dict[str, Any]]:
    converted: list[dict[str, Any]] = []
    if system_message is not None:
        converted.append({"role": "system", "content": system_message})
    converted.extend(_message_to_openai(message) for message in messages)
    return converted


def _gemini_to_openai_messages(
    contents: list[Any], system_instruction: Any | None
) -> list[dict[str, Any]]:
    converted: list[dict[str, Any]] = []
    if system_instruction is not None:
        converted.append({"role": "system", "content": _gemini_text(system_instruction)})
    for content in contents:
        role = "user"
        if isinstance(content, Mapping) and content.get("role") == "model":
            role = "assistant"
        converted.append({"role": role, "content": _gemini_text(content)})
    return converted


def _message_to_openai(message: Any) -> dict[str, Any]:
    if isinstance(message, Mapping):
        return {"role": message.get("role", "user"), "content": message.get("content", "")}
    return {"role": "user", "content": message}


def _gemini_text(value: Any) -> Any:
    if not isinstance(value, Mapping):
        return value
    parts = value.get("parts")
    if not isinstance(parts, list):
        return value.get("text", value)
    texts = [part.get("text", "") for part in parts if isinstance(part, Mapping)]
    return "".join(texts)


def _openai_to_anthropic_response(body: bytes) -> dict[str, Any]:
    data = json.loads(body.decode())
    choice = (data.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    usage = data.get("usage") or {}
    return {
        "id": data.get("id"),
        "type": "message",
        "role": "assistant",
        "model": data.get("model"),
        "content": [{"type": "text", "text": message.get("content", "")}],
        "stop_reason": choice.get("finish_reason"),
        "stop_sequence": None,
        "usage": {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
        },
    }


def _openai_to_gemini_response(body: bytes) -> dict[str, Any]:
    data = json.loads(body.decode())
    choice = (data.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    return {
        "candidates": [
            {
                "content": {
                    "role": "model",
                    "parts": [{"text": message.get("content", "")}],
                },
                "finishReason": choice.get("finish_reason"),
                "index": choice.get("index", 0),
            }
        ],
        "usageMetadata": data.get("usage", {}),
        "modelVersion": data.get("model"),
    }
