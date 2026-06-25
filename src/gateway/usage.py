"""Usage cost computation for the gateway hot path.

Internal cost points (not USD) for a completed LLM call. The durable usage
write path lives in ``events.py`` (Redis queue → batch worker → ``usage_record``).
"""

from __future__ import annotations

from decimal import Decimal

from src.db.models.model_catalog import LogicalModel


def compute_cost(model: LogicalModel, prompt_tokens: int, completion_tokens: int) -> Decimal | None:
    """Compute internal cost points for a logical model, if priced."""
    if model.price_input is None or model.price_output is None:
        return None
    prompt_cost = Decimal(prompt_tokens) * model.price_input
    completion_cost = Decimal(completion_tokens) * model.price_output
    return (prompt_cost + completion_cost) / Decimal(1_000_000)
