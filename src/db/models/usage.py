"""Usage aggregate (data-model batch 3, table 3.1).

Append-only per-call ledger (``LogEntity``): the authoritative source for
billing, usage reports, and downgrade-rate statistics. Real-time quota counting
lives in Redis; this table is the durable account of record.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from sqlalchemy import BigInteger, Index, Numeric, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from src.db.base import LogEntity


class UsageRecord(LogEntity):
    """One row per LLM call (§3.1)."""

    __tablename__ = "usage_record"

    # —— Principal (who it is billed to) ——
    user_id: Mapped[int] = mapped_column(
        BigInteger, index=True
    )  # -> sys_user, principal.
    api_key_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, index=True
    )  # -> api_key; which key issued the call (null for direct SSO).

    # —— Model and carrier (what was used) ——
    logical_model_id: Mapped[int] = mapped_column(
        BigInteger, index=True
    )  # -> logical_model, the unified name the employee requested.
    channel_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, index=True
    )  # -> upstream_channel actually routed to (post-hoc analysis).
    upstream_model: Mapped[str | None] = mapped_column(
        String(128), nullable=True
    )  # Real upstream model name actually called.

    # —— Metering (how much) ——
    prompt_tokens: Mapped[int] = mapped_column(default=0)  # Input tokens.
    completion_tokens: Mapped[int] = mapped_column(default=0)  # Output tokens.
    total_tokens: Mapped[int] = mapped_column(
        default=0
    )  # Sum (redundant, eases aggregation).
    cost: Mapped[Decimal | None] = mapped_column(
        Numeric(14, 6), nullable=True
    )  # Internal cost frozen at call time: (in*price_in + out*price_out) / 1e6.

    # —— Result and observability ——
    status: Mapped[str] = mapped_column(
        String(16), index=True
    )  # UsageStatus: success | error | timeout.
    latency_ms: Mapped[int | None] = mapped_column(
        nullable=True
    )  # End-to-end latency.
    request_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True
    )  # Correlation id across gateway logs/Redis (no call_audit table).
    downgraded_features: Mapped[list[Any] | None] = mapped_column(
        JSONB, nullable=True
    )  # Dropped/downgraded features this call (G13); null/empty = not downgraded.

    __table_args__ = (
        Index("ix_usage_user_created", "user_id", "created_at"),
        Index("ix_usage_logical_model_created", "logical_model_id", "created_at"),
    )
