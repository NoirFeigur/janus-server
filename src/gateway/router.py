from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Mapping
from contextlib import suppress
from decimal import Decimal
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
from src.gateway.context import GatewayRequestContext
from src.gateway.dependencies import get_gateway_service
from src.gateway.finalize import finalize_gateway_request
from src.gateway.quota import QuotaCheckResult
from src.gateway.rate_limit import check_rate_limits
from src.gateway.response_cache import (
    compute_fingerprint,
    get_cached_response,
    is_cacheable_request,
    is_cacheable_response,
    set_cached_response,
)
from src.gateway.router_manager import RouterManager
from src.gateway.schemas import AnthropicRequest, GeminiRequest, OpenAIRequest
from src.gateway.service import GatewayService
from src.gateway.usage import UsageData, compute_cost, record_usage

router = APIRouter(tags=["gateway"])

GatewayServiceDep = Annotated[GatewayService, Depends(get_gateway_service)]
RequestSchemaT = TypeVar("RequestSchemaT", bound=BaseModel)

_MAX_BODY_BYTES = 1_048_576
_STREAM_MAX_DURATION_SECONDS = 1800
_STREAM_IDLE_TIMEOUT_SECONDS = 60


# ---------------------------------------------------------------------------
# OpenAI endpoint (unchanged — acompletion already returns OpenAI format)
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Anthropic endpoint — native passthrough via router.aanthropic_messages()
# ---------------------------------------------------------------------------


@router.post("/v1/messages", response_model=None)
async def anthropic_messages(
    request: Request,
    user: CurrentUser,
    service: GatewayServiceDep,
) -> JSONResponse | StreamingResponse:
    payload = await _parse_body(request, AnthropicRequest)
    request_id = _request_id(request)
    logical_model = await service.resolve_model(user, payload.model)
    quota_result = await service.check_quota(
        user.user_id, user.department_id, logical_model.id
    )

    # Build native Anthropic params (no conversion to OpenAI format)
    native_params: dict[str, Any] = {
        "model": logical_model.name,
        "messages": payload.messages,
        "max_tokens": payload.max_tokens,
        "stream": payload.stream,
    }
    if payload.system is not None:
        native_params["system"] = payload.system
    # Forward extra params (temperature, top_p, top_k, tools, etc.)
    extra = payload.model_dump(
        exclude={"model", "messages", "max_tokens", "stream", "system"}
    )
    native_params.update(extra)

    # Mutable dict — Router writes deployment info into it before the call
    litellm_meta: dict[str, Any] = {}
    native_params["litellm_metadata"] = litellm_meta

    started_at = monotonic()
    llm_router = RouterManager.get_router()

    try:
        response = await llm_router.aanthropic_messages(**native_params)
    except Exception as exc:
        # Build ctx for error finalization
        ctx = GatewayRequestContext(
            request_id=request_id,
            user_id=user.user_id,
            department_id=user.department_id,
            api_key_id=user.api_key_id,
            requested_model=logical_model.name,
            logical_model_id=logical_model.id,
            logical_model_name=logical_model.name,
            started_at=started_at,
            stream=payload.stream,
            quota_reserved=True,
        )
        ctx.mark_error(_usage_status_for_exception(exc))
        _record_usage_task(
            user=user,
            logical_model=logical_model,
            usage=_empty_usage(),
            status_value=ctx.status,
            latency_ms=_latency_ms(started_at),
            request_id=request_id,
        )
        await finalize_gateway_request(ctx, logical_model=logical_model, service=service)
        raise _upstream_error(exc) from exc

    # Extract channel info from metadata (populated by Router before the call)
    channel_id = _channel_id_from_metadata(litellm_meta)
    upstream_model = litellm_meta.get("deployment")

    if payload.stream:
        return StreamingResponse(
            _stream_anthropic_native(
                response=response,
                user=user,
                logical_model=logical_model,
                service=service,
                started_at=started_at,
                request_id=request_id,
                channel_id=channel_id,
                upstream_model=upstream_model,
            ),
            media_type="text/event-stream",
        )

    # Non-streaming: response is AnthropicMessagesResponse (dict-like)
    usage = _extract_anthropic_usage(response)
    ctx = GatewayRequestContext(
        request_id=request_id,
        user_id=user.user_id,
        department_id=user.department_id,
        api_key_id=user.api_key_id,
        requested_model=logical_model.name,
        logical_model_id=logical_model.id,
        logical_model_name=logical_model.name,
        channel_id=channel_id,
        upstream_model=upstream_model,
        started_at=started_at,
        stream=False,
        quota_reserved=True,
    )
    ctx.set_usage(usage["prompt_tokens"], usage["completion_tokens"], usage["total_tokens"])
    _record_usage_task(
        user=user,
        logical_model=logical_model,
        usage=usage,
        status_value=UsageStatus.success.value,
        latency_ms=_latency_ms(started_at),
        request_id=request_id,
        channel_id=channel_id,
        upstream_model=upstream_model,
    )
    await finalize_gateway_request(ctx, logical_model=logical_model, service=service)
    # Return native Anthropic response directly
    return JSONResponse(
        content=_to_json_serializable(response),
        headers=_quota_headers(quota_result),
    )


