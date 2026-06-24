"""Unit tests for src/gateway/router_manager.py — rebuild debounce."""

from __future__ import annotations

import asyncio
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

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
