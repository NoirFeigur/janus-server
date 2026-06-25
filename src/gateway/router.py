from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Mapping
from contextlib import suppress
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
from src.config import get_settings
from src.db.models.model_catalog import LogicalModel
from src.enums import ErrorCode, UsageStatus
from src.exceptions import AppError
from src.gateway.context import GatewayRequestContext
from src.gateway.dependencies import get_gateway_service
from src.gateway.finalize import finalize_gateway_request
from src.gateway.quota import QuotaCheckResult, QuotaReservation
from src.gateway.rate_limit import (
    ESTIMATED_TOKENS_PER_REQUEST,
    check_rate_limits,
    estimate_request_tokens,
    release_concurrent,
)
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

router = APIRouter(tags=["gateway"])

GatewayServiceDep = Annotated[GatewayService, Depends(get_gateway_service)]
RequestSchemaT = TypeVar("RequestSchemaT", bound=BaseModel)

# 4 MiB accommodates Anthropic 200K-token requests with base64 images and
# tool definitions; the previous 1 MiB cap rejected legitimate Claude payloads.
_MAX_BODY_BYTES = 4 * 1_048_576
_STREAM_MAX_DURATION_SECONDS = 1800
_STREAM_IDLE_TIMEOUT_SECONDS = 60


async def _finalize_stream(
    ctx: GatewayRequestContext,
    *,
    logical_model: LogicalModel,
    service: GatewayService,
    response: Any,
    member: str,
    rate_limit_rules: list[dict[str, Any]] | None,
    quota_reservations: list[QuotaReservation] | None,
) -> None:
    """Run all post-stream cleanup in a single shielded block.

    Wrapping the cleanup in :func:`asyncio.shield` is load-bearing: when the
    client disconnects, Starlette cancels the streaming task and the generator's
    ``finally`` runs with a pending ``CancelledError``. Without the shield the
    second ``await`` in the chain (e.g. ``release_concurrent`` after
    ``finalize_gateway_request``) is interrupted at the next checkpoint, leaving
    the TPM reservation un-refunded, the concurrent semaphore slot held, and the
    upstream HTTP connection leaked. The shield guarantees this cleanup runs to
    completion regardless of cancellation.
    """
    await finalize_gateway_request(
        ctx,
        logical_model=logical_model,
        service=service,
        rate_limit_rules=rate_limit_rules,
        quota_reservations=quota_reservations,
    )
    if rate_limit_rules:
        await release_concurrent(member, rate_limit_rules)
    await _aclose_response(response)


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
    rl_member = uuid4().hex
    logical_model = await service.resolve_model(user, payload.model)

    # P2: Rate limiting (before quota reservation)
    rate_limit_rules = await _check_rate_limits(
        service=service,
        user=user,
        logical_model_id=logical_model.id,
        request_id=request_id,
        member=rl_member,
        is_stream=payload.stream,
        estimated_tokens=estimate_request_tokens(payload.messages),
    )

    quota_result = await service.check_quota(user.user_id, user.department_id, logical_model.id)
    quota_reservations = quota_result.reservations

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
    extra = payload.model_dump(exclude={"model", "messages", "max_tokens", "stream", "system"})
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
        await finalize_gateway_request(
            ctx,
            logical_model=logical_model,
            service=service,
            rate_limit_rules=rate_limit_rules,
            quota_reservations=quota_reservations,
        )
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
                member=rl_member,
                channel_id=channel_id,
                upstream_model=upstream_model,
                rate_limit_rules=rate_limit_rules,
                quota_reservations=quota_reservations,
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
    await finalize_gateway_request(
        ctx,
        logical_model=logical_model,
        service=service,
        rate_limit_rules=rate_limit_rules,
        quota_reservations=quota_reservations,
    )
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
    rl_member = uuid4().hex
    logical_model = await service.resolve_model(user, model_name)

    stream = request.url.path.endswith(":streamGenerateContent")

    # P2: Rate limiting (before quota reservation)
    rate_limit_rules = await _check_rate_limits(
        service=service,
        user=user,
        logical_model_id=logical_model.id,
        request_id=request_id,
        member=rl_member,
        is_stream=stream,
        estimated_tokens=estimate_request_tokens(payload.contents),
    )

    quota_result = await service.check_quota(user.user_id, user.department_id, logical_model.id)
    quota_reservations = quota_result.reservations

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
        await finalize_gateway_request(
            ctx,
            logical_model=logical_model,
            service=service,
            rate_limit_rules=rate_limit_rules,
            quota_reservations=quota_reservations,
        )
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
                member=rl_member,
                channel_id=channel_id,
                upstream_model=upstream_model,
                rate_limit_rules=rate_limit_rules,
                quota_reservations=quota_reservations,
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
    await finalize_gateway_request(
        ctx,
        logical_model=logical_model,
        service=service,
        rate_limit_rules=rate_limit_rules,
        quota_reservations=quota_reservations,
    )
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


