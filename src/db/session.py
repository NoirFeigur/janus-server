"""Async engine, session factory, and the request/unit-of-work boundary.

The engine reads its URL from ``Settings`` (``.env``), never a hardcoded string.
``get_session()`` is the only sanctioned way for a request to obtain a session
— repositories never build their own engine (layering discipline).

**Transaction boundary (Unit of Work).** Services no longer ``commit()`` —
they only ``flush()``. The single commit happens here, at the edge: the request
(or an ARQ task) opens a :func:`unit_of_work`, the handler does its writes, and
the boundary commits on success / rolls back on any exception. This makes a
request atomic (a later failure rolls back earlier writes) and removes the
"half-committed then 500" hazard the per-service commits caused.

**FastAPI scope (critical).** On FastAPI ``0.137.1`` a default yield-dependency's
teardown runs *after* the response is sent, so a commit there would land after
the client already saw ``200 OK`` — a commit failure could not be reported.
Write routes therefore depend on ``Depends(get_session, scope="function")`` so
the commit (and any failure) happens *before* the response is serialized.

**After-commit hooks.** Side effects that must run only once the DB commit
succeeds (cache invalidation, Redis session revocation) register via
:func:`add_after_commit_hook`; they fire in :func:`commit_session` strictly
after the commit returns, and are dropped on rollback.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import cast

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

AfterCommitHook = Callable[[], Awaitable[None]]
_AFTER_COMMIT_HOOKS = "after_commit_hooks"


def add_after_commit_hook(session: AsyncSession, hook: AfterCommitHook) -> None:
    """Queue a coroutine to run once the session's commit succeeds.

    Use for side effects that must NOT happen on a transaction that ends up
    rolled back — cache invalidation, Redis session revocation, published
    events. Hooks fire in registration order in :func:`commit_session` after the
    commit returns; on rollback they are discarded (never fired).
    """
    hooks = cast(
        "list[AfterCommitHook]", session.info.setdefault(_AFTER_COMMIT_HOOKS, [])
    )
    hooks.append(hook)


async def _run_after_commit_hooks(session: AsyncSession) -> None:
    hooks = cast("list[AfterCommitHook]", session.info.pop(_AFTER_COMMIT_HOOKS, []))
    for hook in hooks:
        await hook()


async def commit_session(session: AsyncSession) -> None:
    """Commit the session, then fire its after-commit hooks.

    On commit failure the session is rolled back, queued hooks are dropped (the
    write never landed), and the original exception propagates. Hooks run only
    after a successful commit; a hook raising surfaces as an error but does NOT
    roll back the already-committed transaction.
    """
    try:
        await session.commit()
    except BaseException:
        await session.rollback()
        session.info.pop(_AFTER_COMMIT_HOOKS, None)
        raise
    await _run_after_commit_hooks(session)


@asynccontextmanager
async def unit_of_work(
    session_factory: async_sessionmaker[AsyncSession] = async_session_factory,
) -> AsyncIterator[AsyncSession]:
    """Open a session as a single atomic unit of work.

    Commits on clean exit (firing after-commit hooks), rolls back on any
    exception (``BaseException``, so a cancellation also rolls back). The shared
    primitive behind both the request dependency and non-request callers (ARQ
    tasks, the independent login-log write). Do NOT nest a second unit of work
    inside a service that already received a session — use
    ``session.begin_nested()`` for same-transaction isolation instead.
    """
    async with session_factory() as session:
        try:
            yield session
        except BaseException:
            await session.rollback()
            session.info.pop(_AFTER_COMMIT_HOOKS, None)
            raise
        else:
            await commit_session(session)


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency: one request = one unit of work.

    Depend on this with ``Depends(get_session, scope="function")`` on any route
    that writes, so the commit (and any failure) happens before the response is
    serialized — see the module docstring.
    """
    async with unit_of_work() as session:
        yield session
