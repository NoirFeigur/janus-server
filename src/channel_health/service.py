"""Channel health service — orchestrates degradation and recovery logic.

Called from the gateway finalizer to record outcomes, evaluates thresholds,
and triggers auto-degrade / recovery transitions.
"""

from __future__ import annotations

import time
from contextlib import suppress

from src.channel_health.redis_store import (
    add_to_degraded,
    clear_channel_state,
    get_error_rate,
    record_request_outcome,
    remove_from_degraded,
    update_channel_state,
)
from src.config import get_settings
from src.core.logging import get_logger
from src.core.redis import get_redis

_log = get_logger(__name__)


class ChannelHealthService:
    """Stateless service for channel health evaluation and transitions."""

    def __init__(self) -> None:
        settings = get_settings()
        self.window_seconds: int = getattr(settings, "channel_health_window_seconds", 300)
        self.min_requests: int = getattr(settings, "channel_health_min_requests", 10)
        self.error_threshold: float = getattr(settings, "channel_health_error_threshold", 0.5)
        self.max_probe_failures: int = getattr(settings, "channel_health_max_probe_failures", 5)
        self.probe_interval_seconds: int = getattr(
            settings, "channel_health_probe_interval_seconds", 60
        )

    async def record_and_evaluate(
        self,
        channel_id: int,
        *,
        success: bool,
        error_class: str | None = None,
    ) -> None:
        """Record a request outcome and check if channel should be degraded.

        This is called from the gateway finalizer (non-blocking, fail-open).
        """
        await record_request_outcome(
            channel_id,
            success=success,
            error_class=error_class,
            window_seconds=self.window_seconds,
        )

        if success:
            return

        # Check if degradation threshold is reached
        total, errors, rate = await get_error_rate(channel_id, self.window_seconds)
        if total < self.min_requests:
            return
        if rate <= self.error_threshold:
            return

        # Threshold breached — attempt to degrade
        was_first = await add_to_degraded(channel_id)
        if was_first:
            _log.warning(
                "channel_health.auto_degraded",
                channel_id=channel_id,
                error_rate=round(rate, 3),
                total_requests=total,
                errors=errors,
            )
            await update_channel_state(
                channel_id,
                status="degraded",
                degraded_since=str(int(time.time())),
                error_rate=str(round(rate, 3)),
                probe_failures="0",
            )
            # Publish router invalidation so other replicas exclude this channel
            await _publish_invalidation()

    async def record_probe_success(self, channel_id: int) -> None:
        """Mark a channel as recovered after a successful probe."""
        await remove_from_degraded(channel_id)
        await clear_channel_state(channel_id)
        _log.info("channel_health.recovered", channel_id=channel_id)
        await _publish_invalidation()

    async def record_probe_failure(self, channel_id: int, current_failures: int) -> bool:
        """Record a probe failure. Returns True if hard-disable threshold reached."""
        new_count = current_failures + 1
        await update_channel_state(
            channel_id,
            probe_failures=str(new_count),
            last_probe_at=str(int(time.time())),
        )
        if new_count >= self.max_probe_failures:
            _log.error(
                "channel_health.hard_disable_threshold",
                channel_id=channel_id,
                probe_failures=new_count,
            )
            return True
        return False


async def _publish_invalidation() -> None:
    """Publish router invalidation so all replicas rebuild excluding degraded channels."""
    with suppress(Exception):
        await get_redis().publish("gateway:router:invalidate", "1")
