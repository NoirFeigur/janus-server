"""Gateway request context — captures the full lifecycle of a single LLM call.

Every gateway endpoint populates a ``GatewayRequestContext`` that flows through
the hot path and is consumed by the finalizer (quota settlement, usage recording,
observability logging, metrics emission, and channel-health tracking).  This
replaces the scattered dict-based tracking and ``asyncio.create_task`` pattern.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from time import monotonic
from typing import TYPE_CHECKING
from uuid import uuid4

from src.enums import UsageStatus

if TYPE_CHECKING:
    from src.gateway.quota import QuotaReservation


@dataclass(slots=True)
class GatewayRequestContext:
    """Mutable context accumulating state across the gateway request lifecycle."""

    # --- Identity (set at request start) ---
    request_id: str = field(default_factory=lambda: uuid4().hex)
    user_id: int = 0
    department_id: int | None = None
    api_key_id: int | None = None

    # --- Model routing (set after resolve_model) ---
    requested_model: str | None = None
    logical_model_id: int | None = None
    logical_model_name: str | None = None

    # --- Upstream routing (set after LLM call or during streaming) ---
    channel_id: int | None = None
    upstream_model: str | None = None
    provider: str | None = None

    # --- Timing (set during execution) ---
    started_at: float = field(default_factory=monotonic)
    ttft_ms: int | None = None  # Time to first token (streaming only).
    latency_ms: int | None = None  # Total request latency.

    # --- Usage (set after completion or stream end) ---
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cost: Decimal | None = None

    # --- Status ---
    status: str = UsageStatus.success.value
    error_code: str | None = None  # ErrorCode value when request failed.
    error_body: str | None = None  # Redacted upstream error body (max 2048 chars).
    http_status_code: int = 200

    # --- Behavioral flags ---
    stream: bool = False
    cache_hit: bool = False
    retry_count: int = 0
    fallback_used: bool = False

    # --- Quota (set after check_quota) ---
    quota_reserved: bool = False
    quota_settled: bool = False
    # Resolved quota counters reserved at check time; settle/compensate target
    # these exact keys (hot-reload + period-rollover safe). Empty => fall back to
    # legacy re-query settle in the finalizer.
    quota_reservations: list[QuotaReservation] = field(default_factory=list)

    def record_latency(self) -> None:
        """Compute and store latency_ms from started_at to now."""
        self.latency_ms = int((monotonic() - self.started_at) * 1000)

    def record_ttft(self) -> None:
        """Record time-to-first-token from started_at. Call once on first yielded chunk."""
        if self.ttft_ms is None:
            self.ttft_ms = int((monotonic() - self.started_at) * 1000)

    def set_usage(
        self,
        prompt_tokens: int,
        completion_tokens: int,
        total_tokens: int,
    ) -> None:
        """Update token usage counters."""
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.total_tokens = total_tokens

    def mark_error(
        self,
        status: str,
        *,
        http_status_code: int = 502,
        error_code: str | None = None,
        error_body: str | None = None,
    ) -> None:
        """Mark the request as failed with given status and optional error details."""
        self.status = status
        self.http_status_code = http_status_code
        self.error_code = error_code
        if error_body is not None:
            # Redact: keep at most 2048 chars to prevent log bloat.
            self.error_body = error_body[:2048]
