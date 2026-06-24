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
from src.gateway.dependencies import get_gateway_service
from src.gateway.events import enqueue_usage_event
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
    logical_model = await service.resolve_model(user, payload.model)
    await service.check_quota(user.user_id, user.department_id, logical_model.id)

    started_at = monotonic()
    llm_router = RouterManager.get_router()
    kwargs: dict[str, Any] = {
        "model": logical_model.name,
        "input": payload.input,
    }
    if payload.encoding_format is not None:
        kwargs["encoding_format"] = payload.encoding_format
    if payload.dimensions is not None:
        kwargs["dimensions"] = payload.dimensions

    try:
        response = await llm_router.aembedding(**kwargs)
    except Exception as exc:
        await _fire_usage(
            user=user,
            logical_model=logical_model,
            status_value=UsageStatus.error.value,
            latency_ms=_latency_ms(started_at),
            request_id=request_id,
        )
        raise _upstream_error(exc) from exc

    # Extract usage from response
    usage = getattr(response, "usage", None)
    prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0) if usage else 0
    total_tokens = int(getattr(usage, "total_tokens", 0) or 0) if usage else 0

    await service.settle_quota(
        user.user_id,
        user.department_id,
        logical_model.id,
        total_tokens,
        compute_cost(logical_model, prompt_tokens, 0),
    )
    await _fire_usage(
        user=user,
        logical_model=logical_model,
        prompt_tokens=prompt_tokens,
        total_tokens=total_tokens,
        status_value=UsageStatus.success.value,
        latency_ms=_latency_ms(started_at),
        request_id=request_id,
    )

    return JSONResponse(content=jsonable_encoder(_dump_model(response)))


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
    for model_id in grants:
        model = await service.repo.get_logical_model_by_id(model_id)
        if model is not None:
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
    logical_model = await service.resolve_model(user, payload.model)
    await service.check_quota(user.user_id, user.department_id, logical_model.id)

    started_at = monotonic()
    kwargs: dict[str, Any] = {
        "model": logical_model.name,
        "input": payload.input,
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
        response = await litellm.aresponses(**kwargs)
    except Exception as exc:
        await _fire_usage(
            user=user,
            logical_model=logical_model,
            status_value=UsageStatus.error.value,
            latency_ms=_latency_ms(started_at),
            request_id=request_id,
        )
        raise _upstream_error(exc) from exc

    if payload.stream:
        return StreamingResponse(
            _stream_responses(
                response=response,
                user=user,
                logical_model=logical_model,
                service=service,
                started_at=started_at,
                request_id=request_id,
            ),
            media_type="text/event-stream",
        )

    # Non-streaming: extract usage and return
    usage = getattr(response, "usage", None)
    prompt_tokens = int(getattr(usage, "input_tokens", 0) or 0) if usage else 0
    completion_tokens = int(getattr(usage, "output_tokens", 0) or 0) if usage else 0
    total_tokens = prompt_tokens + completion_tokens

    await service.settle_quota(
        user.user_id,
        user.department_id,
        logical_model.id,
        total_tokens,
        compute_cost(logical_model, prompt_tokens, completion_tokens),
    )
    await _fire_usage(
        user=user,
        logical_model=logical_model,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
        status_value=UsageStatus.success.value,
        latency_ms=_latency_ms(started_at),
        request_id=request_id,
    )

    return JSONResponse(content=jsonable_encoder(_dump_model(response)))


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
) -> AsyncIterator[str]:
    """Stream events from litellm.aresponses streaming."""
    import asyncio
    import json

    status_value = UsageStatus.success.value
    prompt_tokens = 0
    completion_tokens = 0
    try:
        async for event in response:
            data = json.dumps(jsonable_encoder(_dump_model(event)), ensure_ascii=False)
            yield f"data: {data}\n\n"
            # Try to extract usage from final event
            usage = getattr(event, "usage", None)
            if usage:
                prompt_tokens = int(getattr(usage, "input_tokens", 0) or 0)
                completion_tokens = int(getattr(usage, "output_tokens", 0) or 0)
    except asyncio.CancelledError:
        status_value = UsageStatus.error.value
        raise
    except Exception:
        status_value = UsageStatus.error.value
        raise
    finally:
        total_tokens = prompt_tokens + completion_tokens
        if status_value == UsageStatus.success.value and total_tokens > 0:
            from src.gateway.service import settle_quota_independent

            await settle_quota_independent(
                user_id=user.user_id,
                department_id=user.department_id,
                logical_model_id=logical_model.id,
                actual_tokens=total_tokens,
                actual_cost=compute_cost(logical_model, prompt_tokens, completion_tokens),
            )
        await _fire_usage(
            user=user,
            logical_model=logical_model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            status_value=status_value,
            latency_ms=_latency_ms(started_at),
            request_id=request_id,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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


def _latency_ms(started_at: float) -> int:
    return int((monotonic() - started_at) * 1000)


def _request_id(request: Request) -> str:
    return getattr(request.state, "trace_id", None) or str(uuid4())


def _dump_model(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump()
    return value
