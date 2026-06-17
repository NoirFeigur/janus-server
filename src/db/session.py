"""Async engine, session factory, and FastAPI session dependency.

The engine reads its URL from ``Settings`` (``.env``), never a hardcoded string.
``get_session()`` is the only sanctioned way for repositories to obtain a
session — repositories never build their own engine (layering discipline).
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from src.config import get_settings

_settings = get_settings()

engine: AsyncEngine = create_async_engine(
    _settings.database_url,
    echo=_settings.database_echo,
    pool_pre_ping=True,  # Recycle stale connections to the shared instance.
    pool_size=_settings.database_pool_size,
    max_overflow=_settings.database_max_overflow,
    pool_timeout=_settings.database_pool_timeout_seconds,
    pool_recycle=_settings.database_pool_recycle_seconds,
)

async_session_factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
    bind=engine,
    expire_on_commit=False,  # Keep ORM objects usable after commit.
    autoflush=False,
)


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency yielding a session bound to the request lifecycle."""
    async with async_session_factory() as session:
        yield session
