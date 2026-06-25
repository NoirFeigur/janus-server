"""Redis 异步客户端（最小生命周期）。

只负责连接的**装配与回收**，外加一个健康探测 ``ping()``。发布订阅、配额计数等
业务原语**不在这里**——它们随各自的消费者（gateway 配额、MCP 协调）在对应里程碑
落地，避免提前堆砌无消费者的基建。

业务缓存与配额计数走 ``settings.redis_url``（db 0）；ARQ 任务队列走
``settings.redis_arq_url``（db 1），由 ARQ 自己持有，不经此模块。

进程级单例：``get_redis()`` 首次调用建连接池，后续复用同一 client；``close_redis()``
在 lifespan 关闭时回收。``decode_responses=True`` 让读写直接走 ``str``，省去逐处解码。

类型：redis-py 5.x 的 ``Redis`` 类同时继承同步和异步 Commands，方法签名是
``Awaitable[X] | X``。在 ``mypy --strict`` 下 ``await redis.xxx(...)`` 会触发
``misc`` 错（"actual type Awaitable[X] | int"）。``get_redis()`` 通过返回
``AsyncRedis`` 协议（只声明异步形态）让调用方静态时拿到干净的 awaitable，
运行时仍是真实的 redis 客户端。"""

from __future__ import annotations

import builtins
from collections.abc import Awaitable, Iterable
from typing import Any, Protocol, cast, runtime_checkable

from redis.asyncio import ConnectionPool, Redis

from src.config import get_settings


@runtime_checkable
class AsyncRedis(Protocol):
    """Async-only typed surface of the redis.asyncio.Redis client.

    Mirrors the methods we actually use across the codebase, with returns
    declared as ``Awaitable[T]`` so ``await`` narrows cleanly. The real
    underlying ``Redis`` instance honours all these calls; this Protocol
    exists purely to keep ``mypy --strict`` happy without dropping a
    ``# type: ignore`` at every call site.
    """

    # --- Strings ---
    def get(self, name: str) -> Awaitable[str | None]: ...
    def set(
        self,
        name: str,
        value: str,
        ex: int | None = None,
        *,
        nx: bool = False,
    ) -> Awaitable[bool | None]: ...
    def mget(self, *keys: str | Iterable[str]) -> Awaitable[list[str | None]]: ...
    def delete(self, *names: str) -> Awaitable[int]: ...
    def exists(self, *names: str) -> Awaitable[int]: ...
    def expire(self, name: str, time: int) -> Awaitable[bool]: ...
    def pexpire(self, name: str, time: int) -> Awaitable[bool]: ...
    def ttl(self, name: str) -> Awaitable[int]: ...
    def getdel(self, name: str) -> Awaitable[str | None]: ...
    def incr(self, name: str, amount: int = 1) -> Awaitable[int]: ...
    def incrby(self, name: str, amount: int = 1) -> Awaitable[int]: ...
    def decr(self, name: str, amount: int = 1) -> Awaitable[int]: ...
    def decrby(self, name: str, amount: int = 1) -> Awaitable[int]: ...
    def ping(self) -> Awaitable[bool]: ...

    # --- Sets ---
    # NOTE: ``builtins.set`` rather than ``set`` — the ``set`` method above
    # shadows the built-in set type inside this class body when annotations
    # are evaluated lazily (``from __future__ import annotations``).
    def sadd(self, name: str, *values: str) -> Awaitable[int]: ...
    def srem(self, name: str, *values: str) -> Awaitable[int]: ...
    def smembers(self, name: str) -> Awaitable[builtins.set[str]]: ...
    def sismember(self, name: str, value: str) -> Awaitable[bool]: ...

    # --- Lists ---
    def lpush(self, name: str, *values: str) -> Awaitable[int]: ...
    def rpush(self, name: str, *values: str) -> Awaitable[int]: ...
    def lpop(
        self, name: str, count: int | None = None
    ) -> Awaitable[str | list[str] | None]: ...
    def lrem(self, name: str, count: int, value: str) -> Awaitable[int]: ...
    def llen(self, name: str) -> Awaitable[int]: ...
    def lrange(self, name: str, start: int, end: int) -> Awaitable[list[str]]: ...

    # --- Hashes ---
    def hset(
        self,
        name: str,
        key: str | None = None,
        value: str | None = None,
        mapping: dict[str, str] | None = None,
    ) -> Awaitable[int]: ...
    def hget(self, name: str, key: str) -> Awaitable[str | None]: ...
    def hmget(self, name: str, *keys: str) -> Awaitable[list[str | None]]: ...
    def hgetall(self, name: str) -> Awaitable[dict[str, str]]: ...
    def hincrby(self, name: str, key: str, amount: int = 1) -> Awaitable[int]: ...

    # --- Sorted sets ---
    def zadd(
        self,
        name: str,
        mapping: dict[str, float] | None = None,
        **kwargs: float,
    ) -> Awaitable[int]: ...
    def zrem(self, name: str, *values: str) -> Awaitable[int]: ...
    def zcard(self, name: str) -> Awaitable[int]: ...
    def zremrangebyscore(
        self, name: str, min: float | str, max: float | str
    ) -> Awaitable[int]: ...

    # --- Scripting / pipelines / pubsub ---
    def eval(self, script: str, numkeys: int, *keys_and_args: Any) -> Awaitable[Any]: ...
    def register_script(self, script: str) -> Any: ...
    def pipeline(self, transaction: bool = True) -> Any: ...
    def pubsub(self) -> Any: ...
    def publish(self, channel: str, message: str) -> Awaitable[int]: ...

    # --- Lifecycle ---
    def aclose(self) -> Awaitable[None]: ...


_client: Redis | None = None


def get_redis() -> AsyncRedis:
    """返回进程级共享的异步 Redis client（首次调用建连接池，后续复用）。

    用 ``ConnectionPool.from_url`` + ``Redis(connection_pool=...)`` 装配（而非模块级
    ``from_url``）：前者带完整类型标注，后者在 redis 5.x 的 stub 里是 untyped，
    在 ``mypy --strict`` 下会触发 ``no-untyped-call``。

    带 socket 超时（``socket_connect_timeout`` / ``socket_timeout``）：redis-py 默认
    无超时，Redis 卡死或网络黑洞时调用会无限挂起，拖垮整个事件循环（鉴权热路径、
    配额计数都走 Redis）。设上限后失败快速上抛，由调用方降级。

    返回的 :class:`AsyncRedis` 协议剥离了 redis-py 在 5.x 暴露的 sync/async 联合
    返回签名，让 ``mypy --strict`` 不再在每个 await 上炸 ``misc``；底层仍是原生
    :class:`redis.asyncio.Redis` 客户端。
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
    return cast("AsyncRedis", _client)


async def close_redis() -> None:
    """关闭共享 client 并释放连接池（lifespan 关闭时调用，幂等）。"""
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


async def ping() -> bool:
    """探测 Redis 可达性。可达返回 True；不可达让底层异常上抛由调用方处理。"""
    return bool(await get_redis().ping())
