from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from redis.exceptions import RedisError

from src.auth.service import AuthenticatedUser
from src.db.models.model_catalog import LogicalModel
from src.db.models.quota import Quota
from src.db.models.rate_limit import RateLimitRule
from src.enums import ErrorCode, QuotaMetric, QuotaPeriod, QuotaScope
from src.exceptions import AppError
from src.gateway.quota import QuotaCheckResult, QuotaExceeded, QuotaLimitExceeded
from src.gateway.service import GatewayService

pytestmark = pytest.mark.asyncio


def _user() -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id=100,
        username="alice",
        department_id=200,
        permissions=frozenset(),
    )


def _model() -> LogicalModel:
    return LogicalModel(id=10, name="claude-sonnet", display_name="Claude Sonnet")


def _service() -> GatewayService:
    service = GatewayService(session=object())
    service.repo = AsyncMock()
    service.quota = AsyncMock()
    return service


async def test_resolve_model_found_and_granted() -> None:
    service = _service()
    model = _model()
    service.repo.get_logical_model_by_name.return_value = model
    service.repo.get_user_granted_models.return_value = {model.id}

    resolved = await service.resolve_model(_user(), "claude-sonnet")

    assert resolved is model


async def test_resolve_model_not_found_raises_404() -> None:
    service = _service()
    service.repo.get_logical_model_by_name.return_value = None

    with pytest.raises(AppError) as exc_info:
        await service.resolve_model(_user(), "missing")

    assert exc_info.value.code == ErrorCode.model_not_found
    assert exc_info.value.status_code == 404


async def test_resolve_model_not_granted_raises_403() -> None:
    service = _service()
    model = _model()
    service.repo.get_logical_model_by_name.return_value = model
    service.repo.get_user_granted_models.return_value = set()

    with pytest.raises(AppError) as exc_info:
        await service.resolve_model(_user(), "claude-sonnet")

    assert exc_info.value.code == ErrorCode.model_not_granted
    assert exc_info.value.status_code == 403
    assert exc_info.value.params == {"model": "claude-sonnet"}


async def test_resolve_model_uses_default_when_none_requested() -> None:
    service = _service()
    model = _model()
    service.repo.get_default_model_id.return_value = model.id
    service.repo.get_logical_model_by_id.return_value = model
    service.repo.get_user_granted_models.return_value = {model.id}

    resolved = await service.resolve_model(_user(), None)

    assert resolved is model
    service.repo.get_logical_model_by_id.assert_awaited_once_with(model.id)


async def test_resolve_model_no_default_raises_404() -> None:
    service = _service()
    service.repo.get_default_model_id.return_value = None

    with pytest.raises(AppError) as exc_info:
        await service.resolve_model(_user(), None)

    assert exc_info.value.code == ErrorCode.model_not_found
    assert exc_info.value.status_code == 404


async def test_check_quota_delegates_to_enforcer() -> None:
    service = _service()
    quota = Quota(
        id=1,
        scope=QuotaScope.user.value,
        scope_id=100,
        logical_model_id=10,
        period=QuotaPeriod.daily.value,
        metric=QuotaMetric.requests.value,
        limit_value=Decimal("10"),
    )
    result = QuotaCheckResult(passed=True)
    service.repo.get_active_quotas.return_value = [quota]
    service.quota.check_and_increment.return_value = result

    checked = await service.check_quota(100, None, 10)

    assert checked is result
    service.quota.check_and_increment.assert_awaited_once_with(
        100, None, 10, [quota], estimated_tokens=0, estimated_cost=None
    )


async def test_check_quota_exceeded_raises_429() -> None:
    service = _service()
    quota = Quota(
        id=1,
        scope=QuotaScope.user.value,
        scope_id=100,
        logical_model_id=10,
        period=QuotaPeriod.daily.value,
        metric=QuotaMetric.requests.value,
        limit_value=Decimal("1"),
    )
    service.repo.get_active_quotas.return_value = [quota]
    service.quota.check_and_increment.side_effect = QuotaLimitExceeded(
        QuotaExceeded(quota=quota, current=Decimal("2"))
    )

    with pytest.raises(AppError) as exc_info:
        await service.check_quota(100, None, 10)

    assert exc_info.value.code == ErrorCode.quota_exceeded
    assert exc_info.value.status_code == 429
    assert exc_info.value.params["current"] == "2"


async def test_check_quota_redis_down_fails_closed_503() -> None:
    """E: a Redis outage during the quota check must fail CLOSED (503), not 500.

    Quota is spend control; allowing traffic through blind would risk unbounded
    overspend. The pre-flight check translates RedisError to a transient 503.
    """
    service = _service()
    quota = Quota(
        id=1,
        scope=QuotaScope.user.value,
        scope_id=100,
        logical_model_id=10,
        period=QuotaPeriod.daily.value,
        metric=QuotaMetric.requests.value,
        limit_value=Decimal("10"),
    )
    service.repo.get_active_quotas.return_value = [quota]
    service.quota.check_and_increment.side_effect = RedisError("connection refused")

    with pytest.raises(AppError) as exc_info:
        await service.check_quota(100, None, 10)

    assert exc_info.value.code == ErrorCode.service_unavailable
    assert exc_info.value.status_code == 503


async def test_get_rate_limit_rules_includes_api_key_and_burst(
    gateway_session,
) -> None:
    service = GatewayService(gateway_session)
    gateway_session.add_all(
        [
            RateLimitRule(
                subject_type="api_key",
                subject_id=900,
                logical_model_id=10,
                tpm_limit=100,
                tpm_burst_limit=250,
                status="active",
                enforce=True,
            ),
            RateLimitRule(
                subject_type="api_key",
                subject_id=901,
                logical_model_id=10,
                rpm_limit=1,
                status="active",
                enforce=True,
            ),
        ]
    )
    await gateway_session.flush()

    rules = await service.get_rate_limit_rules(
        user_id=100,
        department_id=200,
        logical_model_id=10,
        api_key_id=900,
    )

    assert len(rules) == 1
    assert rules[0]["subject_type"] == "api_key"
    assert rules[0]["subject_id"] == 900
    assert rules[0]["tpm_burst_limit"] == 250
