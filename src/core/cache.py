"""统一缓存工具（cache-aside / 旁路缓存）。

读路径标准模式：按 key 查 Redis → 命中则解码返回；未命中则调 loader 查库、回种
缓存、返回。所有方法 **fail-open**——Redis 任何异常都静默降级到 loader（DB 始终是
真相源），Redis 宕机只丢失加速，绝不让请求失败。

**不要**用它缓存「必须跨副本即时反映写入、且没有显式失效通道」的数据
（典型：RBAC 权限码、账户启用状态）——TTL 窗口意味着陈旧读，会留下提权/越权的
时间窗。这类数据要么每请求查库（M1 的选择），要么在 M2 配 pub/sub 主动失效后再缓存。

序列化交给调用方的 ``dumps``/``loads``：缓存层不臆测类型能否 JSON 往返，由消费者
显式给出编解码器（既类型安全，又能缓存非平凡结构）。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TypeVar

from redis.exceptions import RedisError

from src.core.logging import get_logger
from src.core.redis import get_redis

_log = get_logger(__name__)

T = TypeVar("T")


async def get_or_load(
    key: str,
    loader: Callable[[], Awaitable[T]],
    *,
    ttl_seconds: int,
    dumps: Callable[[T], str],
    loads: Callable[[str], T],
) -> T:
    """旁路缓存读：命中 Redis 则解码返回，未命中则 loader 查库 + 回种 + 返回。

    Fail-open：``redis.get`` 失败 → 直接走 loader（DB 真相源），缓存宕机不阻断请求；
    缓存条目解码失败（脏数据）→ 忽略该条、回退 loader，并在回种时**覆盖**掉脏值
    （即坏条目随下一次成功 ``set`` 被自愈;仅当解码与回种同时失败才残留至 TTL 过期，
    此期间每次读都 fail-open 到 loader，不影响正确性）。回种失败只记日志不抛。
    """
    redis = get_redis()
    cached: str | None
    try:
        cached = await redis.get(key)
    except RedisError as exc:
        _log.warning("cache_get_failed", key=key, error=str(exc))
        return await loader()

    if cached is not None:
        try:
            return loads(cached)
        except (ValueError, TypeError) as exc:
            # 脏/不兼容的缓存条目：丢弃，回退 loader 重新种。
            _log.warning("cache_decode_failed", key=key, error=str(exc))

    value = await loader()
    try:
        await redis.set(key, dumps(value), ex=ttl_seconds)
    except RedisError as exc:
        _log.warning("cache_set_failed", key=key, error=str(exc))
    return value


async def invalidate(*keys: str) -> None:
    """尽力删除缓存 key（fail-open：Redis 宕机仅记日志，不抛）。

    写侧（admin mutation）在提交后调用，让本副本立刻失效；跨副本的即时失效要等
    M2 的 pub/sub 通道，M1 靠短 TTL 兜陈旧窗口。
    """
    if not keys:
        return
    try:
        await get_redis().delete(*keys)
    except RedisError as exc:
        _log.warning("cache_invalidate_failed", keys=list(keys), error=str(exc))
