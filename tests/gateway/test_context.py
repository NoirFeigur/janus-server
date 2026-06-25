"""Unit tests for src/gateway/context.py — GatewayRequestContext."""

from __future__ import annotations

import time

from src.enums import UsageStatus
from src.gateway.context import GatewayRequestContext


def test_default_values() -> None:
    ctx = GatewayRequestContext()
    assert ctx.user_id == 0
    assert ctx.status == UsageStatus.success.value
    assert ctx.http_status_code == 200
    assert ctx.stream is False
    assert ctx.cache_hit is False
    assert ctx.prompt_tokens == 0
    assert ctx.completion_tokens == 0
    assert ctx.total_tokens == 0
    assert ctx.request_id  # non-empty UUID hex


def test_record_latency() -> None:
    ctx = GatewayRequestContext()
    # Simulate some elapsed time
    ctx.started_at = time.monotonic() - 0.1  # 100ms ago
    ctx.record_latency()
    assert ctx.latency_ms is not None
    assert ctx.latency_ms >= 90  # At least ~100ms (allow timing variance)


def test_record_ttft_only_once() -> None:
    ctx = GatewayRequestContext()
    ctx.started_at = time.monotonic() - 0.05  # 50ms ago
    ctx.record_ttft()
    first_ttft = ctx.ttft_ms
    assert first_ttft is not None
    assert first_ttft >= 40

    # Second call should not overwrite
    time.sleep(0.01)
    ctx.record_ttft()
    assert ctx.ttft_ms == first_ttft


def test_set_usage() -> None:
    ctx = GatewayRequestContext()
    ctx.set_usage(prompt_tokens=100, completion_tokens=50, total_tokens=150)
    assert ctx.prompt_tokens == 100
    assert ctx.completion_tokens == 50
    assert ctx.total_tokens == 150


def test_mark_error() -> None:
    ctx = GatewayRequestContext()
    ctx.mark_error(
        UsageStatus.error.value,
        http_status_code=502,
        error_code="upstream_error",
        error_body="Bad Gateway" * 500,
    )
    assert ctx.status == UsageStatus.error.value
    assert ctx.http_status_code == 502
    assert ctx.error_code == "upstream_error"
    # Error body truncated to 2048 chars
    assert ctx.error_body is not None
    assert len(ctx.error_body) <= 2048


def test_mark_error_no_body() -> None:
    ctx = GatewayRequestContext()
    ctx.mark_error(UsageStatus.error.value, http_status_code=429)
    assert ctx.error_body is None


def test_unique_request_ids() -> None:
    ctx1 = GatewayRequestContext()
    ctx2 = GatewayRequestContext()
    assert ctx1.request_id != ctx2.request_id
