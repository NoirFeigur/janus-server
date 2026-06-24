"""Unit tests for src/gateway/finalize.py — unified gateway request finalizer."""

from __future__ import annotations

import json
from decimal import Decimal
from unittest.mock import AsyncMock, Mock, patch

import pytest

from src.enums import UsageStatus
from src.gateway.context import GatewayRequestContext
from src.gateway.events import LOG_QUEUE_KEY, USAGE_QUEUE_KEY
from src.gateway.finalize import finalize_gateway_request
from src.gateway.quota import QuotaReservation
from tests._async_redis_double import AsyncRedisDouble


@pytest.mark.asyncio
async def test_finalize_enqueues_usage_event(fake_redis: AsyncRedisDouble) -> None:
    ctx = GatewayRequestContext(
        user_id=1,
        logical_model_id=10,
        logical_model_name="gpt-4",
        channel_id=5,
        upstream_model="gpt-4-0125",
        provider="openai",
        prompt_tokens=50,
        completion_tokens=25,
        total_tokens=75,
    )

    await finalize_gateway_request(ctx)

    assert await fake_redis.llen(USAGE_QUEUE_KEY) == 1
    raw = await fake_redis.lpop(USAGE_QUEUE_KEY)
    assert raw is not None
    payload = json.loads(raw)
    assert payload["user_id"] == 1
    assert payload["logical_model_id"] == 10
    assert payload["prompt_tokens"] == 50
    assert payload["completion_tokens"] == 25


@pytest.mark.asyncio
async def test_finalize_enqueues_log_event(fake_redis: AsyncRedisDouble) -> None:
    ctx = GatewayRequestContext(
        user_id=2,
        request_id="req-abc",
        logical_model_name="claude-3",
        provider="anthropic",
        http_status_code=200,
        stream=True,
    )

    await finalize_gateway_request(ctx)

    assert await fake_redis.llen(LOG_QUEUE_KEY) == 1
    raw = await fake_redis.lpop(LOG_QUEUE_KEY)
    assert raw is not None
    payload = json.loads(raw)
    assert payload["request_id"] == "req-abc"
    assert payload["provider"] == "anthropic"
    assert payload["stream"] is True


@pytest.mark.asyncio
async def test_finalize_computes_latency(fake_redis: AsyncRedisDouble) -> None:
    ctx = GatewayRequestContext(user_id=1)
    # latency_ms should be computed during finalize
    assert ctx.latency_ms is None
    await finalize_gateway_request(ctx)
    assert ctx.latency_ms is not None
    assert ctx.latency_ms >= 0


@pytest.mark.asyncio
async def test_finalize_error_context(fake_redis: AsyncRedisDouble) -> None:
    ctx = GatewayRequestContext(
        user_id=3,
        status=UsageStatus.error.value,
        http_status_code=502,
        error_code="upstream_error",
        error_body="Bad Gateway",
    )

    await finalize_gateway_request(ctx)

    raw = await fake_redis.lpop(LOG_QUEUE_KEY)
    assert raw is not None
    payload = json.loads(raw)
    assert payload["status_code"] == 502
    assert payload["error_code"] == "upstream_error"
    assert payload["error_body"] == "Bad Gateway"


@pytest.mark.asyncio
async def test_finalize_quota_settlement_skipped_without_service(
    fake_redis: AsyncRedisDouble,
) -> None:
    """Quota settlement skipped when service/model not provided (no error)."""
    ctx = GatewayRequestContext(user_id=1, quota_reserved=True)
    # Should not raise
    await finalize_gateway_request(ctx)
    assert ctx.quota_settled is False


@pytest.mark.asyncio
async def test_finalize_individual_step_failure_does_not_block_others(
    fake_redis: AsyncRedisDouble,
) -> None:
    """If usage enqueue fails, log enqueue should still succeed."""
    ctx = GatewayRequestContext(user_id=1, logical_model_name="gpt-4")

    with patch(
        "src.gateway.finalize.enqueue_usage_event",
        new_callable=AsyncMock,
        side_effect=RuntimeError("boom"),
    ):
        await finalize_gateway_request(ctx)

    # Log event should still be enqueued despite usage failure
    assert await fake_redis.llen(LOG_QUEUE_KEY) == 1


# ---------------------------------------------------------------------------
# M6 / A: TPM reservation settlement (reconcile estimate vs actual, both ways)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_finalize_refunds_unused_tpm(fake_redis: AsyncRedisDouble) -> None:
    """Actual usage below the upfront reservation settles a positive delta (refund)."""
    from src.gateway.rate_limit import ESTIMATED_TOKENS_PER_REQUEST

    rules = [{"id": 1, "tpm_limit": 10000, "subject_type": "user", "subject_id": 1}]
    ctx = GatewayRequestContext(
        user_id=1,
        request_id="req-tpm",
        logical_model_id=10,
        total_tokens=30,  # below the 100-token reservation
    )

    with patch(
        "src.gateway.rate_limit.settle_tpm", new_callable=AsyncMock
    ) as mock_settle:
        await finalize_gateway_request(ctx, rate_limit_rules=rules)

    mock_settle.assert_awaited_once()
    call_request_id, call_rules, call_delta = mock_settle.await_args[0]
    assert call_request_id == "req-tpm"
    assert call_rules == rules
    assert call_delta == ESTIMATED_TOKENS_PER_REQUEST - 30  # positive refund


