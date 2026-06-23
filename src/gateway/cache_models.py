"""Hot-path cache models — lightweight snapshots for cache serialization.

These dataclasses represent the minimal data needed by the gateway hot path,
cached in Redis to eliminate per-request DB queries.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class ResolvedModel:
    """Cached result of model resolution (replaces full LogicalModel ORM load)."""

    id: int
    name: str
    provider: str | None
    price_input: Decimal | None
    price_output: Decimal | None
    response_cache_enabled: bool
    response_cache_ttl_seconds: int


@dataclass(frozen=True, slots=True)
class QuotaRuleSnapshot:
    """Cached quota rule for the check_quota hot path."""

    quota_id: int
    scope: str  # QuotaScope value
    metric: str  # QuotaMetric value
    period: str  # QuotaPeriod value
    limit_value: Decimal
    logical_model_id: int | None