# ---------------------------------------------------------------------------
# Gemini endpoint — native passthrough via router.agenerate_content_stream()
# ---------------------------------------------------------------------------


@router.post("/v1beta/models/{model_name}:generateContent", response_model=None)
@router.post("/v1beta/models/{model_name}:streamGenerateContent", response_model=None)
async def gemini_generate_content(
    model_name: str,
    request: Request,
    user: CurrentUser,
    service: GatewayServiceDep,
) -> JSONResponse | StreamingResponse:
    payload = await _parse_body(request, GeminiRequest)
    request_id = _request_id(request)
    logical_model = await service.resolve_model(user, model_name)
    quota_result = await service.check_quota(
        user.user_id, user.department_id, logical_model.id
    )

    stream = request.url.path.endswith(":streamGenerateContent")
    litellm_meta: dict[str, Any] = {}
    started_at = monotonic()
    llm_router = RouterManager.get_router()

    # Build native Gemini params
    native_params: dict[str, Any] = {
        "model": logical_model.name,
        "contents": payload.contents,
        "litellm_metadata": litellm_meta,
    }
    if payload.system_instruction is not None:
        native_params["systemInstruction"] = payload.system_instruction
    if payload.generationConfig is not None:
        native_params["generationConfig"] = payload.generationConfig
    if payload.tools is not None:
        native_params["tools"] = payload.tools
    if payload.tool_config is not None:
        native_params["toolConfig"] = payload.tool_config
    if payload.safety_settings is not None:
        native_params["safetySettings"] = payload.safety_settings
    if payload.cached_content is not None:
        native_params["cachedContent"] = payload.cached_content

    try:
        if stream:
            response = await llm_router.agenerate_content_stream(**native_params)
        else:
            response = await llm_router.agenerate_content(**native_params)
    except Exception as exc:
        ctx = GatewayRequestContext(
            request_id=request_id,
            user_id=user.user_id,
            department_id=user.department_id,
            api_key_id=user.api_key_id,
            requested_model=logical_model.name,
            logical_model_id=logical_model.id,
            logical_model_name=logical_model.name,
            started_at=started_at,
            stream=stream,
            quota_reserved=True,
        )
        ctx.mark_error(_usage_status_for_exception(exc))
        _record_usage_task(
            user=user,
            logical_model=logical_model,
            usage=_empty_usage(),
            status_value=ctx.status,
            latency_ms=_latency_ms(started_at),
            request_id=request_id,
        )
        await finalize_gateway_request(ctx, logical_model=logical_model, service=service)
        raise _upstream_error(exc) from exc

    channel_id = _channel_id_from_metadata(litellm_meta)
    upstream_model = litellm_meta.get("deployment")

    if stream:
        return StreamingResponse(
            _stream_gemini_native(
                response=response,
                user=user,
                logical_model=logical_model,
                service=service,
                started_at=started_at,
                request_id=request_id,
                channel_id=channel_id,
                upstream_model=upstream_model,
            ),
            media_type="text/event-stream",
        )

    # Non-streaming: return native Gemini response
    usage = _extract_gemini_usage_from_response(response)
    ctx = GatewayRequestContext(
        request_id=request_id,
        user_id=user.user_id,
        department_id=user.department_id,
        api_key_id=user.api_key_id,
        requested_model=logical_model.name,
        logical_model_id=logical_model.id,
        logical_model_name=logical_model.name,
        channel_id=channel_id,
        upstream_model=upstream_model,
        started_at=started_at,
        stream=False,
        quota_reserved=True,
    )
    ctx.set_usage(usage["prompt_tokens"], usage["completion_tokens"], usage["total_tokens"])
    _record_usage_task(
        user=user,
        logical_model=logical_model,
        usage=usage,
        status_value=UsageStatus.success.value,
        latency_ms=_latency_ms(started_at),
        request_id=request_id,
        channel_id=channel_id,
        upstream_model=upstream_model,
    )
    await finalize_gateway_request(ctx, logical_model=logical_model, service=service)
    return JSONResponse(
        content=_to_json_serializable(response),
        headers=_quota_headers(quota_result),
    )


