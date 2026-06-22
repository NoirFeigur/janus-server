"""Usage recording — write usage_record after LLM call completes."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from src.core.logging import get_logger
from src.db.models.model_catalog import LogicalModel
from src.db.models.usage import UsageRecord
from src.db.session import async_session_factory

_log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class UsageData:
    user_id: int
    api_key_id: int | None
    logical_model: LogicalModel
    channel_id: int | None
    upstream_model: str | None
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    status: str
    latency_ms: int | None
    request_id: str | None
    downgraded_features: list[str] | None


def compute_cost(model: LogicalModel, prompt_tokens: int, completion_tokens: int) -> Decimal | None:
    """Compute internal cost points for a logical model, if priced."""
    if model.price_input is None or model.price_output is None:
        return None
    prompt_cost = Decimal(prompt_tokens) * model.price_input
    completion_cost = Decimal(completion_tokens) * model.price_output
    return (prompt_cost + completion_cost) / Decimal(1_000_000)


async def record_usage(data: UsageData) -> None:
    """Append a usage record in an independent session without surfacing failures."""
    try:
        async with async_session_factory() as session:
            session.add(
                UsageRecord(
                    user_id=data.user_id,
                    api_key_id=data.api_key_id,
                    logical_model_id=data.logical_model.id,
                    channel_id=data.channel_id,
                    upstream_model=data.upstream_model,
                    prompt_tokens=data.prompt_tokens,
                    completion_tokens=data.completion_tokens,
                    total_tokens=data.total_tokens,
                    cost=compute_cost(
                        data.logical_model,
                        data.prompt_tokens,
                        data.completion_tokens,
                    ),
                    status=data.status,
                    latency_ms=data.latency_ms,
                    request_id=data.request_id,
                    downgraded_features=data.downgraded_features,
                )
            )
            await session.commit()
    except Exception:
        _log.exception(
            "gateway.usage.record_failed",
            request_id=data.request_id,
            user_id=data.user_id,
            logical_model_id=data.logical_model.id,
        )
