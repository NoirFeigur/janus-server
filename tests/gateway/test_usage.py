from __future__ import annotations

from decimal import Decimal

import pytest

from src.db.models.model_catalog import LogicalModel
from src.gateway.usage import compute_cost

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