# ---------------------------------------------------------------------------
# Body parsing
# ---------------------------------------------------------------------------


async def _parse_body(request: Request, schema: type[RequestSchemaT]) -> RequestSchemaT:
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > _MAX_BODY_BYTES:
            raise AppError(
                ErrorCode.request_invalid,
                status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            )
        chunks.append(chunk)
    body = b"".join(chunks)
    try:
        return schema.model_validate_json(body)
    except (ValidationError, ValueError) as exc:
        raise AppError(
            ErrorCode.request_invalid,
            status.HTTP_422_UNPROCESSABLE_ENTITY,
        ) from exc


# ---------------------------------------------------------------------------
# OpenAI completion path (unchanged from before)
# ---------------------------------------------------------------------------


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

    # P2: Rate limiting (before quota reservation)
    rate_limit_rules = await service.get_rate_limit_rules(
        user.user_id, user.department_id, logical_model.id
    )
    if rate_limit_rules:
        rl_result = await check_rate_limits(
            request_id=request_id,
            rules=rate_limit_rules,
            estimated_tokens=100,
            is_stream=stream,
        )
        if not rl_result.allowed:
            raise AppError(
                ErrorCode.rate_limit_exceeded,
                status.HTTP_429_TOO_MANY_REQUESTS,
            )

    quota_result = await service.check_quota(
        user.user_id, user.department_id, logical_model.id
    )

    # P4: Response cache check (non-streaming only)
    fingerprint: str | None = None
    if not stream and is_cacheable_request(
        stream=stream, response_cache_enabled=True, params=params
    ):
        fingerprint = compute_fingerprint(logical_model.name, messages, params)
        cached = await get_cached_response(logical_model.id, fingerprint)
        if cached is not None:
            # Cache hit: still record usage with cache_hit=True
            usage = _extract_usage(cached) if "usage" in str(cached) else _empty_usage()
            ctx = GatewayRequestContext(
                request_id=request_id,
                user_id=user.user_id,
                department_id=user.department_id,
                api_key_id=user.api_key_id,
                requested_model=logical_model.name,
                logical_model_id=logical_model.id,
                logical_model_name=logical_model.name,
                started_at=monotonic(),
                stream=False,
                quota_reserved=True,
                cache_hit=True,
            )
            ctx.set_usage(usage["prompt_tokens"], usage["completion_tokens"], usage["total_tokens"])
            _record_usage_task(
                user=user,
                logical_model=logical_model,
                usage=usage,
                status_value=UsageStatus.success.value,
                latency_ms=0,
                request_id=request_id,
            )
            await finalize_gateway_request(ctx, logical_model=logical_model, service=service)
            return JSONResponse(
                content=cached,
                headers=_quota_headers(quota_result),
            )

    started_at = monotonic()
    llm_router = RouterManager.get_router()
    if stream:
        params["stream_options"] = {"include_usage": True}
    try:
        response = await llm_router.acompletion(
            model=logical_model.name,
            messages=messages,
            stream=stream,
            **params,
        )
    except Exception as exc:
        # Build ctx for error finalization
        ctx = GatewayRequestContext(
            request_id=request_id,
            user_id=user.user_id,
            department_id=user.department_id,
            api_key_id=user.api_key_id,
            requested_model=logical_model.name,
            logical_model_id=logical_model.id,
            logical_model_name=logical_model.name,
            started_at=started_at,
            stream=stream,
            quota_reserved=True,
        )
        ctx.mark_error(_usage_status_for_exception(exc))
        _record_usage_task(
            user=user,
            logical_model=logical_model,
            usage=_empty_usage(),
            status_value=ctx.status,
            latency_ms=_latency_ms(started_at),
            request_id=request_id,
        )
        await finalize_gateway_request(ctx, logical_model=logical_model, service=service)
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
    channel_id, upstream_model = _extract_routing_info(response)
    # Build ctx for success finalization
    ctx = GatewayRequestContext(
        request_id=request_id,
        user_id=user.user_id,
        department_id=user.department_id,
        api_key_id=user.api_key_id,
        requested_model=logical_model.name,
        logical_model_id=logical_model.id,
        logical_model_name=logical_model.name,
        channel_id=channel_id,
        upstream_model=upstream_model,
        started_at=started_at,
        stream=False,
        quota_reserved=True,
    )
    ctx.set_usage(usage["prompt_tokens"], usage["completion_tokens"], usage["total_tokens"])
    # Dual-write: direct DB path
    _record_usage_task(
        user=user,
        logical_model=logical_model,
        usage=usage,
        status_value=UsageStatus.success.value,
        latency_ms=_latency_ms(started_at),
        request_id=request_id,
        channel_id=channel_id,
        upstream_model=upstream_model,
    )
    await finalize_gateway_request(ctx, logical_model=logical_model, service=service)

    # P4: Cache the response if eligible
    if fingerprint is not None and is_cacheable_response(response):
        await set_cached_response(
            logical_model.id, fingerprint, jsonable_encoder(_dump_model(response))
        )

    return JSONResponse(
        content=jsonable_encoder(_dump_model(response)),
        headers=_quota_headers(quota_result),
    )


