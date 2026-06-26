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
async def test_finalize_enqueues_downgraded_features(fake_redis: AsyncRedisDouble) -> None:
    ctx = GatewayRequestContext(
        user_id=1,
        logical_model_id=10,
        logical_model_name="claude-sonnet",
        downgraded_features=["prompt_caching"],
    )

    await finalize_gateway_request(ctx)

    raw = await fake_redis.lpop(USAGE_QUEUE_KEY)
    assert raw is not None
    payload = json.loads(raw)
    assert payload["downgraded_features"] == ["prompt_caching"]


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
async def test_finalize_uses_actual_tpm_reservation(fake_redis: AsyncRedisDouble) -> None:
    rules = [{"id": 1, "tpm_limit": 10000, "subject_type": "user", "subject_id": 1}]
    ctx = GatewayRequestContext(
        user_id=1,
        request_id="req-tpm-estimated",
        logical_model_id=10,
        total_tokens=300,
        tpm_estimated_tokens=1000,
    )

    with patch(
        "src.gateway.rate_limit.settle_tpm", new_callable=AsyncMock
    ) as mock_settle:
        await finalize_gateway_request(ctx, rate_limit_rules=rules)

    _request_id, _rules, call_delta = mock_settle.await_args[0]
    assert call_delta == 700


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
        prompt_tokens=30,
        completion_tokens=45,
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
async def test_finalize_settles_partial_usage_even_when_stream_errors(
    fake_redis: AsyncRedisDouble,
) -> None:
    """A failed/aborted stream with parsed usage still charges observed tokens."""
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
        prompt_tokens=20,
        completion_tokens=30,
        total_tokens=50,
        quota_reserved=True,
    )
    ctx.mark_error(UsageStatus.error.value)
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

    service.quota.settle_reservations.assert_awaited_once()
    service.quota.compensate_reservations.assert_not_called()


@pytest.mark.asyncio
async def test_finalize_full_refund_on_success_with_zero_tokens(
    fake_redis: AsyncRedisDouble,
) -> None:
    """A SUCCESS whose usage parsed to zero tokens compensates the full
    reservation (NOT a token-settle).

    This locks the billing consequence of the streaming ``usage_unparsed`` hole
    (router._finalize_stream L162): when SSE usage framing can't be parsed the
    stream still reports status=success with total_tokens==0, and
    ``should_compensate`` (finalize.py L105) gives back the full reservation —
    the request is effectively free. The team chose log+refund deliberately;
    this test pins that behavior so a future change can't silently flip it to a
    partial/zero settle (which would leave the reservation leaked) without a
    failing test.
    """
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
        prompt_tokens=0,
        completion_tokens=0,
        total_tokens=0,
        quota_reserved=True,
    )
    # Status stays the default success — this is the unparsed-usage path, NOT an error.
    assert ctx.status == UsageStatus.success.value
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

    service.quota.compensate_reservations.assert_awaited_once_with(reservations)
    service.quota.settle_reservations.assert_not_called()
    assert ctx.quota_settled is True


@pytest.mark.asyncio
async def test_finalize_is_idempotent_does_not_double_settle(
    fake_redis: AsyncRedisDouble,
) -> None:
    """Calling finalize twice settles quota exactly once (quota_settled guard).

    The stream cleanup path and an error path could both reach the finalizer for
    the same request. ``_settle_quota`` early-returns on ``ctx.quota_settled``
    (finalize.py L93), so a second finalize must NOT issue a second
    settle/compensate against Redis — otherwise the counter is adjusted twice and
    the quota is over- or under-charged.
    """
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
        prompt_tokens=30,
        completion_tokens=45,
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
    assert ctx.quota_settled is True

    # Second finalize for the same request must be a no-op for quota settlement.
    await finalize_gateway_request(
        ctx,
        logical_model=logical_model,
        service=service,
        quota_reservations=reservations,
    )

    service.quota.settle_reservations.assert_awaited_once()
    service.quota.compensate_reservations.assert_not_called()


@pytest.mark.asyncio
async def test_finalize_does_not_requery_quota_without_reservations(
    fake_redis: AsyncRedisDouble,
) -> None:
    """No reservations must not fall back to hot-reloading quota rules."""
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

    service.repo.get_active_quotas.assert_not_called()
    service.quota.settle_reservations.assert_not_called()
    service.quota.compensate_reservations.assert_not_called()
    assert ctx.quota_settled is False


# ---------------------------------------------------------------------------
# #9: billing-critical step failures must NOT be silently swallowed — they land
# in a durable billing DLQ (observable + replayable). Telemetry steps may still
# be swallowed.
# ---------------------------------------------------------------------------


