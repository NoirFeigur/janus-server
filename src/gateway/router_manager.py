from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import NoReturn

from litellm import Router
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette import status

from src.core.logging import get_logger
from src.enums import ErrorCode
from src.exceptions import AppError
from src.gateway.repository import GatewayRepository
from src.gateway.router_factory import build_router

_log = get_logger(__name__)


class RouterManager:
    """Singleton managing the live LiteLLM Router reference."""

    _router: Router | None = None
    _lock = asyncio.Lock()
    _rebuild_interval_seconds: int = 30
    # Trailing-edge debounce: admin bulk writes (e.g. a script adding many keys)
    # publish a burst of invalidate events. Without debounce each event triggers a
    # full rebuild (DB query + new Router + closing the old Router's aiohttp/Redis
    # sessions under in-flight requests). We coalesce a burst into a single rebuild
    # by waiting for a short quiet window; each new event resets the timer.
    _debounce_seconds: float = 1.5
    # Grace period before closing a *superseded* Router. A rebuild swaps the live
    # reference atomically, but requests that already obtained the old Router via
    # get_router() are still streaming through its aiohttp/Redis sessions. Closing
    # inline tore those sessions down mid-flight (connection-reset errors). We
    # defer the close so in-flight requests drain first. (Very long streams beyond
    # this window are an accepted edge for a config hot-reload; the next rebuild
    # will not affect already-started streams that completed within the grace.)
    _router_close_grace_seconds: float = 60.0
    _invalidate_event: asyncio.Event = asyncio.Event()
    _poll_task: asyncio.Task[None] | None = None
    _sub_task: asyncio.Task[None] | None = None
    _debounce_task: asyncio.Task[None] | None = None
    _pending_close_tasks: set[asyncio.Task[None]] = set()
    _session_factory: async_sessionmaker[AsyncSession] | None = None

    @classmethod
    async def startup(cls, session_factory: async_sessionmaker[AsyncSession]) -> None:
        cls._session_factory = session_factory
        await cls.rebuild(session_factory)
        cls._debounce_task = asyncio.create_task(cls._debounce_worker(session_factory))
        cls._poll_task = asyncio.create_task(cls._poll(session_factory))
        cls._sub_task = asyncio.create_task(cls._subscribe(session_factory))

    @classmethod
    async def shutdown(cls) -> None:
        for task in (cls._poll_task, cls._sub_task, cls._debounce_task):
            if task is not None:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
        cls._poll_task = None
        cls._sub_task = None
        cls._debounce_task = None
        # Flush deferred Router closes: cancelling triggers the close in `finally`,
        # so superseded Routers release their sessions before the process exits.
        for close_task in list(cls._pending_close_tasks):
            close_task.cancel()
            with suppress(asyncio.CancelledError):
                await close_task
        cls._pending_close_tasks.clear()

    @classmethod
    async def rebuild(cls, session_factory: async_sessionmaker[AsyncSession]) -> None:
        async with cls._lock:
            async with session_factory() as session:
                rows = await GatewayRepository(session).get_router_config()

            # Exclude degraded channels (fail-open: if Redis is down, include all)
            degraded_ids = await cls._get_degraded_ids()
            if degraded_ids:
                rows = [r for r in rows if r.channel_id not in degraded_ids]

            new_router = build_router(rows)
            old_router = cls._router
            cls._router = new_router
            # Defer closing the old Router so in-flight requests holding it can
            # drain; closing inline tore down their sessions mid-stream.
            if old_router is not None:
                cls._schedule_router_close(old_router)
            _log.info(
                "gateway.router.rebuilt",
                deployments=len(rows),
                degraded_excluded=len(degraded_ids),
            )

    @classmethod
    def _schedule_router_close(cls, router: Router) -> None:
        """Close ``router`` after the grace period, tracking the task for shutdown."""
        task = asyncio.create_task(cls._close_router_after_grace(router))
        cls._pending_close_tasks.add(task)
        task.add_done_callback(cls._pending_close_tasks.discard)

    @classmethod
    async def _close_router_after_grace(cls, router: Router) -> None:
        """Wait the grace window, then close the Router.

        The close runs in ``finally`` so a shutdown-time cancellation still
        releases the Router's aiohttp/Redis sessions instead of leaking them.
        """
        try:
            await asyncio.sleep(cls._router_close_grace_seconds)
        finally:
            with suppress(Exception):
                await router.close()  # type-safe: litellm Router exposes close()

    @classmethod
    def get_router(cls) -> Router:
        if cls._router is None:
            raise AppError(ErrorCode.model_unavailable, status.HTTP_503_SERVICE_UNAVAILABLE)
        return cls._router

    @classmethod
    async def _poll(cls, session_factory: async_sessionmaker[AsyncSession]) -> NoReturn:
        while True:
            await asyncio.sleep(cls._rebuild_interval_seconds)
            try:
                await cls.rebuild(session_factory)
            except Exception:
                _log.exception("gateway.router.rebuild_failed")

    @classmethod
    async def _debounce_worker(
        cls, session_factory: async_sessionmaker[AsyncSession]
    ) -> NoReturn:
        """Coalesce invalidate bursts into a single trailing-edge rebuild.

        Blocks until an invalidate is signalled, then waits for a quiet window of
        ``_debounce_seconds`` with no further signals before rebuilding. Each new
        signal during the window resets the timer, so a burst of N events yields
        exactly one rebuild shortly after the burst ends.
        """
        while True:
            await cls._invalidate_event.wait()
            # Quiet-window loop: extend while events keep arriving.
            while True:
                cls._invalidate_event.clear()
                try:
                    await asyncio.wait_for(
                        cls._invalidate_event.wait(), timeout=cls._debounce_seconds
                    )
                except TimeoutError:
                    break  # window elapsed with no new event → rebuild
            try:
                await cls.rebuild(session_factory)
            except Exception:
                _log.exception("gateway.router.rebuild_after_invalidate_failed")

    @classmethod
    async def _get_degraded_ids(cls) -> set[int]:
        """Load degraded channel IDs from Redis (fail-open: empty set on error)."""
        with suppress(Exception):
            from src.channel_health.redis_store import get_degraded_channel_ids

            return await get_degraded_channel_ids()
        return set()

    @classmethod
    async def _subscribe(
        cls, session_factory: async_sessionmaker[AsyncSession]
    ) -> NoReturn:
        """Listen for catalog invalidation events from admin writes."""
        from src.core.redis import get_redis

        while True:
            try:
                redis = get_redis()
                pubsub = redis.pubsub()
                await pubsub.subscribe("gateway:router:invalidate")
                try:
                    while True:
                        msg = await pubsub.get_message(
                            ignore_subscribe_messages=True, timeout=1.0
                        )
                        if msg is not None and msg.get("type") == "message":
                            _log.info("gateway.router.invalidate_received")
                            # Signal the debounce worker instead of rebuilding
                            # inline; a burst of events coalesces into one rebuild.
                            cls._invalidate_event.set()
                finally:
                    await pubsub.unsubscribe("gateway:router:invalidate")
                    await pubsub.aclose()
            except asyncio.CancelledError:
                raise
            except Exception:
                _log.exception("gateway.router.subscribe_failed_retrying")
                await asyncio.sleep(5)