# ---------------------------------------------------------------------------
# OpenAI streaming generator (unchanged)
# ---------------------------------------------------------------------------


async def _stream_openai(
    *,
    response: Any,
    user: AuthenticatedUser,
    logical_model: LogicalModel,
    service: GatewayService,
    started_at: float,
    request_id: str,
) -> AsyncIterator[str]:
    ctx = GatewayRequestContext(
        request_id=request_id,
        user_id=user.user_id,
        department_id=user.department_id,
        api_key_id=user.api_key_id,
        requested_model=logical_model.name,
        logical_model_id=logical_model.id,
        logical_model_name=logical_model.name,
        started_at=started_at,
        stream=True,
        quota_reserved=True,
    )
    usage = _empty_usage()
    status_value = UsageStatus.success.value
    channel_id: int | None = None
    upstream_model: str | None = None
    stream_start = monotonic()
    response_iter = response.__aiter__()
    bytes_yielded = 0
    try:
        while True:
            elapsed = monotonic() - stream_start
            if elapsed > _STREAM_MAX_DURATION_SECONDS:
                status_value = UsageStatus.timeout.value
                break
            try:
                async with asyncio.timeout(_STREAM_IDLE_TIMEOUT_SECONDS):
                    chunk = await response_iter.__anext__()
            except TimeoutError:
                status_value = UsageStatus.timeout.value
                break
            except StopAsyncIteration:
                break
            chunk_channel_id, chunk_upstream_model = _extract_routing_info(chunk)
            channel_id = channel_id or chunk_channel_id
            upstream_model = upstream_model or chunk_upstream_model
            chunk_usage = _extract_usage(chunk)
            if chunk_usage["total_tokens"]:
                usage = chunk_usage
            data = f"data: {_dump_json(chunk)}\n\n"
            if bytes_yielded == 0:
                ctx.record_ttft()
            bytes_yielded += len(data.encode("utf-8"))
            yield data
        if status_value == UsageStatus.success.value:
            data = "data: [DONE]\n\n"
            bytes_yielded += len(data.encode("utf-8"))
            yield data
    except asyncio.CancelledError:
        status_value = UsageStatus.error.value
        raise
    except Exception as exc:
        status_value = _usage_status_for_exception(exc)
        raise _upstream_error(exc) from exc
    finally:
        response_channel_id, response_upstream_model = _extract_routing_info(response)
        channel_id = channel_id or response_channel_id
        upstream_model = upstream_model or response_upstream_model
        # Populate context for finalizer
        ctx.channel_id = channel_id
        ctx.upstream_model = upstream_model
        ctx.status = status_value
        ctx.set_usage(usage["prompt_tokens"], usage["completion_tokens"], usage["total_tokens"])
        # Dual-write: direct DB path (existing tests depend on this)
        _record_usage_task(
            user=user,
            logical_model=logical_model,
            usage=usage,
            status_value=status_value,
            latency_ms=_latency_ms(started_at),
            request_id=request_id,
            channel_id=channel_id,
            upstream_model=upstream_model,
        )
        # Unified finalizer: quota settlement + durable enqueue + metrics
        await finalize_gateway_request(ctx, logical_model=logical_model, service=service)


