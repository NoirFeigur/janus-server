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
        {"comment": "配额配置：运维设的限额规则（非已消耗量，实时用量在 Redis）"},
    )

    # —— Quota subject (polymorphic scope, same shape as user_model_grant) ——
    scope: Mapped[str] = mapped_column(
        String(16),
        index=True,
        comment="配额范围 QuotaScope：user | department | global",
    )
    scope_id: Mapped[int | None] = mapped_column(
        BigInteger,
        nullable=True,
        index=True,
        comment="范围主体 id；scope=global 时为空",
    )

    logical_model_id: Mapped[int | None] = mapped_column(
        BigInteger,
        nullable=True,
        index=True,
        comment="逻辑模型 logical_model.id；null=跨所有模型的总配额",
    )

    # —— Period + metric ——
    period: Mapped[str] = mapped_column(
        String(16),
        comment="周期 QuotaPeriod：daily | monthly | total",
    )
    metric: Mapped[str] = mapped_column(
        String(16),
        default="tokens",
        comment="度量 QuotaMetric：tokens | requests | cost",
    )

    limit_value: Mapped[Decimal] = mapped_column(
        Numeric(14, 6),
        comment="限额上限（tokens/requests 用整数）",
    )

    enforce: Mapped[bool] = mapped_column(
        default=True,
        comment="true=超限拒绝(429) | false=仅告警（软配额）",
    )

    status: Mapped[str] = mapped_column(
        String(16),
        default="active",
        index=True,
        comment="状态 ActiveStatus",
    )
    remark: Mapped[str | None] = mapped_column(String(255), nullable=True)
