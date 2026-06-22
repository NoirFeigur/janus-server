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
    _poll_task: asyncio.Task[None] | None = None

    @classmethod
    async def startup(cls, session_factory: async_sessionmaker[AsyncSession]) -> None:
        await cls.rebuild(session_factory)
        cls._poll_task = asyncio.create_task(cls._poll(session_factory))

    @classmethod
    async def shutdown(cls) -> None:
        if cls._poll_task is None:
            return
        cls._poll_task.cancel()
        with suppress(asyncio.CancelledError):
            await cls._poll_task
        cls._poll_task = None

    @classmethod
    async def rebuild(cls, session_factory: async_sessionmaker[AsyncSession]) -> None:
        async with cls._lock:
            async with session_factory() as session:
                rows = await GatewayRepository(session).get_router_config()
            router = build_router(rows)
            cls._router = router
            _log.info("gateway.router.rebuilt", deployments=len(rows))

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
