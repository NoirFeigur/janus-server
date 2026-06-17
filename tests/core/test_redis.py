"""Tests for the minimal async Redis client (src/core/redis.py).

These use the ``fake_redis`` fixture (conftest) which swaps the module singleton
for an in-process fake — the shared Redis instance is never touched. They assert
lifecycle behavior (singleton reuse, ping reachability, close resets state).
"""

from __future__ import annotations

import src.core.redis as redis_module
from src.core.redis import get_redis, ping


async def test_get_redis_returns_singleton(fake_redis: object) -> None:
    """get_redis() 多次调用返回同一个 client 实例(进程级单例)。"""
    first = get_redis()
    second = get_redis()
    assert first is second
    assert first is fake_redis


async def test_ping_returns_true_when_reachable(fake_redis: object) -> None:
    assert await ping() is True


async def test_set_get_roundtrip_through_singleton(fake_redis: object) -> None:
    """经单例读写应同源(decode_responses=True 让返回值是 str)。"""
    client = get_redis()
    await client.set("k", "v")
    assert await client.get("k") == "v"


async def test_close_redis_resets_singleton(fake_redis: object) -> None:
    """close_redis() 后 _client 应被重置,下次 get_redis() 会重建。"""
    get_redis()
    assert redis_module._client is not None
    await redis_module.close_redis()
    assert redis_module._client is None


async def test_get_redis_lazily_builds_client_when_unset(fake_redis: object) -> None:
    """_client 为 None 时,get_redis() 走懒建分支(建连接池 + Redis 实例)。

    autouse fixture 平时把 _client 设成 double;这里显式清空以触发真正的懒建路径,
    断言返回的是 redis.asyncio.Redis 实例,然后立即回收以免泄漏到后续测试。
    """
    from redis.asyncio import Redis

    redis_module._client = None
    built = get_redis()
    try:
        assert isinstance(built, Redis)
        assert redis_module._client is built  # 缓存为单例
    finally:
        await redis_module.close_redis()  # 回收懒建出来的真实 client
