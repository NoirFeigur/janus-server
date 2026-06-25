"""Hot-path Redis cache with generation-versioned keys.

Eliminates per-request DB queries for model resolution and quota config lookup.
Uses the existing ``src/core/cache.get_or_load`` pattern augmented with
generation counters: admin writes INCR a generation key, making all prior
cache entries unaddressable (keys embed the generation number).

Cache policy:
- Model resolution: cache-aside, 30s TTL, positive results only.
- Quota config: cache-aside, 30s TTL, always hit Redis Lua for actual counters.
- Fail-open: Redis down → bypass cache, hit DB directly (existing behavior).
"""

from __future__ import annotations

import json
import random
from contextlib import suppress
from typing import Any

from redis.exceptions import RedisError

from src.core.logging import get_logger
from src.core.redis import get_redis

_log = get_logger(__name__)

# ---------------------------------------------------------------------------
# Generation key constants
# ---------------------------------------------------------------------------

CATALOG_GEN_KEY = "janus:gw:v1:catalog:gen"
GRANT_GEN_USER_PREFIX = "janus:gw:v1:grant:gen:u:"
GRANT_GEN_DEPT_PREFIX = "janus:gw:v1:grant:gen:d:"
QUOTA_GEN_GLOBAL_KEY = "janus:gw:v1:quota:gen:global"
QUOTA_GEN_USER_PREFIX = "janus:gw:v1:quota:gen:u:"
QUOTA_GEN_DEPT_PREFIX = "janus:gw:v1:quota:gen:d:"

# Cache TTL
_BASE_TTL_SECONDS = 30
_TTL_JITTER_SECONDS = 5


def _ttl() -> int:
    """30s TTL + small jitter to prevent thundering herd."""
    return _BASE_TTL_SECONDS + random.randint(0, _TTL_JITTER_SECONDS)


# ---------------------------------------------------------------------------
# Generation management
# ---------------------------------------------------------------------------


async def get_generation(key: str) -> int:
    """Get current generation value (0 if key doesn't exist or Redis is down)."""
    try:
        redis = get_redis()
        val = await redis.get(key)
        return int(val) if val is not None else 0
    except (RedisError, ValueError):
        return 0


async def bump_generation(key: str) -> int:
    """Increment a generation counter (after admin write commits)."""
    try:
        redis = get_redis()
        return int(await redis.incr(key))
    except RedisError as exc:
        _log.warning("cache.bump_generation_failed", key=key, error=str(exc))
        return 0


async def bump_catalog_generation() -> int:
    """Bump catalog generation (call after channel/model/deployment writes)."""
    return await bump_generation(CATALOG_GEN_KEY)


async def bump_grant_generation(*, user_id: int | None = None, dept_id: int | None = None) -> None:
    """Bump grant generation for specific user/dept (call after grant writes)."""
    if user_id is not None:
        await bump_generation(f"{GRANT_GEN_USER_PREFIX}{user_id}")
    if dept_id is not None:
        await bump_generation(f"{GRANT_GEN_DEPT_PREFIX}{dept_id}")


async def bump_quota_generation(
    *, user_id: int | None = None, dept_id: int | None = None, is_global: bool = False
) -> None:
    """Bump quota generation (call after quota rule writes)."""
    if is_global:
        await bump_generation(QUOTA_GEN_GLOBAL_KEY)
    if user_id is not None:
        await bump_generation(f"{QUOTA_GEN_USER_PREFIX}{user_id}")
    if dept_id is not None:
        await bump_generation(f"{QUOTA_GEN_DEPT_PREFIX}{dept_id}")


# ---------------------------------------------------------------------------
# Model resolution cache
# ---------------------------------------------------------------------------


def _resolve_cache_key(
    catalog_gen: int, user_gen: int, dept_gen: int, user_id: int, dept_id: int, model: str
) -> str:
    """Build a generation-versioned cache key for model resolution."""
    return (
        f"janus:gw:v1:resolve:{catalog_gen}:{user_gen}:{dept_gen}"
        f":u:{user_id}:d:{dept_id}:m:{model}"
    )