async def _check_rate_limits(
    *,
    service: GatewayService,
    user: AuthenticatedUser,
    logical_model_id: int,
    request_id: str,
    member: str,
    is_stream: bool,
    estimated_tokens: int = ESTIMATED_TOKENS_PER_REQUEST,
) -> list[dict[str, Any]]:
    """Enforce rate limits before quota reservation (P2).

    ``member`` is a server-side unguessable token (not the client-controllable
    request id) used as the RPM/concurrent sorted-set member; the same value
    must be passed to ``release_concurrent`` in the stream finally block.

    ``estimated_tokens`` is the upfront TPM reservation. Callers should pass a
    length-derived estimate via :func:`estimate_request_tokens` so a 5k-prompt
    request cannot bypass a tight TPM bucket by reserving only the default
    floor. Settlement at finalize reconciles to actuals either way.

    Returns the applicable rules so the caller can release the concurrent slot
    in the stream finally block.  Raises 429 if any limit is exceeded.
    """
    rules = await service.get_rate_limit_rules(
        user.user_id,
        user.department_id,
        logical_model_id,
        user.api_key_id,
    )
    if rules:
        rl_result = await check_rate_limits(
            request_id=request_id,
            member=member,
            rules=rules,
            estimated_tokens=estimated_tokens,
            is_stream=is_stream,
        )
        if not rl_result.allowed:
            raise AppError(
                ErrorCode.rate_limit_exceeded,
                status.HTTP_429_TOO_MANY_REQUESTS,
            )
    return rules


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
    rl_member = uuid4().hex
    logical_model = await service.resolve_model(user, requested_model)

    # P2: Rate limiting (before quota reservation)
    rate_limit_rules = await _check_rate_limits(
        service=service,
        user=user,
        logical_model_id=logical_model.id,
        request_id=request_id,
        member=rl_member,
        is_stream=stream,
        estimated_tokens=estimate_request_tokens(messages),
    )

    quota_result = await service.check_quota(user.user_id, user.department_id, logical_model.id)
    quota_reservations = quota_result.reservations

    # P4: Response cache check (non-streaming only)
    settings = get_settings()
    fingerprint: str | None = None
    if not stream and is_cacheable_request(
        stream=stream,
        response_cache_enabled=settings.response_cache_enabled,
        params=params,
    ):
        fingerprint = compute_fingerprint(logical_model.name, messages, params)
        cached = await get_cached_response(logical_model.id, fingerprint)
        if cached is not None:
            # Cache hit: still record usage with cache_hit=True. _extract_usage
            # safely returns _empty_usage() when the cached payload lacks a
            # usage dict, so probe directly without a stringly-typed sniff (the
            # old "usage" in str(cached) test false-matched any user content
            # mentioning the word "usage").
            usage = _extract_usage(cached)
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
            await finalize_gateway_request(
                ctx,
                logical_model=logical_model,
                service=service,
                rate_limit_rules=rate_limit_rules,
                quota_reservations=quota_reservations,
            )
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
        await finalize_gateway_request(
            ctx,
            logical_model=logical_model,
            service=service,
            rate_limit_rules=rate_limit_rules,
            quota_reservations=quota_reservations,
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
                member=rl_member,
                rate_limit_rules=rate_limit_rules,
                quota_reservations=quota_reservations,
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
    await finalize_gateway_request(
        ctx,
        logical_model=logical_model,
        service=service,
        rate_limit_rules=rate_limit_rules,
        quota_reservations=quota_reservations,
    )

    # P4: Cache the response if eligible
    if fingerprint is not None and is_cacheable_response(response):
        await set_cached_response(
            logical_model.id,
            fingerprint,
            jsonable_encoder(_dump_model(response)),
            ttl_seconds=settings.response_cache_ttl_seconds,
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
    member: str,
    rate_limit_rules: list[dict[str, Any]] | None = None,
    quota_reservations: list[QuotaReservation] | None = None,
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
        # Always emit [DONE] so OpenAI SDK clients don't hang waiting for the
        # terminator on idle/duration timeout. Errors raise out of the loop and
        # are signalled separately via the response status, so they skip [DONE]
        # — but timeout breaks fall through here.
        done = "data: [DONE]\n\n"
        bytes_yielded += len(done.encode("utf-8"))
        yield done
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
        # Shielded cleanup: settle quota, refund TPM, release concurrent slot,
        # close upstream. Without the shield, a client disconnect cancels these
        # mid-flight (TPM leak, concurrent slot leak, upstream connection leak).
        await asyncio.shield(
            _finalize_stream(
                ctx,
                logical_model=logical_model,
                service=service,
                response=response,
                member=member,
                rate_limit_rules=rate_limit_rules,
                quota_reservations=quota_reservations,
            )
        )


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
    member: str,
    channel_id: int | None,
    upstream_model: str | None,
    rate_limit_rules: list[dict[str, Any]] | None = None,
    quota_reservations: list[QuotaReservation] | None = None,
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
        # Shielded cleanup: see _finalize_stream docstring for why this is
        # mandatory for client-disconnect safety.
        await asyncio.shield(
            _finalize_stream(
                ctx,
                logical_model=logical_model,
                service=service,
                response=response,
                member=member,
                rate_limit_rules=rate_limit_rules,
                quota_reservations=quota_reservations,
            )
        )


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
    member: str,
    channel_id: int | None,
    upstream_model: str | None,
    rate_limit_rules: list[dict[str, Any]] | None = None,
    quota_reservations: list[QuotaReservation] | None = None,
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
        # Shielded cleanup: see _finalize_stream docstring for why this is
        # mandatory for client-disconnect safety.
        await asyncio.shield(
            _finalize_stream(
                ctx,
                logical_model=logical_model,
                service=service,
                response=response,
                member=member,
                rate_limit_rules=rate_limit_rules,
                quota_reservations=quota_reservations,
            )
        )


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
                "completion_tokens": int(getattr(resp_usage, "output_tokens", 0) or 0),
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
                "completion_tokens": int(getattr(um, "candidates_token_count", 0) or 0),
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
    """Extract channel_id and upstream_model from a litellm OpenAI response.

    OpenAI's ``Router.acompletion`` does not honour a caller-provided
    ``litellm_metadata`` mutable dict (the way ``aanthropic_messages`` /
    ``agenerate_content`` do), so this path reads the response's
    ``_hidden_params.model_info.id`` that litellm populates on every
    ``ModelResponse`` / ``ModelResponseStream``. The leading underscore is a
    library convention, not a private contract: this field is part of
    litellm's public Router contract and is asserted against in their own
    test suite. Both attribute and ``Mapping`` access shapes are handled so
    a future shape change (e.g. typed dataclass) does not silently null out
    channel attribution.
    """
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


async def _aclose_response(response: Any) -> None:
    """Close an upstream streaming response, swallowing any close-time error.

    litellm streaming wrappers expose ``aclose()`` (CustomStreamWrapper) over the
    underlying provider iterator. When a stream is abandoned mid-flight (idle
    timeout, max-duration cap, client disconnect, upstream error) the generator's
    ``finally`` must close it, otherwise the upstream HTTP connection is leaked
    back to the pool unconsumed. Best-effort: a missing ``aclose`` or a close-time
    exception must never mask the original outcome.
    """
    aclose = getattr(response, "aclose", None)
    if aclose is None:
        return
    with suppress(Exception):
        await aclose()


def _quota_headers(quota_result: QuotaCheckResult) -> dict[str, str]:
    if not quota_result.warnings:
        return {}
    return {"X-Gateway-Quota-Warnings": str(len(quota_result.warnings))}


def _usage_value(usage: Any, key: str) -> int:
    value = usage.get(key, 0) if isinstance(usage, Mapping) else getattr(usage, key, 0)
    return int(value or 0)
