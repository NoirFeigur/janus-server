"""Tests for the cache-aside utility (src/core/cache.py).

Behaviour contracts, not snapshots:
- miss → loader runs once, value is stored, second read is a hit (loader not re-run)
- invalidate → next read is a miss again (loader re-runs)
- Redis errors fail open: loader still serves, request never breaks
- corrupt cache entry → dropped, loader re-runs
"""

from __future__ import annotations

import json

import pytest
from fakeredis.aioredis import FakeRedis
from redis.exceptions import RedisError

from src.core import cache

pytestmark = pytest.mark.asyncio


def _int_codec() -> tuple:
    return (lambda v: json.dumps(v), lambda s: int(json.loads(s)))


async def test_miss_then_hit_runs_loader_once(fake_redis: FakeRedis) -> None:
    calls = {"n": 0}

    async def loader() -> int:
        calls["n"] += 1
        return 42

    dumps, loads = _int_codec()

    first = await cache.get_or_load(
        "k:1", loader, ttl_seconds=60, dumps=dumps, loads=loads
    )
    second = await cache.get_or_load(
        "k:1", loader, ttl_seconds=60, dumps=dumps, loads=loads
    )

    assert first == 42
    assert second == 42
    assert calls["n"] == 1  # second read served from cache, loader not re-run


async def test_value_actually_stored_in_redis(fake_redis: FakeRedis) -> None:
    dumps, loads = _int_codec()

    async def loader() -> int:
        return 7

    await cache.get_or_load("k:store", loader, ttl_seconds=60, dumps=dumps, loads=loads)

    raw = await fake_redis.get("k:store")
    assert raw is not None
    assert int(json.loads(raw)) == 7


async def test_ttl_applied(fake_redis: FakeRedis) -> None:
    dumps, loads = _int_codec()

    async def loader() -> int:
        return 1

    await cache.get_or_load("k:ttl", loader, ttl_seconds=123, dumps=dumps, loads=loads)

    ttl = await fake_redis.ttl("k:ttl")
    assert 0 < ttl <= 123


async def test_invalidate_forces_reload(fake_redis: FakeRedis) -> None:
    calls = {"n": 0}

    async def loader() -> int:
        calls["n"] += 1
        return calls["n"]

    dumps, loads = _int_codec()

    first = await cache.get_or_load("k:inv", loader, ttl_seconds=60, dumps=dumps, loads=loads)
    await cache.invalidate("k:inv")
    second = await cache.get_or_load("k:inv", loader, ttl_seconds=60, dumps=dumps, loads=loads)

    assert first == 1
    assert second == 2  # loader re-ran after invalidation
    assert calls["n"] == 2


async def test_corrupt_entry_dropped_and_reloads(fake_redis: FakeRedis) -> None:
    # Poison the cache with a value that the codec cannot decode.
    await fake_redis.set("k:bad", "not-json")
    calls = {"n": 0}

    async def loader() -> int:
        calls["n"] += 1
        return 99

    dumps, loads = _int_codec()
    value = await cache.get_or_load("k:bad", loader, ttl_seconds=60, dumps=dumps, loads=loads)

    assert value == 99
    assert calls["n"] == 1  # corrupt entry ignored, loader served

    # Self-heal: the poison entry was overwritten by the reload's set, so a
    # second read is a clean hit (loader not re-run) and decodes correctly.
    again = await cache.get_or_load("k:bad", loader, ttl_seconds=60, dumps=dumps, loads=loads)
    assert again == 99
    assert calls["n"] == 1  # served from the now-healed cache, no reload
    assert int(json.loads(await fake_redis.get("k:bad"))) == 99  # poison gone


async def test_get_failure_fails_open_to_loader(
    fake_redis: FakeRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def boom(*_args: object, **_kwargs: object) -> object:
        raise RedisError("redis down")

    monkeypatch.setattr(fake_redis, "get", boom)
    calls = {"n": 0}

    async def loader() -> int:
        calls["n"] += 1
        return 5

    dumps, loads = _int_codec()
    value = await cache.get_or_load("k:down", loader, ttl_seconds=60, dumps=dumps, loads=loads)

    assert value == 5  # Redis outage degraded to DB, request did not break
    assert calls["n"] == 1


async def test_set_failure_still_returns_value(
    fake_redis: FakeRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def boom(*_args: object, **_kwargs: object) -> object:
        raise RedisError("write failed")

    monkeypatch.setattr(fake_redis, "set", boom)

    async def loader() -> int:
        return 8

    dumps, loads = _int_codec()
    value = await cache.get_or_load("k:nowrite", loader, ttl_seconds=60, dumps=dumps, loads=loads)

    assert value == 8  # set failure swallowed, loader value still returned


async def test_invalidate_failure_is_swallowed(
    fake_redis: FakeRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def boom(*_args: object, **_kwargs: object) -> object:
        raise RedisError("delete failed")

    monkeypatch.setattr(fake_redis, "delete", boom)

    # Should not raise.
    await cache.invalidate("k:whatever")


async def test_invalidate_noop_on_empty(fake_redis: FakeRedis) -> None:
    # No keys → no Redis call, no error.
    await cache.invalidate()
