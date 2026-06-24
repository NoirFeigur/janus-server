"""Unit tests for src/gateway/router_manager.py — rebuild debounce."""

from __future__ import annotations

import asyncio
from typing import Any, cast

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
