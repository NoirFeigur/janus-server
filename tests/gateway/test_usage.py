from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.db.models.model_catalog import LogicalModel
from src.db.models.usage import UsageRecord
from src.enums import UsageStatus
from src.gateway.usage import UsageData, compute_cost, record_usage

pytestmark = pytest.mark.asyncio


async def test_compute_cost_with_prices(seed_logical_model) -> None:
    model = await seed_logical_model(
        price_input=Decimal("3.000000"), price_output=Decimal("15.000000")
    )

    cost = compute_cost(model, prompt_tokens=1000, completion_tokens=2000)

    assert cost == Decimal("0.033000")


async def test_compute_cost_returns_none_without_prices() -> None:
    model = LogicalModel(name="free", display_name="free")

    assert compute_cost(model, prompt_tokens=1000, completion_tokens=2000) is None


async def test_record_usage_inserts_row(
    monkeypatch: pytest.MonkeyPatch,
    gateway_session: AsyncSession,
    sqlite_session_factory: async_sessionmaker[AsyncSession],
    seed_logical_model,
) -> None:
    model = await seed_logical_model()
    await gateway_session.commit()
    monkeypatch.setattr("src.gateway.usage.async_session_factory", sqlite_session_factory)

    await record_usage(
        UsageData(
            user_id=100,
            api_key_id=200,
            logical_model=model,
            channel_id=300,
            upstream_model="claude-3-5-sonnet",
            prompt_tokens=100,
            completion_tokens=20,
            total_tokens=120,
            status=UsageStatus.success.value,
            latency_ms=42,
            request_id="req-1",
            downgraded_features=["temperature"],
        )
    )

    row = await gateway_session.scalar(
        select(UsageRecord).where(UsageRecord.request_id == "req-1")
    )
    assert row is not None
    assert row.user_id == 100
    assert row.cost == Decimal("0.000600")


async def test_record_usage_swallows_exceptions(
    monkeypatch: pytest.MonkeyPatch,
    seed_logical_model,
) -> None:
    model = await seed_logical_model()

    class RaisingFactory:
        def __call__(self) -> RaisingFactory:
            return self

        async def __aenter__(self) -> RaisingFactory:
            raise RuntimeError("session failed")

        async def __aexit__(self, *_: object) -> None:
            return None

    monkeypatch.setattr("src.gateway.usage.async_session_factory", RaisingFactory())

    await record_usage(
        UsageData(
            user_id=100,
            api_key_id=None,
            logical_model=model,
            channel_id=None,
            upstream_model=None,
            prompt_tokens=0,
            completion_tokens=0,
            total_tokens=0,
            status=UsageStatus.error.value,
            latency_ms=None,
            request_id="req-error",
            downgraded_features=None,
        )
    )