# ---------------------------------------------------------------------------
# Anthropic native streaming — pipe raw SSE bytes, parse usage mid-stream
# ---------------------------------------------------------------------------


async def _stream_anthropic_native(
    *,
    response: Any,
    user: AuthenticatedUser,
    logical_model: LogicalModel,
    service: GatewayService,
    started_at: float,
    request_id: str,
    channel_id: int | None,
    upstream_model: str | None,
) -> AsyncIterator[bytes]:
    """Pipe raw Anthropic SSE bytes to client, parsing usage from events."""
    ctx = GatewayRequestContext(
        request_id=request_id,
        user_id=user.user_id,
        department_id=user.department_id,
        api_key_id=user.api_key_id,
        requested_model=logical_model.name,
        logical_model_id=logical_model.id,
        logical_model_name=logical_model.name,
        channel_id=channel_id,
        upstream_model=upstream_model,
        started_at=started_at,
        stream=True,
        quota_reserved=True,
    )
    usage = _empty_usage()
    status_value = UsageStatus.success.value
    stream_start = monotonic()
    response_iter = response.__aiter__()
    bytes_yielded = 0
    sse_buffer = [""]

    try:
        while True:
            elapsed = monotonic() - stream_start
            if elapsed > _STREAM_MAX_DURATION_SECONDS:
                status_value = UsageStatus.timeout.value
                break
            try:
                async with asyncio.timeout(_STREAM_IDLE_TIMEOUT_SECONDS):
                    chunk = await response_iter.__anext__()
            except TimeoutError:
                status_value = UsageStatus.timeout.value
                break
            except StopAsyncIteration:
                break

            # chunk is bytes (raw SSE) — parse for usage then yield as-is
            raw_bytes = chunk if isinstance(chunk, bytes) else chunk.encode("utf-8")
            _accumulate_anthropic_usage(raw_bytes, usage, sse_buffer)
            if bytes_yielded == 0:
                ctx.record_ttft()
            bytes_yielded += len(raw_bytes)
            yield raw_bytes

    except asyncio.CancelledError:
        status_value = UsageStatus.error.value
        raise
    except Exception as exc:
        status_value = _usage_status_for_exception(exc)
        raise _upstream_error(exc) from exc
    finally:
        # Populate context for finalizer
        ctx.status = status_value
        ctx.set_usage(usage["prompt_tokens"], usage["completion_tokens"], usage["total_tokens"])
        # Dual-write: direct DB path (existing tests depend on this)
        _record_usage_task(
            user=user,
            logical_model=logical_model,
            usage=usage,
            status_value=status_value,
            latency_ms=_latency_ms(started_at),
            request_id=request_id,
            channel_id=channel_id,
            upstream_model=upstream_model,
        )
        # Unified finalizer: quota settlement + durable enqueue + metrics
        await finalize_gateway_request(ctx, logical_model=logical_model, service=service)