@pytest.mark.asyncio
async def test_finalize_deducts_tpm_overage_when_usage_exceeds_estimate(
    fake_redis: AsyncRedisDouble,
) -> None:
    """Actual usage above the reservation settles a NEGATIVE delta (extra deduction).

    Regression for bug A: the old refund-only path skipped this case entirely, so
    over-consumption beyond the flat estimate was never billed to the TPM bucket.
    """
    from src.gateway.rate_limit import ESTIMATED_TOKENS_PER_REQUEST

    rules = [{"id": 1, "tpm_limit": 10000, "subject_type": "user", "subject_id": 1}]
    ctx = GatewayRequestContext(
        user_id=1,
        request_id="req-tpm2",
        logical_model_id=10,
        total_tokens=5000,  # well above the 100-token reservation
    )

    with patch(
        "src.gateway.rate_limit.settle_tpm", new_callable=AsyncMock
    ) as mock_settle:
        await finalize_gateway_request(ctx, rate_limit_rules=rules)

    mock_settle.assert_awaited_once()
    _request_id, _rules, call_delta = mock_settle.await_args[0]
    assert call_delta == ESTIMATED_TOKENS_PER_REQUEST - 5000  # negative deduction
    assert call_delta < 0


@pytest.mark.asyncio
async def test_finalize_no_tpm_settle_without_rules(
    fake_redis: AsyncRedisDouble,
) -> None:
    """No rate-limit rules means no TPM reservation to settle."""
    ctx = GatewayRequestContext(user_id=1, total_tokens=10)

    with patch(
        "src.gateway.rate_limit.settle_tpm", new_callable=AsyncMock
    ) as mock_settle:
        await finalize_gateway_request(ctx)

    mock_settle.assert_not_called()


# ---------------------------------------------------------------------------
# M1: reservation-based settlement preferred over legacy re-query
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_finalize_settles_against_captured_reservations(
    fake_redis: AsyncRedisDouble,
) -> None:
    """When reservations are captured, settle targets them (no re-query)."""
    reservations = [
        QuotaReservation(
            key="quota:u:1:10:2026-06:tokens",
            quota_id=7,
            metric="tokens",
            scope="user",
            enforce=True,
            limit_value=Decimal("100000"),
        )
    ]
    ctx = GatewayRequestContext(
        user_id=1,
        logical_model_id=10,
        total_tokens=75,
        quota_reserved=True,
    )
    logical_model = Mock()
    logical_model.id = 10
    logical_model.price_input = None
    logical_model.price_output = None

    service = AsyncMock()
    service.quota = AsyncMock()
    service.repo = AsyncMock()

    await finalize_gateway_request(
        ctx,
        logical_model=logical_model,
        service=service,
        quota_reservations=reservations,
    )

    # Reservation-based settle used; legacy re-query NOT used.
    service.quota.settle_reservations.assert_awaited_once()
    service.repo.get_active_quotas.assert_not_called()
    service.settle_quota.assert_not_called()
    assert ctx.quota_settled is True


@pytest.mark.asyncio
async def test_finalize_compensates_reservations_on_error(
    fake_redis: AsyncRedisDouble,
) -> None:
    """Error/zero-token requests compensate the captured reservations."""
    reservations = [
        QuotaReservation(
            key="quota:u:1:10:2026-06:tokens",
            quota_id=7,
            metric="tokens",
            scope="user",
            enforce=True,
            limit_value=Decimal("100000"),
        )
    ]
    ctx = GatewayRequestContext(
        user_id=1,
        logical_model_id=10,
        total_tokens=0,
        quota_reserved=True,
    )
    ctx.mark_error(UsageStatus.error.value)
    logical_model = Mock()
    logical_model.id = 10

    service = AsyncMock()
    service.quota = AsyncMock()
    service.repo = AsyncMock()

    await finalize_gateway_request(
        ctx,
        logical_model=logical_model,
        service=service,
        quota_reservations=reservations,
    )

    service.quota.compensate_reservations.assert_awaited_once_with(reservations)
    service.quota.settle_reservations.assert_not_called()
    service.repo.get_active_quotas.assert_not_called()


@pytest.mark.asyncio
async def test_finalize_falls_back_to_legacy_settle_without_reservations(
    fake_redis: AsyncRedisDouble,
) -> None:
    """No reservations (e.g. cache-hit synthetic ctx) uses the legacy re-query path."""
    ctx = GatewayRequestContext(
        user_id=1,
        logical_model_id=10,
        total_tokens=75,
        quota_reserved=True,
    )
    logical_model = Mock()
    logical_model.id = 10
    logical_model.price_input = None
    logical_model.price_output = None

    service = AsyncMock()
    service.quota = AsyncMock()
    service.repo = AsyncMock()

    await finalize_gateway_request(
        ctx,
        logical_model=logical_model,
        service=service,
    )

    # Legacy re-query settle used; reservation-based NOT used.
    service.settle_quota.assert_awaited_once()
    service.quota.settle_reservations.assert_not_called()
