"""ARQ task for probing degraded channels.

Runs as a cron job every minute.  For each degraded channel, acquires a probe
lock, runs a lightweight LLM probe, and either recovers the channel or
increments probe failure count toward hard-disable.
"""

from __future__ import annotations

from contextlib import suppress
from typing import Any

from src.channel_health.probe import probe_channel
from src.channel_health.redis_store import (
    acquire_probe_lock,
    get_channel_state,
    get_degraded_channel_ids,
    release_probe_lock,
)
from src.channel_health.service import ChannelHealthService
from src.core.logging import get_logger

_log = get_logger(__name__)


async def probe_degraded_channels(ctx: dict[str, Any]) -> int:
    """ARQ cron task: probe all degraded channels and handle recovery/disable.

    Returns the number of channels probed.
    """
    degraded_ids = await get_degraded_channel_ids()
    if not degraded_ids:
        return 0

    health_service = ChannelHealthService()
    probed = 0

    for channel_id in degraded_ids:
        # Acquire per-channel lock (prevents concurrent probes)
        if not await acquire_probe_lock(channel_id, health_service.probe_interval_seconds):
            continue

        try:
            await _probe_single_channel(channel_id, health_service)
            probed += 1
        finally:
            await release_probe_lock(channel_id)

    return probed


async def _probe_single_channel(channel_id: int, health_service: ChannelHealthService) -> None:
    """Probe a single degraded channel and handle the outcome."""
    # Get probe target info from channel state or DB
    probe_info = await _get_probe_info(channel_id)
    if probe_info is None:
        _log.warning("channel_health.probe_no_info", channel_id=channel_id)
        return

    success = await probe_channel(
        provider=probe_info["provider"],
        upstream_model=probe_info["upstream_model"],
        api_key=probe_info["api_key"],
        api_base=probe_info.get("api_base"),
    )

    if success:
        await health_service.record_probe_success(channel_id)
    else:
        state = await get_channel_state(channel_id)
        current_failures = int(state.get("probe_failures", "0"))
        should_disable = await health_service.record_probe_failure(channel_id, current_failures)
        if should_disable:
            await _hard_disable_channel(channel_id)


async def _get_probe_info(channel_id: int) -> dict[str, Any] | None:
    """Load probe target info from DB (one active key + deployment for this channel).

    Returns None if channel has no active keys/deployments to probe.
    """
    with suppress(Exception):
        from src.core.channel_crypto import decrypt_channel_key
        from src.db.session import async_session_factory

        async with async_session_factory() as session:
            from sqlalchemy import select

            from src.db.models.model_catalog import ChannelKey, ModelDeployment, UpstreamChannel

            # Get channel with one active key
            channel_result = await session.execute(
                select(UpstreamChannel).where(UpstreamChannel.id == channel_id)
            )
            channel = channel_result.scalar_one_or_none()
            if channel is None:
                return None

            # Get one active key
            key_result = await session.execute(
                select(ChannelKey)
                .where(ChannelKey.channel_id == channel_id)
                .where(ChannelKey.status == "active")
                .limit(1)
            )
            key_row = key_result.scalar_one_or_none()
            if key_row is None:
                return None

            # Get one deployment to know the upstream model
            deploy_result = await session.execute(
                select(ModelDeployment)
                .where(ModelDeployment.channel_id == channel_id)
                .where(ModelDeployment.is_deleted == False)  # noqa: E712
                .limit(1)
            )
            deployment = deploy_result.scalar_one_or_none()
            if deployment is None:
                return None

            return {
                "provider": channel.provider,
                "upstream_model": deployment.upstream_model,
                "api_key": decrypt_channel_key(key_row.api_key_encrypted),
                "api_base": channel.api_base,
            }
    return None


async def _hard_disable_channel(channel_id: int) -> None:
    """Persist hard-disable to DB (set channel status=disabled) and audit."""
    with suppress(Exception):
        from src.db.session import async_session_factory

        async with async_session_factory() as session:
            from sqlalchemy import update

            from src.db.models.model_catalog import UpstreamChannel

            await session.execute(
                update(UpstreamChannel)
                .where(UpstreamChannel.id == channel_id)
                .values(status="disabled")
            )
            await session.commit()

        _log.error("channel_health.hard_disabled", channel_id=channel_id)
        # Publish invalidation so Router rebuilds without this channel
        from src.channel_health.service import _publish_invalidation

        await _publish_invalidation()