# ---------------------------------------------------------------------------
# Gemini native streaming — pipe raw bytes, parse usage mid-stream
# ---------------------------------------------------------------------------


async def _stream_gemini_native(
    *,
    response: Any,
    user: AuthenticatedUser,
    logical_model: LogicalModel,
    service: GatewayService,
    started_at: float,
    request_id: str,
    channel_id: int | None,
    upstream_model: str | None,
) -> AsyncIterator[bytes]:
    """Pipe raw Gemini streaming bytes to client, parsing usage from chunks."""
    ctx = GatewayRequestContext(
        request_id=request_id,
        user_id=user.user_id,
        department_id=user.department_id,
        api_key_id=user.api_key_id,
        requested_model=logical_model.name,
        logical_model_id=logical_model.id,
        logical_model_name=logical_model.name,
        channel_id=channel_id,
        upstream_model=upstream_model,
        started_at=started_at,
        stream=True,
        quota_reserved=True,
    )
    usage = _empty_usage()
    status_value = UsageStatus.success.value
    stream_start = monotonic()
    response_iter = response.__aiter__()
    bytes_yielded = 0
    sse_buffer = [""]

    try:
        while True:
            elapsed = monotonic() - stream_start
            if elapsed > _STREAM_MAX_DURATION_SECONDS:
                status_value = UsageStatus.timeout.value
                break
            try:
                async with asyncio.timeout(_STREAM_IDLE_TIMEOUT_SECONDS):
                    chunk = await response_iter.__anext__()
            except TimeoutError:
                status_value = UsageStatus.timeout.value
                break
            except StopAsyncIteration:
                break

            raw_bytes = chunk if isinstance(chunk, bytes) else chunk.encode("utf-8")
            _accumulate_gemini_usage(raw_bytes, usage, sse_buffer)
            if bytes_yielded == 0:
                ctx.record_ttft()
            bytes_yielded += len(raw_bytes)
            yield raw_bytes

    except asyncio.CancelledError:
        status_value = UsageStatus.error.value
        raise
    except Exception as exc:
        status_value = _usage_status_for_exception(exc)
        raise _upstream_error(exc) from exc
    finally:
        # Populate context for finalizer
        ctx.status = status_value
        ctx.set_usage(usage["prompt_tokens"], usage["completion_tokens"], usage["total_tokens"])
        # Dual-write: direct DB path (existing tests depend on this)
        _record_usage_task(
            user=user,
            logical_model=logical_model,
            usage=usage,
            status_value=status_value,
            latency_ms=_latency_ms(started_at),
            request_id=request_id,
            channel_id=channel_id,
            upstream_model=upstream_model,
        )
        # Unified finalizer: quota settlement + durable enqueue + metrics
        await finalize_gateway_request(ctx, logical_model=logical_model, service=service)


# ---------------------------------------------------------------------------
# Usage extraction from native SSE bytes
# ---------------------------------------------------------------------------


def _accumulate_anthropic_usage(
    raw_bytes: bytes, usage: dict[str, int], buffer: list[str] | None = None
) -> None:
    """Parse Anthropic SSE bytes and extract usage from relevant events.

    Anthropic usage arrives in:
    - message_start.message.usage.input_tokens
    - message_delta.usage.output_tokens
    """
    try:
        sse_buffer = buffer if buffer is not None else [""]
        text = sse_buffer[0] + raw_bytes.decode("utf-8", errors="replace")
    except Exception:
        return
    lines = text.split("\n")
    sse_buffer[0] = lines[-1]
    for line in lines[:-1]:
        if not line.startswith("data: "):
            continue
        try:
            data = json.loads(line[6:])
        except (json.JSONDecodeError, ValueError):
            continue
        event_type = data.get("type", "")
        if event_type == "message_start":
            msg_usage = (data.get("message") or {}).get("usage") or {}
            input_tokens = int(msg_usage.get("input_tokens", 0))
            if input_tokens:
                usage["prompt_tokens"] = input_tokens
                usage["total_tokens"] = input_tokens + usage["completion_tokens"]
        elif event_type == "message_delta":
            delta_usage = data.get("usage") or {}
            output_tokens = int(delta_usage.get("output_tokens", 0))
            if output_tokens:
                usage["completion_tokens"] = output_tokens
                usage["total_tokens"] = usage["prompt_tokens"] + output_tokens


