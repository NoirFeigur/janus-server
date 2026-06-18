"""Semantics tests for the in-memory Redis double's session-store surface.

The session store (B1) relies on exact Redis semantics for ``exists`` (counts
presence), ``getdel`` (atomic read+remove — the refresh-rotation serialization
point), and the set ops (``sadd``/``srem``/``smembers`` — per-user session
indexes). If the double's semantics drift from Redis, session-store tests could
pass for the wrong reason. These tests pin the double itself.
"""

from __future__ import annotations

import pytest

from tests._async_redis_double import AsyncRedisDouble

pytestmark = pytest.mark.asyncio


@pytest.fixture
def redis() -> AsyncRedisDouble:
    return AsyncRedisDouble(decode_responses=True)


async def test_exists_counts_present_keys(redis: AsyncRedisDouble) -> None:
    await redis.set("a", "1")
    await redis.set("b", "2")
    assert await redis.exists("a") == 1
    assert await redis.exists("a", "b") == 2
    assert await redis.exists("a", "missing") == 1
    assert await redis.exists("missing") == 0


async def test_exists_counts_repeated_key_each_time(redis: AsyncRedisDouble) -> None:
    await redis.set("a", "1")
    assert await redis.exists("a", "a") == 2


async def test_getdel_returns_value_and_removes_key(redis: AsyncRedisDouble) -> None:
    await redis.set("refresh", "payload")
    assert await redis.getdel("refresh") == "payload"
    assert await redis.get("refresh") is None
    assert await redis.exists("refresh") == 0


async def test_getdel_missing_key_returns_none(redis: AsyncRedisDouble) -> None:
    assert await redis.getdel("nope") is None


async def test_getdel_is_single_winner_under_repeat(redis: AsyncRedisDouble) -> None:
    """Refresh rotation: the first consume wins, a second sees nothing."""
    await redis.set("refresh", "payload")
    first = await redis.getdel("refresh")
    second = await redis.getdel("refresh")
    assert first == "payload"
    assert second is None


async def test_sadd_returns_newly_added_count(redis: AsyncRedisDouble) -> None:
    assert await redis.sadd("user:1", "jti-a") == 1
    assert await redis.sadd("user:1", "jti-a") == 0  # already present
    assert await redis.sadd("user:1", "jti-b", "jti-c") == 2
    assert await redis.sadd("user:1", "jti-c", "jti-d") == 1  # only jti-d is new


async def test_smembers_returns_all_members(redis: AsyncRedisDouble) -> None:
    await redis.sadd("user:1", "jti-a", "jti-b")
    assert await redis.smembers("user:1") == {"jti-a", "jti-b"}


async def test_smembers_missing_key_is_empty_set(redis: AsyncRedisDouble) -> None:
    assert await redis.smembers("user:nope") == set()


async def test_smembers_returns_a_copy(redis: AsyncRedisDouble) -> None:
    """Mutating the returned set must not corrupt the stored set."""
    await redis.sadd("user:1", "jti-a")
    members = await redis.smembers("user:1")
    members.add("injected")
    assert await redis.smembers("user:1") == {"jti-a"}


async def test_srem_removes_members_and_counts(redis: AsyncRedisDouble) -> None:
    await redis.sadd("user:1", "jti-a", "jti-b", "jti-c")
    assert await redis.srem("user:1", "jti-a", "missing") == 1
    assert await redis.smembers("user:1") == {"jti-b", "jti-c"}


async def test_srem_drops_key_when_set_emptied(redis: AsyncRedisDouble) -> None:
    await redis.sadd("user:1", "jti-a")
    assert await redis.srem("user:1", "jti-a") == 1
    assert await redis.exists("user:1") == 0


async def test_srem_missing_key_returns_zero(redis: AsyncRedisDouble) -> None:
    assert await redis.srem("user:nope", "jti-a") == 0


async def test_delete_removes_set_key(redis: AsyncRedisDouble) -> None:
    await redis.sadd("user:1", "jti-a", "jti-b")
    assert await redis.delete("user:1") == 1
    assert await redis.smembers("user:1") == set()


async def test_expire_sets_ttl_on_existing_key(redis: AsyncRedisDouble) -> None:
    await redis.set("k", "v")
    assert await redis.ttl("k") == -1  # no expiry yet
    assert await redis.expire("k", 100) is True
    assert 1 <= await redis.ttl("k") <= 100


async def test_expire_on_set_key(redis: AsyncRedisDouble) -> None:
    await redis.sadd("user:1", "jti-a")
    assert await redis.expire("user:1", 100) is True
    assert 1 <= await redis.ttl("user:1") <= 100


async def test_expire_missing_key_returns_false(redis: AsyncRedisDouble) -> None:
    assert await redis.expire("nope", 100) is False


async def test_incr_missing_key_starts_at_one(redis: AsyncRedisDouble) -> None:
    assert await redis.incr("c") == 1


async def test_incr_accumulates(redis: AsyncRedisDouble) -> None:
    await redis.incr("c")
    await redis.incr("c")
    assert await redis.incr("c") == 3


async def test_incr_preserves_existing_ttl(redis: AsyncRedisDouble) -> None:
    """INCR must not touch an existing key's expiry (only SET/EXPIRE do)."""
    await redis.incr("c")
    await redis.expire("c", 100)
    await redis.incr("c")
    assert 1 <= await redis.ttl("c") <= 100


async def test_incr_reflected_in_get(redis: AsyncRedisDouble) -> None:
    await redis.incr("c")
    assert await redis.get("c") == "1"
