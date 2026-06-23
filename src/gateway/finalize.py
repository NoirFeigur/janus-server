"""Unified gateway request finalizer.

Called once per gateway request (both streaming and non-streaming) after the
upstream LLM call completes or fails.  Handles:

1. Quota settlement (token-accurate settle or compensate on error).
2. Usage event enqueue (durable Redis queue, replaces asyncio.create_task).
3. Gateway log event enqueue (P1 observability).
4. Prometheus metrics emission.
5. Channel health recording (future: channel_health module).

The finalizer is fail-safe: individual steps catch their own exceptions so a
failure in (say) metrics never prevents usage recording.
"""

from __future__ import annotations

from contextlib import suppress
from decimal import Decimal
from typing import TYPE_CHECKING

from src.core.logging import get_logger
from src.core.metrics import emit_request_metrics
from src.gateway.context import GatewayRequestContext
from src.gateway.events import enqueue_log_event, enqueue_usage_event

if TYPE_CHECKING:
    from src.db.models.model_catalog import LogicalModel
    from src.gateway.service import GatewayService

_log = get_logger(__name__)


async def finalize_gateway_request(
    ctx: GatewayRequestContext,
    *,
    logical_model: LogicalModel | None = None,
    service: GatewayService | None = None,
) -> None:
    """Run all post-request tasks.  Each step is independently fail-safe."""
    # Ensure latency is computed
    if ctx.latency_ms is None:
        ctx.record_latency()

    # 1. Quota settlement
    await _settle_quota(ctx, logical_model=logical_model, service=service)

    # 2. Usage event enqueue
    await _enqueue_usage(ctx, logical_model=logical_model)

    # 3. Gateway log event enqueue
    await _enqueue_log(ctx)

    # 4. Metrics emission
    _emit_metrics(ctx)

    # 5. Channel health recording
    await _record_channel_health(ctx)


# ---------------------------------------------------------------------------
# Internal steps
# ---------------------------------------------------------------------------


async def _settle_quota(
    ctx: GatewayRequestContext,
    *,
    logical_model: LogicalModel | None,
    service: GatewayService | None,
) -> None:
    """Settle quota: compensate on error (no tokens used), settle with actuals on success."""
    if ctx.quota_settled or not ctx.quota_reserved:
        return
    if service is None or logical_model is None:
        return

    with suppress(Exception):
        from src.enums import UsageStatus

        if ctx.status != UsageStatus.success.value or ctx.total_tokens == 0:
            # Error or zero usage: compensate (give back reserved tokens)
            quotas = await service.repo.get_active_quotas(
                ctx.user_id, ctx.department_id, logical_model.id
            )
            await service.quota.compensate(
                ctx.user_id, ctx.department_id, logical_model.id, quotas
            )
        else:
            # Success: settle with actual token count
            cost = _compute_cost(logical_model)
            await service.settle_quota(
                ctx.user_id,
                ctx.department_id,
                logical_model.id,
                ctx.total_tokens,
                cost,
            )
        ctx.quota_settled = True


def _compute_cost(logical_model: LogicalModel) -> Decimal | None:
    """Placeholder — actual cost computation deferred to usage module."""
    # NOTE: This is a simplified reference; full cost computation uses
    # compute_cost() from src.gateway.usage. We import lazily to avoid cycles.
    return None


async def _enqueue_usage(
    ctx: GatewayRequestContext,
    *,
    logical_model: LogicalModel | None,
) -> None:
    """Enqueue a usage record event to the durable Redis queue."""
    with suppress(Exception):
        from src.gateway.usage import compute_cost

        cost: Decimal | None = None
        if logical_model is not None:
            cost = compute_cost(logical_model, ctx.prompt_tokens, ctx.completion_tokens)

        payload = {
            "request_id": ctx.request_id,
            "user_id": ctx.user_id,
            "api_key_id": ctx.api_key_id,
            "logical_model_id": ctx.logical_model_id,
            "logical_model_name": ctx.logical_model_name,
            "channel_id": ctx.channel_id,
            "upstream_model": ctx.upstream_model,
            "prompt_tokens": ctx.prompt_tokens,
            "completion_tokens": ctx.completion_tokens,
            "total_tokens": ctx.total_tokens,
            "cost": str(cost) if cost is not None else None,
            "status": ctx.status,
            "latency_ms": ctx.latency_ms,
            "cache_hit": ctx.cache_hit,
            "downgraded_features": None,
        }
        await enqueue_usage_event(payload)


async def _enqueue_log(ctx: GatewayRequestContext) -> None:
    """Enqueue a gateway request log event for P1 observability."""
    with suppress(Exception):
        payload = {
            "request_id": ctx.request_id,
            "user_id": ctx.user_id,
            "api_key_id": ctx.api_key_id,
            "logical_model_id": ctx.logical_model_id,
            "model": ctx.logical_model_name or ctx.requested_model,
            "channel_id": ctx.channel_id,
            "upstream_model": ctx.upstream_model,
            "provider": ctx.provider,
            "status_code": ctx.http_status_code,
            "error_code": ctx.error_code,
            "error_body": ctx.error_body,
            "retry_count": ctx.retry_count,
            "fallback_used": ctx.fallback_used,
            "latency_ms": ctx.latency_ms,
            "ttft_ms": ctx.ttft_ms,
            "tokens_in": ctx.prompt_tokens,
            "tokens_out": ctx.completion_tokens,
            "cache_hit": ctx.cache_hit,
            "stream": ctx.stream,
        }
        await enqueue_log_event(payload)


def _emit_metrics(ctx: GatewayRequestContext) -> None:
    """Emit Prometheus metrics from the request context."""
    with suppress(Exception):
        latency_s = (ctx.latency_ms / 1000.0) if ctx.latency_ms else 0.0
        ttft_s = (ctx.ttft_ms / 1000.0) if ctx.ttft_ms is not None else None
        emit_request_metrics(
            model=ctx.logical_model_name or ctx.requested_model or "unknown",
            provider=ctx.provider or "unknown",
            status_code=ctx.http_status_code,
            error_code=ctx.error_code or "",
            latency_seconds=latency_s,
            ttft_seconds=ttft_s,
            prompt_tokens=ctx.prompt_tokens,
            completion_tokens=ctx.completion_tokens,
            stream=ctx.stream,
            cache_hit=ctx.cache_hit,
        )


async def _record_channel_health(ctx: GatewayRequestContext) -> None:
    """Record request outcome for channel health tracking (fail-open)."""
    if ctx.channel_id is None:
        return
    with suppress(Exception):
        from src.channel_health.service import ChannelHealthService
        from src.enums import UsageStatus

        svc = ChannelHealthService()
        success = ctx.status == UsageStatus.success.value
        error_class: str | None = None
        if not success:
            error_class = ctx.error_code or ctx.status
        await svc.record_and_evaluate(ctx.channel_id, success=success, error_class=error_class)