def _one_reservation() -> QuotaReservation:
    return QuotaReservation(
        key="quota:u:1:10:2026-06:tokens",
        quota_id=7,
        metric="tokens",
        scope="user",
        enforce=True,
        limit_value=Decimal("100000"),
    )


@pytest.mark.asyncio
async def test_finalize_quota_settle_failure_lands_in_billing_dlq(
    fake_redis: AsyncRedisDouble,
) -> None:
    """Oracle #9: a raised quota settlement (e.g. DB down) was swallowed by
    suppress(Exception) — billing silently lost. It must instead be recorded to
    the billing DLQ for retry/inspection."""
    from src.gateway.events import BILLING_DLQ_KEY, reset_outbox

    reset_outbox()
    ctx = GatewayRequestContext(
        user_id=1,
        request_id="req-q-fail",
        logical_model_id=10,
        prompt_tokens=30,
        completion_tokens=45,
        total_tokens=75,
        quota_reserved=True,
    )
    logical_model = Mock()
    logical_model.id = 10
    logical_model.price_input = None
    logical_model.price_output = None

    service = AsyncMock()
    service.quota = AsyncMock()
    service.quota.settle_reservations.side_effect = RuntimeError("db down")

    await finalize_gateway_request(
        ctx,
        logical_model=logical_model,
        service=service,
        quota_reservations=[_one_reservation()],
    )

    assert await fake_redis.llen(BILLING_DLQ_KEY) == 1
    raw = await fake_redis.lpop(BILLING_DLQ_KEY)
    assert raw is not None
    rec = json.loads(raw)
    assert rec["kind"] == "quota_settle"
    assert rec["request_id"] == "req-q-fail"
    assert "db down" in rec["error"]


@pytest.mark.asyncio
async def test_finalize_tpm_settle_failure_lands_in_billing_dlq(
    fake_redis: AsyncRedisDouble,
) -> None:
    """A raised TPM settlement must be recorded to the billing DLQ, not swallowed."""
    from src.gateway.events import BILLING_DLQ_KEY, reset_outbox

    reset_outbox()
    rules = [{"id": 1, "tpm_limit": 10000, "subject_type": "user", "subject_id": 1}]
    ctx = GatewayRequestContext(
        user_id=1,
        request_id="req-tpm-fail",
        logical_model_id=10,
        total_tokens=50,
    )

    with patch(
        "src.gateway.rate_limit.settle_tpm",
        new_callable=AsyncMock,
        side_effect=RuntimeError("redis settle boom"),
    ):
        await finalize_gateway_request(ctx, rate_limit_rules=rules)

    assert await fake_redis.llen(BILLING_DLQ_KEY) == 1
    raw = await fake_redis.lpop(BILLING_DLQ_KEY)
    assert raw is not None
    rec = json.loads(raw)
    assert rec["kind"] == "tpm_settle"
    assert rec["request_id"] == "req-tpm-fail"


@pytest.mark.asyncio
async def test_finalize_usage_enqueue_failure_lands_in_billing_dlq_and_log_still_enqueued(
    fake_redis: AsyncRedisDouble,
) -> None:
    """A raised usage enqueue is recorded to the billing DLQ AND must not block
    the sibling log enqueue (fail-safe-between-steps contract preserved)."""
    from src.gateway.events import BILLING_DLQ_KEY, reset_outbox

    reset_outbox()
    ctx = GatewayRequestContext(user_id=1, request_id="req-u-fail", logical_model_name="gpt-4")

    with patch(
        "src.gateway.finalize.enqueue_usage_event",
        new_callable=AsyncMock,
        side_effect=RuntimeError("boom"),
    ):
        await finalize_gateway_request(ctx)

    assert await fake_redis.llen(BILLING_DLQ_KEY) == 1
    raw = await fake_redis.lpop(BILLING_DLQ_KEY)
    assert raw is not None
    assert json.loads(raw)["kind"] == "usage_enqueue"
    # Observability step still ran despite the billing failure.
    assert await fake_redis.llen(LOG_QUEUE_KEY) == 1


@pytest.mark.asyncio
async def test_finalize_telemetry_failure_is_still_swallowed(
    fake_redis: AsyncRedisDouble,
) -> None:
    """Telemetry/observability failures (metrics) stay swallowed — they must not
    land in the billing DLQ nor abort finalize."""
    from src.gateway.events import BILLING_DLQ_KEY, reset_outbox

    reset_outbox()
    ctx = GatewayRequestContext(user_id=1, logical_model_name="gpt-4")

    with patch(
        "src.gateway.finalize.emit_request_metrics",
        side_effect=RuntimeError("metrics down"),
    ):
        # Must not raise.
        await finalize_gateway_request(ctx)

    assert await fake_redis.llen(BILLING_DLQ_KEY) == 0
