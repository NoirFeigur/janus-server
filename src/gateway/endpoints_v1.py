"""P5 endpoint expansion — /v1/embeddings, /v1/models, /v1/responses.

These endpoints reuse the same auth/quota/rate-limit pipeline as chat completions
but target different litellm capabilities.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from time import monotonic
from typing import Annotated, Any
from uuid import uuid4

import litellm
from fastapi import APIRouter, Depends, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel
from starlette import status

from src.auth.dependencies import CurrentUser
from src.auth.service import AuthenticatedUser
from src.enums import ErrorCode, UsageStatus
from src.exceptions import AppError
from src.gateway.context import GatewayRequestContext
from src.gateway.dependencies import get_gateway_service
from src.gateway.events import enqueue_usage_event
from src.gateway.finalize import finalize_gateway_request
from src.gateway.quota import QuotaCheckResult, QuotaReservation
from src.gateway.rate_limit import (
    ESTIMATED_TOKENS_PER_REQUEST,
    check_rate_limits,
    release_concurrent,
)
from src.gateway.router_manager import RouterManager
from src.gateway.service import GatewayService
from src.gateway.usage import compute_cost

router = APIRouter(tags=["gateway"])

GatewayServiceDep = Annotated[GatewayService, Depends(get_gateway_service)]


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class EmbeddingsRequest(BaseModel):
    model: str
    input: str | list[str]  # noqa: A003
    encoding_format: str | None = None
    dimensions: int | None = None


class ResponsesRequest(BaseModel):
    model: str
    input: str | list[Any]  # noqa: A003
    stream: bool = False
    max_output_tokens: int | None = None
    temperature: float | None = None
    instructions: str | None = None


# ---------------------------------------------------------------------------
# /v1/embeddings
# ---------------------------------------------------------------------------


@router.post("/v1/embeddings", response_model=None)
async def embeddings(
    request: Request,
    user: CurrentUser,
    service: GatewayServiceDep,
    payload: EmbeddingsRequest,
) -> JSONResponse:
    """OpenAI-compatible embeddings endpoint."""
    request_id = _request_id(request)
    rl_member = uuid4().hex
    logical_model = await service.resolve_model(user, payload.model)
    rate_limit_rules = await _check_rate_limits(
        service=service,
        user=user,
        logical_model_id=logical_model.id,
        request_id=request_id,
        member=rl_member,
        is_stream=False,
    )
    quota_result = await service.check_quota(user.user_id, user.department_id, logical_model.id)
    quota_reservations = quota_result.reservations

    started_at = monotonic()
    llm_router = RouterManager.get_router()
    litellm_meta: dict[str, Any] = {}
    kwargs: dict[str, Any] = {
        "model": logical_model.name,
        "input": payload.input,
        "litellm_metadata": litellm_meta,
    }
    if payload.encoding_format is not None:
        kwargs["encoding_format"] = payload.encoding_format
    if payload.dimensions is not None:
        kwargs["dimensions"] = payload.dimensions

    try:
        response = await llm_router.aembedding(**kwargs)
    except Exception as exc:
        ctx = _base_context(
            request_id=request_id,
            user=user,
            logical_model=logical_model,
            started_at=started_at,
            stream=False,
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

    usage = _extract_embedding_usage(response)
    ctx = _base_context(
        request_id=request_id,
        user=user,
        logical_model=logical_model,
        started_at=started_at,
        stream=False,
        channel_id=_channel_id_from_metadata(litellm_meta),
        upstream_model=litellm_meta.get("deployment") or _response_model(response),
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
        content=jsonable_encoder(_dump_model(response)),
        headers=_quota_headers(quota_result),
    )


# ---------------------------------------------------------------------------
# /v1/models
# ---------------------------------------------------------------------------


@router.get("/v1/models", response_model=None)
async def list_models(
    user: CurrentUser,
    service: GatewayServiceDep,
) -> JSONResponse:
    """List models the authenticated user has access to (OpenAI-compatible)."""
    grants = await service.repo.get_user_granted_models(user.user_id, user.department_id)
    models: list[dict[str, Any]] = []
    for model in await service.repo.get_logical_models_by_ids(list(grants)):
        models.append({
            "id": model.name,
            "object": "model",
            "created": 0,
            "owned_by": "janus",
        })
    return JSONResponse(content={"object": "list", "data": models})


# ---------------------------------------------------------------------------
# /v1/responses (OpenAI Responses API)
# ---------------------------------------------------------------------------


@router.post("/v1/responses", response_model=None)
async def responses(
    request: Request,
    user: CurrentUser,
    service: GatewayServiceDep,
    payload: ResponsesRequest,
) -> JSONResponse | StreamingResponse:
    """OpenAI Responses API endpoint (uses litellm.aresponses)."""
    request_id = _request_id(request)
    rl_member = uuid4().hex
    logical_model = await service.resolve_model(user, payload.model)
    rate_limit_rules = await _check_rate_limits(
        service=service,
        user=user,
        logical_model_id=logical_model.id,
        request_id=request_id,
        member=rl_member,
        is_stream=payload.stream,
    )
    quota_result = await service.check_quota(user.user_id, user.department_id, logical_model.id)
    quota_reservations = quota_result.reservations

    started_at = monotonic()
    litellm_meta: dict[str, Any] = {}
    kwargs: dict[str, Any] = {
        "model": logical_model.name,
        "input": payload.input,
        "litellm_metadata": litellm_meta,
    }
    if payload.max_output_tokens is not None:
        kwargs["max_output_tokens"] = payload.max_output_tokens
    if payload.temperature is not None:
        kwargs["temperature"] = payload.temperature
    if payload.instructions is not None:
        kwargs["instructions"] = payload.instructions
    if payload.stream:
        kwargs["stream"] = True

    try:
        response = await _router_aresponses(RouterManager.get_router(), **kwargs)
    except Exception as exc:
        ctx = _base_context(
            request_id=request_id,
            user=user,
            logical_model=logical_model,
            started_at=started_at,
            stream=payload.stream,
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

    if payload.stream:
        channel_id = _channel_id_from_metadata(litellm_meta)
        upstream_model = litellm_meta.get("deployment")
        return StreamingResponse(
            _stream_responses(
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

    usage = _extract_responses_usage(response)
    ctx = _base_context(
        request_id=request_id,
        user=user,
        logical_model=logical_model,
        started_at=started_at,
        stream=False,
        channel_id=_channel_id_from_metadata(litellm_meta),
        upstream_model=litellm_meta.get("deployment") or _response_model(response),
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
        content=jsonable_encoder(_dump_model(response)),
        headers=_quota_headers(quota_result),
    )


# ---------------------------------------------------------------------------
# Streaming generator for Responses API
# ---------------------------------------------------------------------------


async def _stream_responses(
    *,
    response: Any,
    user: AuthenticatedUser,
    logical_model: Any,
    service: GatewayService,
    started_at: float,
    request_id: str,
    member: str,
    channel_id: int | None,
    upstream_model: str | None,
    rate_limit_rules: list[dict[str, Any]] | None = None,
    quota_reservations: list[QuotaReservation] | None = None,
) -> AsyncIterator[str]:
    """Stream events from litellm.aresponses streaming."""
    import asyncio
    import json

    ctx = _base_context(
        request_id=request_id,
        user=user,
        logical_model=logical_model,
        started_at=started_at,
        stream=True,
        channel_id=channel_id,
        upstream_model=upstream_model,
    )
    status_value = UsageStatus.success.value
    prompt_tokens = 0
    completion_tokens = 0
    try:
        async for event in response:
            data = json.dumps(jsonable_encoder(_dump_model(event)), ensure_ascii=False)
            ctx.record_ttft()
            yield f"data: {data}\n\n"
            # Try to extract usage from final event
            usage = _extract_responses_usage(event)
            if usage["total_tokens"]:
                prompt_tokens = usage["prompt_tokens"]
                completion_tokens = usage["completion_tokens"]
    except asyncio.CancelledError:
        status_value = UsageStatus.error.value
        raise
    except Exception:
        status_value = UsageStatus.error.value
        raise
    finally:
        total_tokens = prompt_tokens + completion_tokens
        ctx.status = status_value
        ctx.set_usage(prompt_tokens, completion_tokens, total_tokens)
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
# Helpers
# ---------------------------------------------------------------------------


async def _check_rate_limits(
    *,
    service: GatewayService,
    user: AuthenticatedUser,
    logical_model_id: int,
    request_id: str,
    member: str,
    is_stream: bool,
) -> list[dict[str, Any]]:
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
            estimated_tokens=ESTIMATED_TOKENS_PER_REQUEST,
            is_stream=is_stream,
        )
        if not rl_result.allowed:
            raise AppError(ErrorCode.rate_limit_exceeded, status.HTTP_429_TOO_MANY_REQUESTS)
    return rules


async def _router_aresponses(router: Any, **kwargs: Any) -> Any:
    aresponses = getattr(router, "aresponses", None)
    if aresponses is not None:
        return await aresponses(**kwargs)
    fallback = getattr(router, "_aresponses_with_streaming_fallbacks", None)
    if fallback is None:
        raise AppError(ErrorCode.model_unavailable, status.HTTP_503_SERVICE_UNAVAILABLE)
    return await fallback(original_function=litellm.aresponses, **kwargs)


def _base_context(
    *,
    request_id: str,
    user: AuthenticatedUser,
    logical_model: Any,
    started_at: float,
    stream: bool,
    channel_id: int | None = None,
    upstream_model: str | None = None,
) -> GatewayRequestContext:
    return GatewayRequestContext(
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
        stream=stream,
        quota_reserved=True,
    )


def _extract_embedding_usage(response: Any) -> dict[str, int]:
    usage = getattr(response, "usage", None)
    if usage is None and isinstance(response, dict):
        usage = response.get("usage")
    prompt_tokens = _usage_value(usage, "prompt_tokens") if usage is not None else 0
    total_tokens = _usage_value(usage, "total_tokens") if usage is not None else 0
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": 0,
        "total_tokens": total_tokens or prompt_tokens,
    }


def _extract_responses_usage(response: Any) -> dict[str, int]:
    usage = getattr(response, "usage", None)
    if usage is None and isinstance(response, dict):
        usage = response.get("usage")
    if usage is None:
        return {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    prompt_tokens = _usage_value(usage, "input_tokens")
    completion_tokens = _usage_value(usage, "output_tokens")
    total_tokens = _usage_value(usage, "total_tokens") or prompt_tokens + completion_tokens
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
    }


def _usage_value(usage: Any, key: str) -> int:
    value = usage.get(key, 0) if isinstance(usage, dict) else getattr(usage, key, 0)
    return int(value or 0)


def _channel_id_from_metadata(litellm_meta: dict[str, Any]) -> int | None:
    model_info = litellm_meta.get("model_info")
    if isinstance(model_info, dict):
        raw_id = model_info.get("id")
        if raw_id is not None:
            try:
                return int(raw_id)
            except (TypeError, ValueError):
                return None
    return None


def _response_model(response: Any) -> str | None:
    model = getattr(response, "model", None)
    if isinstance(model, str):
        return model
    if isinstance(response, dict):
        value = response.get("model")
        return value if isinstance(value, str) else None
    return None


async def _aclose_response(response: Any) -> None:
    aclose = getattr(response, "aclose", None)
    if aclose is None:
        return
    try:
        await aclose()
    except Exception:
        return


def _quota_headers(quota_result: QuotaCheckResult) -> dict[str, str]:
    if not quota_result.warnings:
        return {}
    return {"X-Gateway-Quota-Warnings": str(len(quota_result.warnings))}


async def _fire_usage(
    *,
    user: AuthenticatedUser,
    logical_model: Any,
    status_value: str,
    latency_ms: int | None,
    request_id: str,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    total_tokens: int = 0,
) -> None:
    """Enqueue usage to the durable Redis queue (single write path, GC-safe).

    Awaits a single Redis RPUSH (sub-millisecond, non-blocking); the batch
    worker drains the queue into ``usage_record``.  This replaces the
    fire-and-forget ``asyncio.create_task`` pattern, which could drop writes
    when the task was garbage-collected before completion.
    """
    cost = compute_cost(logical_model, prompt_tokens, completion_tokens)
    await enqueue_usage_event(
        {
            "request_id": request_id,
            "user_id": user.user_id,
            "api_key_id": user.api_key_id,
            "logical_model_id": logical_model.id,
            "logical_model_name": getattr(logical_model, "name", None),
            "channel_id": None,
            "upstream_model": None,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
            "cost": str(cost) if cost is not None else None,
            "status": status_value,
            "latency_ms": latency_ms,
            "cache_hit": False,
            "downgraded_features": None,
        }
    )


def _upstream_error(exc: Exception) -> AppError:
    if hasattr(litellm, "RateLimitError") and isinstance(exc, litellm.RateLimitError):
        return AppError(ErrorCode.upstream_rate_limited, status.HTTP_429_TOO_MANY_REQUESTS)
    if hasattr(litellm, "Timeout") and isinstance(exc, litellm.Timeout):
        return AppError(ErrorCode.upstream_timeout, status.HTTP_504_GATEWAY_TIMEOUT)
    return AppError(ErrorCode.upstream_error, status.HTTP_502_BAD_GATEWAY)


def _usage_status_for_exception(exc: Exception) -> str:
    if hasattr(litellm, "Timeout") and isinstance(exc, litellm.Timeout):
        return UsageStatus.timeout.value
    return UsageStatus.error.value


def _latency_ms(started_at: float) -> int:
    return int((monotonic() - started_at) * 1000)


def _request_id(request: Request) -> str:
    return getattr(request.state, "trace_id", None) or str(uuid4())


def _dump_model(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump()
    return value
