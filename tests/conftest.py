"""Shared test fixtures.

Tests never touch the shared PostgreSQL/Redis instances — they use an in-memory
SQLite engine. A separate ``MetadataBase`` is reserved for test-only models so
SQLite-incompatible production tables (JSONB columns, PG partial indexes) are
not created here.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import cast

import pytest
import pytest_asyncio
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

import src.core.redis as redis_module
from tests._async_redis_double import AsyncRedisDouble


@pytest_asyncio.fixture
async def sqlite_engine() -> AsyncIterator[AsyncEngine]:
    """Fresh in-memory SQLite engine per test (zero cross-test leakage)."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def sqlite_session_factory(
    sqlite_engine: AsyncEngine,
) -> async_sessionmaker[AsyncSession]:
    """Session factory bound to the in-memory engine."""
    return async_sessionmaker(bind=sqlite_engine, expire_on_commit=False, autoflush=False)


@pytest_asyncio.fixture(autouse=True)
async def fake_redis() -> AsyncIterator[AsyncRedisDouble]:
    """In-process async Redis double wired into ``src.core.redis``'s singleton.

    Overrides the module-global ``_client`` so ``get_redis()`` / ``ping()`` resolve
    to this double — tests never touch the shared Redis instance. The original
    singleton is restored on teardown to prevent cross-test leakage.

    Autouse: enforces the no-shared-Redis invariant for *every* test, so any code
    path that lazily reaches ``get_redis()`` (e.g. a mutation calling
    ``cache.invalidate``) hits the double instead of the real instance. Tests that
    need a handle still request it.

    Why a hand-rolled double instead of ``fakeredis``: ``fakeredis`` resolves its
    awaits on a background thread, which corrupts coverage.py's C tracer on
    CPython 3.11 (lines after every ``await redis.*`` are dropped). This double
    awaits nothing off-loop, so coverage of cache/redis paths reflects reality.
    See ``tests/_async_redis_double.py``.
    """
    double = AsyncRedisDouble(decode_responses=True)
    original = redis_module._client
    redis_module._client = cast(Redis, double)
    try:
        yield double
    finally:
        redis_module._client = original
        await double.aclose()


@pytest.fixture(autouse=True)
def reset_gateway_process_state() -> None:
    """Clear process-local gateway globals before every test.

    The event outbox (``_outboxes``) and emergency rate-limiter window
    (``_emergency_windows``) are module-level state that survives between tests.
    Without a reset, a test that fills the bounded outbox to its cap leaks those
    parked events into the next test's first ``enqueue_event`` flush (which
    drains the outbox FIFO before the new event), inflating queue lengths. Reset
    mirrors the no-shared-state guarantee the ``fake_redis`` fixture provides.
    """
    from src.gateway.events import reset_outbox
    from src.gateway.rate_limit import reset_emergency_limiter

    reset_outbox()
    reset_emergency_limiter()
