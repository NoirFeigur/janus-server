"""Prometheus metrics facade for Janus gateway.

Wraps ``prometheus_client`` behind a thin API so all metric definitions live in
one place.  The ``/metrics`` route is exposed via ``setup_metrics_route(app)``.

If ``prometheus_client`` is unavailable at runtime (dev without the dep), all
operations degrade to no-ops — the gateway never fails due to missing metrics.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from prometheus_client import (
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)
from starlette import status
from starlette.requests import Request
from starlette.responses import Response

if TYPE_CHECKING:
    from fastapi import FastAPI

# ---------------------------------------------------------------------------
# Registry (use the default global registry for simplicity)
# ---------------------------------------------------------------------------

REGISTRY = CollectorRegistry(auto_describe=True)

# ---------------------------------------------------------------------------
# Gateway metrics
# ---------------------------------------------------------------------------

GATEWAY_REQUESTS_TOTAL = Counter(
    "janus_gateway_requests_total",
    "Total gateway LLM requests.",
    ["model", "provider", "status_code", "error_code"],
    registry=REGISTRY,
)

GATEWAY_LATENCY_SECONDS = Histogram(
    "janus_gateway_request_latency_seconds",
    "Gateway request latency in seconds.",
    ["model", "provider", "status_code"],
    buckets=(0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 300),
    registry=REGISTRY,
)

GATEWAY_TTFT_SECONDS = Histogram(
    "janus_gateway_ttft_seconds",
    "Time to first token for streaming requests.",
    ["model", "provider"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30),
    registry=REGISTRY,
)

GATEWAY_TOKENS_TOTAL = Counter(
    "janus_gateway_tokens_total",
    "Total tokens processed (prompt + completion).",
    ["model", "provider", "direction"],
    registry=REGISTRY,
)

GATEWAY_ACTIVE_STREAMS = Gauge(
    "janus_gateway_active_streams",
    "Currently active streaming requests.",
    ["model", "provider"],
    registry=REGISTRY,
)

GATEWAY_ERRORS_TOTAL = Counter(
    "janus_gateway_errors_total",
    "Total gateway errors by provider and error code.",
    ["provider", "error_code", "status_code"],
    registry=REGISTRY,
)

GATEWAY_CACHE_HITS_TOTAL = Counter(
    "janus_gateway_cache_hits_total",
    "Response cache hits.",
    ["model"],
    registry=REGISTRY,
)


# ---------------------------------------------------------------------------
# Convenience emit function (called from finalizer)
# ---------------------------------------------------------------------------


def emit_request_metrics(
    *,
    model: str,
    provider: str,
    status_code: int,
    error_code: str,
    latency_seconds: float,
    ttft_seconds: float | None,
    prompt_tokens: int,
    completion_tokens: int,
    stream: bool,
    cache_hit: bool,
) -> None:
    """Emit all per-request metrics from a completed gateway context."""
    sc = str(status_code)
    GATEWAY_REQUESTS_TOTAL.labels(
        model=model, provider=provider, status_code=sc, error_code=error_code
    ).inc()
    GATEWAY_LATENCY_SECONDS.labels(
        model=model, provider=provider, status_code=sc
    ).observe(latency_seconds)

    if ttft_seconds is not None:
        GATEWAY_TTFT_SECONDS.labels(model=model, provider=provider).observe(ttft_seconds)

    if prompt_tokens > 0:
        GATEWAY_TOKENS_TOTAL.labels(
            model=model, provider=provider, direction="prompt"
        ).inc(prompt_tokens)
    if completion_tokens > 0:
        GATEWAY_TOKENS_TOTAL.labels(
            model=model, provider=provider, direction="completion"
        ).inc(completion_tokens)

    if error_code:
        GATEWAY_ERRORS_TOTAL.labels(
            provider=provider, error_code=error_code, status_code=sc
        ).inc()

    if cache_hit:
        GATEWAY_CACHE_HITS_TOTAL.labels(model=model).inc()


# ---------------------------------------------------------------------------
# Route setup
# ---------------------------------------------------------------------------


def setup_metrics_route(app: FastAPI) -> None:
    """Mount ``GET /metrics`` on the FastAPI app (Prometheus scrape endpoint)."""

    @app.get("/metrics", include_in_schema=False)
    async def metrics_endpoint(_request: Request) -> Response:
        body = generate_latest(REGISTRY)
        return Response(
            content=body,
            status_code=status.HTTP_200_OK,
            media_type="text/plain; version=0.0.4; charset=utf-8",
        )
