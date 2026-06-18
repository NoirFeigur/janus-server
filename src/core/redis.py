"""Redis 异步客户端（最小生命周期）。

只负责连接的**装配与回收**，外加一个健康探测 ``ping()``。发布订阅、配额计数等
业务原语**不在这里**——它们随各自的消费者（gateway 配额、MCP 协调）在对应里程碑
落地，避免提前堆砌无消费者的基建。

业务缓存与配额计数走 ``settings.redis_url``（db 0）；ARQ 任务队列走
``settings.redis_arq_url``（db 1），由 ARQ 自己持有，不经此模块。

进程级单例：``get_redis()`` 首次调用建连接池，后续复用同一 client；``close_redis()``
在 lifespan 关闭时回收。``decode_responses=True`` 让读写直接走 ``str``，省去逐处解码。
"""

from __future__ import annotations

from redis.asyncio import ConnectionPool, Redis

from src.config import get_settings

_client: Redis | None = None


def get_redis() -> Redis:
    """返回进程级共享的异步 Redis client（首次调用建连接池，后续复用）。

    用 ``ConnectionPool.from_url`` + ``Redis(connection_pool=...)`` 装配（而非模块级
    ``from_url``）：前者带完整类型标注，后者在 redis 5.x 的 stub 里是 untyped，
    在 ``mypy --strict`` 下会触发 ``no-untyped-call``。

    带 socket 超时（``socket_connect_timeout`` / ``socket_timeout``）：redis-py 默认
    无超时，Redis 卡死或网络黑洞时调用会无限挂起，拖垮整个事件循环（鉴权热路径、
    配额计数都走 Redis）。设上限后失败快速上抛，由调用方降级。
    """
    global _client
    if _client is None:
        settings = get_settings()
        pool = ConnectionPool.from_url(
            settings.redis_url,
            decode_responses=True,
            socket_connect_timeout=settings.redis_socket_connect_timeout_seconds,
            socket_timeout=settings.redis_socket_timeout_seconds,
        )
        _client = Redis(connection_pool=pool)
    return _client


async def close_redis() -> None:
    """关闭共享 client 并释放连接池（lifespan 关闭时调用，幂等）。"""
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


async def ping() -> bool:
    """探测 Redis 可达性。可达返回 True；不可达让底层异常上抛由调用方处理。"""
    return bool(await get_redis().ping())
