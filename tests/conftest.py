"""Shared test fixtures.

Tests never touch the shared PostgreSQL/Redis instances — they use an in-memory
SQLite engine. A separate ``MetadataBase`` is reserved for test-only models so
SQLite-incompatible production tables (JSONB columns, PG partial indexes) are
not created here.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest_asyncio
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


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
    return async_sessionmaker(
        bind=sqlite_engine, expire_on_commit=False, autoflush=False
    )
