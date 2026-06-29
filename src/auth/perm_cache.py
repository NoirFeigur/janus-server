"""鉴权热路径的权限快照缓存——auth 域内的纯基础设施叶子模块。

**为什么这个模块要存在(且为何不能靠朴素短 TTL 缓存)。** 每个已鉴权请求都要把
用户的有效权限码 + 角色码聚合出来(``AuthRepository.list_permission_codes`` 是
``UserRole→Role→RoleMenu→Menu`` 四表 join,``list_active_role_codes`` 是两表 join)。
这是热路径上每请求两查的固定成本。``core.cache`` 的文档明言
**权限码默认不缓存**——朴素的 cache-aside + TTL 会留提权窗口(已撤销的权限在 TTL 内仍
被命中)。本模块刻意打破那条默认,但用一套**无竞态**的失效机制把窗口关死,而不是靠短
TTL 兜。

**朴素 cache-aside 的致命竞态(本模块要消灭的就是它)。** 读副本在写提交前 miss、查到
旧(更宽)权限,然后在写副本失效**之后**才把旧值 ``set`` 回 Redis——旧权限于是存活一个
TTL。对低敏配置值可接受(纵深防御),对权限码就是提权窗口,不可接受。

**解法:分层 generation 版本化 key。** 两个 Redis 持久计数器(无 TTL,只增不减):

- ``auth:perm:gen:global`` —— 任何**角色/菜单**变更(影响多用户)INCR 一次,一击失效所有人。
- ``auth:perm:gen:user:{id}`` —— 单用户变更(角色绑定 / 状态)INCR,只失效该用户。

快照 key 把这两个 gen 嵌进去:``auth:perm:snap:{user_id}:{global_gen}:{user_gen}``。
读路径先读两个 gen 组 key,再 cache-aside 读/种快照。

**为什么这关死了竞态(核心不变式)。** 写侧在 **commit 之后**(after-commit hook)才
INCR gen。读者把它读到的 gen **嵌进自己要写的 key**:若写侧随后 INCR,读者那次陈旧
``set`` 落在一个**孤儿旧-gen key** 上,而其后所有读者读到的是新 gen → 组出新 key → miss
→ 查库拿到新值。陈旧值再也不会被任何人读到(只靠快照 TTL 清内存,不影响正确性)。读者
**只 GET gen、从不写 gen**,故无 lost-update;gen 单调递增,故无 ABA。

**多用户失效为何用「global gen 一次 INCR」而非枚举受影响用户。** menu 变更要
``RoleMenu⋈UserRole`` join 枚举上千用户,role 删除还要在物理删 ``UserRole`` 前抢救
user_id——时序脆、代码繁。改为「global gen INCR 一次失效所有人」:这类 admin 改动罕见,
一次触发至多让当前在线用户(本规模峰值约 500)下一请求各查一次库,正是加缓存前的基线
负载,完全可接受。精确度的损失(误失效了未受影响用户的缓存)换来的是零枚举、零时序陷阱。

**Fail-open(绝不 fail-closed-broken,也绝不发陈旧权限)。** gen 读失败 → **绕过缓存
直接 loader 查库**(DB 是真相源,Redis 宕机只丢加速,绝不阻断也绝不发陈旧快照)。失效
INCR 失败 → 仅记日志;此时正确性退化为「至多陈旧一个快照 TTL(``permission_cache_ttl_seconds``,
默认 60s)」——这正是那个短 TTL 兜底的唯一用途。

只依赖 ``core.cache`` + ``core.redis`` + ``json`` + ``config``,无业务逻辑,谁都能安全 import。
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from redis.exceptions import RedisError

from src.config import get_settings
from src.core import cache
from src.core.logging import get_logger
from src.core.redis import get_redis

_log = get_logger(__name__)

_GLOBAL_GEN_KEY = "auth:perm:gen:global"
_USER_GEN_PREFIX = "auth:perm:gen:user:"
_SNAP_PREFIX = "auth:perm:snap:"


@dataclass(frozen=True, slots=True)
class PermissionSnapshot:
    """A user's cached RBAC aggregation: effective permission codes + role codes.

    Combined into one entry because ``_build_user`` always reads both together
    and every mutation that changes one can change the other — one key, one
    round-trip, one invalidation.
    """

    permissions: frozenset[str]
    role_codes: frozenset[str]


def encode(snapshot: PermissionSnapshot) -> str:
    """JSON-encode a snapshot (frozensets → sorted lists for deterministic output)."""
    return json.dumps(
        {
            "permissions": sorted(snapshot.permissions),
            "role_codes": sorted(snapshot.role_codes),
        }
    )


def decode(raw: str) -> PermissionSnapshot:
    """Decode a snapshot; raises ValueError/TypeError on malformed input.

    Those are exactly the exceptions ``core.cache.get_or_load`` treats as a dirty
    entry — it drops the bad value, falls back to the loader, and re-seeds.
    """
    data = json.loads(raw)
    return PermissionSnapshot(
        permissions=frozenset(data["permissions"]),
        role_codes=frozenset(data["role_codes"]),
    )


def _user_gen_key(user_id: int) -> str:
    return f"{_USER_GEN_PREFIX}{user_id}"


def _snapshot_key(user_id: int, global_gen: str, user_gen: str) -> str:
    return f"{_SNAP_PREFIX}{user_id}:{global_gen}:{user_gen}"


async def _read_generations(user_id: int) -> tuple[str, str]:
    """Read (global_gen, user_gen) in one round-trip; missing counters read as "0".

    A missing gen counter (never bumped yet) is treated as generation ``0`` — the
    first read for a fresh user keys on ``...:0:0``. Because the counters are
    persistent (no TTL) and only ever ``INCR`` from there, the gen a reader sees
    is monotonic; a missing counter can never resurrect an old keyed snapshot.
    """
    raw = await get_redis().mget(_GLOBAL_GEN_KEY, _user_gen_key(user_id))
    global_gen = raw[0] if raw[0] is not None else "0"
    user_gen = raw[1] if raw[1] is not None else "0"
    return global_gen, user_gen


async def load_snapshot(
    user_id: int, loader: Callable[[], Awaitable[PermissionSnapshot]]
) -> PermissionSnapshot:
    """Return the user's permission snapshot, cache-aside over the versioned key.

    Reads the layered generations, composes the snapshot key, and delegates to
    ``cache.get_or_load`` (fail-open on Redis errors). If reading the generations
    fails, bypasses the cache entirely and loads straight from the DB — Redis
    being down degrades to the pre-cache per-request query, never to a stale
    permission grant.
    """
    try:
        global_gen, user_gen = await _read_generations(user_id)
    except RedisError as exc:
        # Gen read failed: do NOT touch the cache (a key without a fresh gen could
        # serve a stale snapshot). Degrade to the source of truth.
        _log.warning("perm_cache_gen_read_failed", user_id=user_id, error=str(exc))
        return await loader()

    return await cache.get_or_load(
        _snapshot_key(user_id, global_gen, user_gen),
        loader,
        ttl_seconds=get_settings().permission_cache_ttl_seconds,
        dumps=encode,
        loads=decode,
    )


async def invalidate_user(user_id: int) -> None:
    """Invalidate one user's permission snapshot (single-user mutation).

    Bumps the per-user generation so every replica's next read for this user
    misses and reloads. Call from an after-commit hook on user-role binding or
    user-status changes. Fail-open: a Redis error is logged, not raised (the TTL
    backstop bounds staleness to one snapshot window).
    """
    try:
        await get_redis().incr(_user_gen_key(user_id))
    except RedisError as exc:
        _log.warning("perm_cache_invalidate_user_failed", user_id=user_id, error=str(exc))


async def invalidate_all() -> None:
    """Invalidate every user's permission snapshot (many-user mutation).

    Bumps the single global generation so every user's composed key changes at
    once — one ``INCR`` invalidates the whole population without enumerating
    affected users. Call from an after-commit hook on any role/menu mutation that
    can change conferred permissions (status, perms, menu bindings, deletion).
    Fail-open: a Redis error is logged, not raised.
    """
    try:
        await get_redis().incr(_GLOBAL_GEN_KEY)
    except RedisError as exc:
        _log.warning("perm_cache_invalidate_all_failed", error=str(exc))