def _accumulate_gemini_usage(
    raw_bytes: bytes, usage: dict[str, int], buffer: list[str] | None = None
) -> None:
    """Parse Gemini streaming bytes and extract usageMetadata.

    Gemini sends `data: {...}` with optional `usageMetadata` containing
    promptTokenCount, candidatesTokenCount, totalTokenCount.
    """
    try:
        sse_buffer = buffer if buffer is not None else [""]
        text = sse_buffer[0] + raw_bytes.decode("utf-8", errors="replace")
    except Exception:
        return
    lines = text.split("\n")
    sse_buffer[0] = lines[-1]
    for line in lines[:-1]:
        if not line.startswith("data: "):
            continue
        try:
            data = json.loads(line[6:])
        except (json.JSONDecodeError, ValueError):
            continue
        usage_meta = data.get("usageMetadata")
        if usage_meta:
            prompt = int(usage_meta.get("promptTokenCount", 0))
            completion = int(usage_meta.get("candidatesTokenCount", 0))
            total = int(usage_meta.get("totalTokenCount", 0))
            if total:
                usage["prompt_tokens"] = prompt
                usage["completion_tokens"] = completion
                usage["total_tokens"] = total


def _extract_anthropic_usage(response: Any) -> dict[str, int]:
    """Extract usage from a non-streaming Anthropic response."""
    if isinstance(response, Mapping):
        resp_usage = response.get("usage") or {}
    elif hasattr(response, "usage"):
        resp_usage = response.usage
        if hasattr(resp_usage, "input_tokens"):
            return {
                "prompt_tokens": int(getattr(resp_usage, "input_tokens", 0) or 0),
                "completion_tokens": int(
                    getattr(resp_usage, "output_tokens", 0) or 0
                ),
                "total_tokens": int(getattr(resp_usage, "input_tokens", 0) or 0)
                + int(getattr(resp_usage, "output_tokens", 0) or 0),
            }
        resp_usage = {}
    else:
        resp_usage = {}
    input_tokens = int(resp_usage.get("input_tokens", 0))
    output_tokens = int(resp_usage.get("output_tokens", 0))
    return {
        "prompt_tokens": input_tokens,
        "completion_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
    }


def _extract_gemini_usage_from_response(response: Any) -> dict[str, int]:
    """Extract usage from a non-streaming Gemini response."""
    if isinstance(response, Mapping):
        usage_meta = response.get("usageMetadata") or {}
    elif hasattr(response, "usage_metadata"):
        um = response.usage_metadata
        if um is not None:
            return {
                "prompt_tokens": int(getattr(um, "prompt_token_count", 0) or 0),
                "completion_tokens": int(
                    getattr(um, "candidates_token_count", 0) or 0
                ),
                "total_tokens": int(getattr(um, "total_token_count", 0) or 0),
            }
        usage_meta = {}
    else:
        usage_meta = {}
    prompt = int(usage_meta.get("promptTokenCount", 0))
    completion = int(usage_meta.get("candidatesTokenCount", 0))
    total = int(usage_meta.get("totalTokenCount", 0))
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total or (prompt + completion),
    }


# ---------------------------------------------------------------------------
# Metadata helpers
# ---------------------------------------------------------------------------


def _channel_id_from_metadata(litellm_meta: dict[str, Any]) -> int | None:
    """Extract channel_id from the mutable litellm_metadata dict."""
    model_info = litellm_meta.get("model_info")
    if isinstance(model_info, dict):
        raw_id = model_info.get("id")
        if raw_id is not None:
            with suppress(ValueError, TypeError):
                return int(raw_id)
    return None


