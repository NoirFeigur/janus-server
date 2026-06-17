"""Shared test fixtures.

Tests never touch the shared PostgreSQL/Redis instances — they use an in-memory
SQLite engine. A separate ``MetadataBase`` is reserved for test-only models so
SQLite-incompatible production tables (JSONB columns, PG partial indexes) are
not created here.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest_asyncio
from fakeredis.aioredis import FakeRedis
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

import src.core.redis as redis_module


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


@pytest_asyncio.fixture
async def fake_redis() -> AsyncIterator[FakeRedis]:
    """In-process fake Redis wired into ``src.core.redis``'s module singleton.

    Overrides the module-global ``_client`` so ``get_redis()`` / ``ping()`` resolve
    to this fake — tests never touch the shared Redis instance. The original
    singleton is restored on teardown to prevent cross-test leakage.
    """
    fake = FakeRedis(decode_responses=True)
    original = redis_module._client
    redis_module._client = fake
    try:
        yield fake
    finally:
        redis_module._client = original
        await fake.aclose()
