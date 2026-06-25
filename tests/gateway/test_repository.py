from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models.model_catalog import LogicalModel
from src.db.models.quota import Quota
from src.enums import (
    ActiveStatus,
    ChannelStatus,
    GrantScope,
    QuotaMetric,
    QuotaPeriod,
    QuotaScope,
)
from src.gateway.repository import GatewayRepository

pytestmark = pytest.mark.asyncio


async def test_get_router_config_returns_cartesian_expansion(
    gateway_session: AsyncSession,
    seed_logical_model,
    seed_channel,
    seed_deployment,
) -> None:
    model = await seed_logical_model()
    channel, keys = await seed_channel(key_count=2)
    await seed_deployment(model=model, channel=channel)

    rows = await GatewayRepository(gateway_session).get_router_config()

    assert len(rows) == 2
    assert {row.channel_key_id for row in rows} == {key.id for key in keys}
    assert {row.logical_model_name for row in rows} == {"claude-sonnet"}


async def test_get_router_config_excludes_disabled_channel(
    gateway_session: AsyncSession,
    seed_logical_model,
    seed_channel,
    seed_deployment,
) -> None:
    model = await seed_logical_model()
    channel, _ = await seed_channel(channel_status=ChannelStatus.disabled.value)
    await seed_deployment(model=model, channel=channel)

    rows = await GatewayRepository(gateway_session).get_router_config()

    assert rows == []


async def test_get_router_config_excludes_deleted_deployment(
    gateway_session: AsyncSession,
    seed_logical_model,
    seed_channel,
    seed_deployment,
) -> None:
    model = await seed_logical_model()
    channel, _ = await seed_channel()
    await seed_deployment(model=model, channel=channel, is_deleted=True)

    rows = await GatewayRepository(gateway_session).get_router_config()

    assert rows == []


async def test_get_user_granted_models_user_scope(
    gateway_session: AsyncSession,
    seed_logical_model,
    seed_grant,
) -> None:
    model = await seed_logical_model()
    await seed_grant(logical_model_id=model.id, scope_id=100)

    granted = await GatewayRepository(gateway_session).get_user_granted_models(100, None)

    assert granted == {model.id}


async def test_get_user_granted_models_union_user_and_dept(
    gateway_session: AsyncSession,
    seed_logical_model,
    seed_grant,
) -> None:
    user_model = await seed_logical_model(name="user-model")
    dept_model = await seed_logical_model(name="dept-model")
    await seed_grant(logical_model_id=user_model.id, scope_id=100)
    await seed_grant(
        logical_model_id=dept_model.id,
        scope=GrantScope.department.value,
        scope_id=200,
    )

    granted = await GatewayRepository(gateway_session).get_user_granted_models(100, 200)

    assert granted == {user_model.id, dept_model.id}


async def test_get_default_model_id_user_scope_overrides_dept(
    gateway_session: AsyncSession,
    seed_logical_model,
    seed_grant,
) -> None:
    user_model = await seed_logical_model(name="user-default")
    dept_model = await seed_logical_model(name="dept-default")
    await seed_grant(
        logical_model_id=dept_model.id,
        scope=GrantScope.department.value,
        scope_id=200,
        is_default=True,
    )
    await seed_grant(logical_model_id=user_model.id, scope_id=100, is_default=True)

    default_id = await GatewayRepository(gateway_session).get_default_model_id(100, 200)

    assert default_id == user_model.id


async def test_get_default_model_id_returns_none_when_no_default(
    gateway_session: AsyncSession,
    seed_logical_model,
    seed_grant,
) -> None:
    model = await seed_logical_model()
    await seed_grant(logical_model_id=model.id, scope_id=100, is_default=False)

    default_id = await GatewayRepository(gateway_session).get_default_model_id(100, None)

    assert default_id is None


async def test_get_active_quotas_matches_user_and_global(
    gateway_session: AsyncSession,
    seed_logical_model,
) -> None:
    model = await seed_logical_model()
    matching_user = Quota(
        scope=QuotaScope.user.value,
        scope_id=100,
        logical_model_id=model.id,
        period=QuotaPeriod.daily.value,
        metric=QuotaMetric.requests.value,
        limit_value=Decimal("10"),
    )
    matching_global = Quota(
        scope=QuotaScope.global_.value,
        scope_id=None,
        logical_model_id=None,
        period=QuotaPeriod.monthly.value,
        metric=QuotaMetric.tokens.value,
        limit_value=Decimal("1000"),
    )
    wrong_user = Quota(
        scope=QuotaScope.user.value,
        scope_id=101,
        logical_model_id=model.id,
        period=QuotaPeriod.daily.value,
        metric=QuotaMetric.requests.value,
        limit_value=Decimal("1"),
    )
    disabled = Quota(
        scope=QuotaScope.global_.value,
        scope_id=None,
        logical_model_id=None,
        period=QuotaPeriod.total.value,
        metric=QuotaMetric.cost.value,
        limit_value=Decimal("1"),
        status=ActiveStatus.disabled.value,
    )
    gateway_session.add_all([matching_user, matching_global, wrong_user, disabled])
    await gateway_session.flush()

    quotas = await GatewayRepository(gateway_session).get_active_quotas(100, None, model.id)

    assert {quota.id for quota in quotas} == {matching_user.id, matching_global.id}


async def test_get_logical_model_by_name_excludes_disabled(
    gateway_session: AsyncSession,
) -> None:
    gateway_session.add(
        LogicalModel(
            name="disabled-model",
            display_name="disabled-model",
            status=ActiveStatus.disabled.value,
        )
    )
    await gateway_session.flush()

    model = await GatewayRepository(gateway_session).get_logical_model_by_name(
        "disabled-model"
    )

    assert model is None