def _to_json_serializable(value: Any) -> Any:
    """Convert a response to a JSON-serializable dict."""
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if isinstance(value, Mapping):
        return dict(value)
    return jsonable_encoder(value)


# ---------------------------------------------------------------------------
# Shared helpers (quota, usage recording, errors)
# ---------------------------------------------------------------------------


async def _compensate_quota(
    service: GatewayService, user: AuthenticatedUser, logical_model_id: int
) -> None:
    quotas = await service.repo.get_active_quotas(
        user.user_id, user.department_id, logical_model_id
    )
    await service.quota.compensate(
        user.user_id, user.department_id, logical_model_id, quotas
    )


def _compute_internal_cost(
    logical_model: LogicalModel, usage: dict[str, int]
) -> Decimal | None:
    """Compute internal cost from the logical model's pricing."""
    return compute_cost(logical_model, usage["prompt_tokens"], usage["completion_tokens"])


def _record_usage_task(
    *,
    user: AuthenticatedUser,
    logical_model: LogicalModel,
    usage: dict[str, int],
    status_value: str,
    latency_ms: int | None,
    request_id: str,
    channel_id: int | None = None,
    upstream_model: str | None = None,
) -> None:
    # Primary: direct DB write (existing behavior, preserved for tests).
    asyncio.create_task(
        record_usage(
            UsageData(
                user_id=user.user_id,
                api_key_id=user.api_key_id,
                logical_model=logical_model,
                channel_id=channel_id,
                upstream_model=upstream_model,
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
    # Secondary: durable enqueue to Redis queue (P3) for gateway log / metrics.
    # This is fire-and-forget; if Redis is down, the primary path above handles it.
    asyncio.create_task(
        _enqueue_log_event(
            user=user,
            logical_model=logical_model,
            usage=usage,
            status_value=status_value,
            latency_ms=latency_ms,
            request_id=request_id,
            channel_id=channel_id,
            upstream_model=upstream_model,
        )
    )


async def _enqueue_log_event(
    *,
    user: AuthenticatedUser,
    logical_model: LogicalModel,
    usage: dict[str, int],
    status_value: str,
    latency_ms: int | None,
    request_id: str,
    channel_id: int | None = None,
    upstream_model: str | None = None,
) -> None:
    """Enqueue a gateway log event to the durable Redis queue (P1 observability)."""
    from src.gateway.events import enqueue_log_event

    with suppress(Exception):
        payload = {
            "request_id": request_id,
            "user_id": user.user_id,
            "api_key_id": user.api_key_id,
            "logical_model_id": logical_model.id,
            "model": logical_model.name,
            "channel_id": channel_id,
            "upstream_model": upstream_model,
            "provider": None,
            "status_code": 200 if status_value == UsageStatus.success.value else 502,
            "error_code": None,
            "error_body": None,
            "latency_ms": latency_ms,
            "ttft_ms": None,
            "tokens_in": usage["prompt_tokens"],
            "tokens_out": usage["completion_tokens"],
            "cache_hit": False,
            "stream": False,
            "retry_count": 0,
            "fallback_used": False,
        }
        await enqueue_log_event(payload)


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


def _extract_routing_info(response: Any) -> tuple[int | None, str | None]:
    """Extract channel_id and upstream_model from litellm response metadata."""
    upstream_model: str | None = None
    channel_id: int | None = None
    if hasattr(response, "model"):
        upstream_model = response.model
    elif isinstance(response, Mapping):
        model = response.get("model")
        if isinstance(model, str):
            upstream_model = model
    hidden = getattr(response, "_hidden_params", None)
    if hidden is None and isinstance(response, Mapping):
        hidden = response.get("_hidden_params")
    if isinstance(hidden, dict):
        model_info = hidden.get("model_info", {})
        if isinstance(model_info, dict):
            key_id = model_info.get("id")
            if key_id is not None:
                with suppress(ValueError, TypeError):
                    channel_id = int(key_id)
    return channel_id, upstream_model


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


def _usage_value(usage: Any, key: str) -> int:
    value = usage.get(key, 0) if isinstance(usage, Mapping) else getattr(usage, key, 0)
    return int(value or 0)