async def get_cached_resolution(
    user_id: int, dept_id: int | None, requested_model: str
) -> dict[str, Any] | None:
    """Try to load a cached model resolution result.

    Returns None on cache miss or Redis failure (fail-open).
    """
    try:
        redis = get_redis()
        catalog_gen = await get_generation(CATALOG_GEN_KEY)
        user_gen = await get_generation(f"{GRANT_GEN_USER_PREFIX}{user_id}")
        dept_gen = await get_generation(f"{GRANT_GEN_DEPT_PREFIX}{dept_id or 0}")

        key = _resolve_cache_key(
            catalog_gen, user_gen, dept_gen, user_id, dept_id or 0, requested_model
        )
        raw = await redis.get(key)
        if raw is None:
            return None
        result: dict[str, Any] = json.loads(raw)
        return result
    except (RedisError, json.JSONDecodeError, ValueError):
        return None


async def set_cached_resolution(
    user_id: int, dept_id: int | None, requested_model: str, data: dict[str, Any]
) -> None:
    """Store a model resolution result in cache (fail-open)."""
    with suppress(Exception):
        redis = get_redis()
        catalog_gen = await get_generation(CATALOG_GEN_KEY)
        user_gen = await get_generation(f"{GRANT_GEN_USER_PREFIX}{user_id}")
        dept_gen = await get_generation(f"{GRANT_GEN_DEPT_PREFIX}{dept_id or 0}")

        key = _resolve_cache_key(
            catalog_gen, user_gen, dept_gen, user_id, dept_id or 0, requested_model
        )
        await redis.set(key, json.dumps(data, default=str), ex=_ttl())


# ---------------------------------------------------------------------------
# Quota config cache
# ---------------------------------------------------------------------------


def _quota_cache_key(
    global_gen: int, user_gen: int, dept_gen: int, user_id: int, dept_id: int, model_id: int
) -> str:
    return (
        f"janus:gw:v1:quota_cfg:{global_gen}:{user_gen}:{dept_gen}"
        f":u:{user_id}:d:{dept_id}:m:{model_id}"
    )


async def get_cached_quota_config(
    user_id: int, dept_id: int | None, model_id: int
) -> list[dict[str, Any]] | None:
    """Try to load cached quota rules. Returns None on miss (fail-open)."""
    try:
        redis = get_redis()
        global_gen = await get_generation(QUOTA_GEN_GLOBAL_KEY)
        user_gen = await get_generation(f"{QUOTA_GEN_USER_PREFIX}{user_id}")
        dept_gen = await get_generation(f"{QUOTA_GEN_DEPT_PREFIX}{dept_id or 0}")

        key = _quota_cache_key(global_gen, user_gen, dept_gen, user_id, dept_id or 0, model_id)
        raw = await redis.get(key)
        if raw is None:
            return None
        result: list[dict[str, Any]] = json.loads(raw)
        return result
    except (RedisError, json.JSONDecodeError, ValueError):
        return None


async def set_cached_quota_config(
    user_id: int, dept_id: int | None, model_id: int, data: list[dict[str, Any]]
) -> None:
    """Store quota config in cache (fail-open)."""
    with suppress(Exception):
        redis = get_redis()
        global_gen = await get_generation(QUOTA_GEN_GLOBAL_KEY)
        user_gen = await get_generation(f"{QUOTA_GEN_USER_PREFIX}{user_id}")
        dept_gen = await get_generation(f"{QUOTA_GEN_DEPT_PREFIX}{dept_id or 0}")

        key = _quota_cache_key(global_gen, user_gen, dept_gen, user_id, dept_id or 0, model_id)
        await redis.set(key, json.dumps(data, default=str), ex=_ttl())
