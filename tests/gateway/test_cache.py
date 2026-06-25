"""Unit tests for src/gateway/cache.py — generation-versioned Redis cache."""

from __future__ import annotations

import pytest

from src.gateway.cache import (
    CATALOG_GEN_KEY,
    GRANT_GEN_DEPT_PREFIX,
    GRANT_GEN_USER_PREFIX,
    QUOTA_GEN_GLOBAL_KEY,
    QUOTA_GEN_USER_PREFIX,
    bump_catalog_generation,
    bump_generation,
    bump_grant_generation,
    bump_quota_generation,
    get_cached_quota_config,
    get_cached_resolution,
    get_generation,
    set_cached_quota_config,
    set_cached_resolution,
)
from tests._async_redis_double import AsyncRedisDouble


@pytest.mark.asyncio
async def test_get_generation_default_zero(fake_redis: AsyncRedisDouble) -> None:
    assert await get_generation("nonexistent:key") == 0


@pytest.mark.asyncio
async def test_bump_generation_increments(fake_redis: AsyncRedisDouble) -> None:
    val = await bump_generation(CATALOG_GEN_KEY)
    assert val == 1
    val = await bump_generation(CATALOG_GEN_KEY)
    assert val == 2


@pytest.mark.asyncio
async def test_bump_catalog_generation(fake_redis: AsyncRedisDouble) -> None:
    val = await bump_catalog_generation()
    assert val == 1
    assert await get_generation(CATALOG_GEN_KEY) == 1


@pytest.mark.asyncio
async def test_bump_grant_generation_user(fake_redis: AsyncRedisDouble) -> None:
    await bump_grant_generation(user_id=10)
    assert await get_generation(f"{GRANT_GEN_USER_PREFIX}10") == 1


@pytest.mark.asyncio
async def test_bump_grant_generation_dept(fake_redis: AsyncRedisDouble) -> None:
    await bump_grant_generation(dept_id=5)
    assert await get_generation(f"{GRANT_GEN_DEPT_PREFIX}5") == 1


@pytest.mark.asyncio
async def test_bump_quota_generation_global(fake_redis: AsyncRedisDouble) -> None:
    await bump_quota_generation(is_global=True)
    assert await get_generation(QUOTA_GEN_GLOBAL_KEY) == 1


@pytest.mark.asyncio
async def test_bump_quota_generation_user(fake_redis: AsyncRedisDouble) -> None:
    await bump_quota_generation(user_id=7)
    assert await get_generation(f"{QUOTA_GEN_USER_PREFIX}7") == 1


# ---------------------------------------------------------------------------
# Model resolution cache
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolution_cache_miss(fake_redis: AsyncRedisDouble) -> None:
    result = await get_cached_resolution(user_id=1, dept_id=2, requested_model="gpt-4")
    assert result is None


@pytest.mark.asyncio
async def test_resolution_cache_hit(fake_redis: AsyncRedisDouble) -> None:
    data = {"model_id": 42, "deployments": [{"channel_id": 1}]}
    await set_cached_resolution(user_id=1, dept_id=2, requested_model="gpt-4", data=data)
    result = await get_cached_resolution(user_id=1, dept_id=2, requested_model="gpt-4")
    assert result == data


@pytest.mark.asyncio
async def test_resolution_cache_invalidated_by_generation_bump(
    fake_redis: AsyncRedisDouble,
) -> None:
    data = {"model_id": 42}
    await set_cached_resolution(user_id=1, dept_id=2, requested_model="gpt-4", data=data)

    # Bump catalog generation — old cache entry becomes unaddressable
    await bump_catalog_generation()

    result = await get_cached_resolution(user_id=1, dept_id=2, requested_model="gpt-4")
    assert result is None


@pytest.mark.asyncio
async def test_resolution_cache_dept_none(fake_redis: AsyncRedisDouble) -> None:
    """dept_id=None should work (stored as 0)."""
    data = {"model_id": 99}
    await set_cached_resolution(user_id=5, dept_id=None, requested_model="claude-3", data=data)
    result = await get_cached_resolution(user_id=5, dept_id=None, requested_model="claude-3")
    assert result == data


# ---------------------------------------------------------------------------
# Quota config cache
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_quota_config_cache_miss(fake_redis: AsyncRedisDouble) -> None:
    result = await get_cached_quota_config(user_id=1, dept_id=2, model_id=10)
    assert result is None


@pytest.mark.asyncio
async def test_quota_config_cache_hit(fake_redis: AsyncRedisDouble) -> None:
    rules = [{"id": 1, "limit": 1000, "period": "daily"}]
    await set_cached_quota_config(user_id=1, dept_id=2, model_id=10, data=rules)
    result = await get_cached_quota_config(user_id=1, dept_id=2, model_id=10)
    assert result == rules


@pytest.mark.asyncio
async def test_quota_config_cache_invalidated_by_generation_bump(
    fake_redis: AsyncRedisDouble,
) -> None:
    rules = [{"id": 1}]
    await set_cached_quota_config(user_id=1, dept_id=2, model_id=10, data=rules)

    await bump_quota_generation(is_global=True)

    result = await get_cached_quota_config(user_id=1, dept_id=2, model_id=10)
    assert result is None
