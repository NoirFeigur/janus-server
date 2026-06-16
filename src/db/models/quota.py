"""Quota aggregate (data-model batch 3, table 3.2).

Quota *definitions* (the rules ops set), not consumed amounts — real-time usage
lives in Redis (§3 Redis<->DB split). Config nature, so it uses ``BaseEntity``
(soft-delete + audit). Adjudication is AND-semantics: every matching rule must
pass (orthogonal to grant's OR-semantics).
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import BigInteger, CheckConstraint, Index, Numeric, String, text
from sqlalchemy.orm import Mapped, mapped_column

from src.db.base import BaseEntity


class Quota(BaseEntity):
    """Quota config + period ceiling (§3.2)."""

    __tablename__ = "quota"

    # —— Quota subject (polymorphic scope, same shape as user_model_grant) ——
    scope: Mapped[str] = mapped_column(
        String(16), index=True
    )  # QuotaScope: user | department | global.
    scope_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, index=True
    )  # null when scope=global.

    logical_model_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, index=True
    )  # -> logical_model; null = total quota across all models for this scope.

    # —— Period + metric ——
    period: Mapped[str] = mapped_column(
        String(16)
    )  # QuotaPeriod: daily | monthly | total.
    metric: Mapped[str] = mapped_column(
        String(16), default="tokens"
    )  # QuotaMetric: tokens | requests | cost.

    limit_value: Mapped[Decimal] = mapped_column(
        Numeric(14, 6)
    )  # Ceiling; same precision as usage_record.cost (tokens/requests use integers).

    enforce: Mapped[bool] = mapped_column(
        default=True
    )  # true = reject on breach (429); false = warn only (soft quota).

    status: Mapped[str] = mapped_column(
        String(16), default="active", index=True
    )  # ActiveStatus: active | disabled.
    remark: Mapped[str | None] = mapped_column(String(255), nullable=True)

    __table_args__ = (
        Index(
            "uq_quota_scope_model_period_metric_active",
            "scope",
            "scope_id",
            "logical_model_id",
            "period",
            "metric",
            unique=True,
            postgresql_nulls_not_distinct=True,
            postgresql_where=text("is_deleted = false"),
        ),  # One active quota per (subject, model, period, metric); NULLs treated equal (PG15+).
        CheckConstraint(
            "(scope = 'global' AND scope_id IS NULL) "
            "OR (scope <> 'global' AND scope_id IS NOT NULL)",
            name="ck_quota_scope_id_presence",
        ),  # global => no scope_id; non-global => scope_id required.
    )
