"""Unit tests for src/gateway/router_manager.py — rebuild debounce."""

from __future__ import annotations

import asyncio
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.gateway.repository import RouterDeploymentRow
from src.gateway.router_manager import RouterManager

_DUMMY_FACTORY = cast("async_sessionmaker[AsyncSession]", object())


@pytest.mark.asyncio
async def test_invalidate_burst_coalesces_into_single_rebuild(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A burst of invalidate signals within the debounce window → one rebuild."""
    calls = 0

    async def _fake_rebuild(_cls: Any, _session_factory: Any) -> None:
        nonlocal calls
        calls += 1

    monkeypatch.setattr(RouterManager, "rebuild", classmethod(_fake_rebuild))
    monkeypatch.setattr(RouterManager, "_debounce_seconds", 0.05)
    monkeypatch.setattr(RouterManager, "_invalidate_event", asyncio.Event())

    worker = asyncio.create_task(RouterManager._debounce_worker(object()))  # type: ignore[arg-type]
    try:
        # Fire a burst faster than the debounce window.
        for _ in range(10):
            RouterManager._invalidate_event.set()
            await asyncio.sleep(0.005)
        # Wait past the quiet window so the trailing-edge rebuild fires.
        await asyncio.sleep(0.15)
        assert calls == 1
    finally:
        worker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await worker


@pytest.mark.asyncio
async def test_separate_bursts_trigger_separate_rebuilds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two bursts separated by a full quiet window → two rebuilds."""
    calls = 0

    async def _fake_rebuild(_cls: Any, _session_factory: Any) -> None:
        nonlocal calls
        calls += 1

    monkeypatch.setattr(RouterManager, "rebuild", classmethod(_fake_rebuild))
    monkeypatch.setattr(RouterManager, "_debounce_seconds", 0.05)
    monkeypatch.setattr(RouterManager, "_invalidate_event", asyncio.Event())

    worker = asyncio.create_task(RouterManager._debounce_worker(object()))  # type: ignore[arg-type]
    try:
        RouterManager._invalidate_event.set()
        await asyncio.sleep(0.15)  # let first rebuild fire
        RouterManager._invalidate_event.set()
        await asyncio.sleep(0.15)  # let second rebuild fire
        assert calls == 2
    finally:
        worker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await worker


@pytest.mark.asyncio
async def test_old_router_closed_after_grace_not_immediately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D: a superseded Router must close only AFTER the grace window, so in-flight
    requests holding it can drain instead of having their sessions torn down."""
    monkeypatch.setattr(RouterManager, "_router_close_grace_seconds", 0.1)
    monkeypatch.setattr(RouterManager, "_pending_close_tasks", set())
    router = AsyncMock()

    RouterManager._schedule_router_close(cast("Any", router))

    # Immediately after scheduling, the old Router must still be open.
    await asyncio.sleep(0.02)
    router.close.assert_not_awaited()

    # After the grace window elapses, it is closed exactly once.
    await asyncio.sleep(0.15)
    router.close.assert_awaited_once()
    assert not RouterManager._pending_close_tasks


@pytest.mark.asyncio
async def test_rebuild_skips_unchanged_config_without_closing_old_router(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = RouterDeploymentRow(
        logical_model_name="gpt-4",
        logical_model_id=1,
        upstream_model="gpt-4o",
        provider="openai",
        protocol="openai",
        api_base=None,
        extra_config=None,
        api_key_encrypted="ciphertext",
        channel_id=10,
        channel_key_id=20,
        deployment_weight=1,
        deployment_priority=0,
        key_weight=1,
        key_rpm_limit=None,
        key_tpm_limit=None,
    )
    old_router = AsyncMock()
    session = AsyncMock()
    session.__aenter__.return_value = session
    session.__aexit__.return_value = None
    def session_factory() -> AsyncMock:
        return session

    repo = AsyncMock()
    repo.get_router_config.return_value = [row]

    monkeypatch.setattr("src.gateway.router_manager.GatewayRepository", lambda _: repo)
    monkeypatch.setattr(RouterManager, "_get_degraded_ids", AsyncMock(return_value=set()))
    monkeypatch.setattr(RouterManager, "_router", old_router)
    monkeypatch.setattr(RouterManager, "_router_fingerprint", RouterManager._fingerprint([row]))
    monkeypatch.setattr(RouterManager, "_pending_close_tasks", set())
    schedule_close = AsyncMock()
    monkeypatch.setattr(RouterManager, "_schedule_router_close", schedule_close)

    await RouterManager.rebuild(cast("async_sessionmaker[AsyncSession]", session_factory))

    assert RouterManager._router is old_router
    schedule_close.assert_not_called()


@pytest.mark.asyncio
async def test_shutdown_flushes_pending_router_close_immediately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D: shutdown must not wait out the grace window — it flushes pending closes
    (close runs in the task's `finally`, so no Router session is leaked)."""
    monkeypatch.setattr(RouterManager, "_router_close_grace_seconds", 1000.0)
    monkeypatch.setattr(RouterManager, "_pending_close_tasks", set())
    monkeypatch.setattr(RouterManager, "_poll_task", None)
    monkeypatch.setattr(RouterManager, "_sub_task", None)
    monkeypatch.setattr(RouterManager, "_debounce_task", None)
    router = AsyncMock()

    RouterManager._schedule_router_close(cast("Any", router))
    await asyncio.sleep(0.02)  # let the close task start sleeping
    router.close.assert_not_awaited()

    await RouterManager.shutdown()

    router.close.assert_awaited_once()
    assert not RouterManager._pending_close_tasks
