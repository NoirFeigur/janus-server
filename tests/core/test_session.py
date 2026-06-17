"""Tests for the DB session dependency (src/db/session.py).

The engine/factory are module-level singletons bound to the configured URL; the
only behaviour worth asserting here is that ``get_session`` yields a live
``AsyncSession`` and that the async-context teardown runs without error. A
plain in-memory SQLite session would couple this to a specific engine, so we
drive the dependency's own factory directly.
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from src.db import session as session_module

pytestmark = pytest.mark.asyncio


async def test_get_session_yields_async_session() -> None:
    """``get_session`` 应产出一个可用的 AsyncSession,并在退出时正常收尾。"""
    agen = session_module.get_session()
    session = await agen.__anext__()
    try:
        assert isinstance(session, AsyncSession)
    finally:
        # Drive the generator to completion so the ``async with`` teardown runs.
        with pytest.raises(StopAsyncIteration):
            await agen.__anext__()
