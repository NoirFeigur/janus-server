"""Tests for the race-free permission snapshot cache (src/auth/perm_cache.py).

Behaviour contracts, not snapshots:
- miss → loader runs once; hit → loader not re-run (per-user versioned key)
- invalidate_user → only that user's next read misses (per-user generation bump)
- invalidate_all → every user's next read misses (global generation bump)
- **race closure (the security property)**: a reader that loads a stale snapshot
  while a concurrent revocation commits cannot resurrect the revoked perms — its
  stale write lands on an orphaned old-generation key, and the next read (seeing
  the bumped generation) misses and reloads fresh.
- Redis errors fail open: a generation-read failure bypasses the cache and loads
  straight from the source — never serves a stale snapshot.
- encode/decode round-trips the frozenset payload.
"""

from __future__ import annotations

import pytest
from redis.exceptions import RedisError

from src.auth import perm_cache
from src.auth.perm_cache import PermissionSnapshot
from tests._async_redis_double import AsyncRedisDouble

pytestmark = pytest.mark.asyncio


def _snap(perms: set[str], roles: set[str] = frozenset()) -> PermissionSnapshot:
    return PermissionSnapshot(permissions=frozenset(perms), role_codes=frozenset(roles))


async def test_encode_decode_roundtrip() -> None:
    snap = _snap({"user:read", "user:write"}, {"admin"})
    assert perm_cache.decode(perm_cache.encode(snap)) == snap


async def test_miss_then_hit_runs_loader_once(fake_redis: AsyncRedisDouble) -> None:
    calls = {"n": 0}

    async def loader() -> PermissionSnapshot:
        calls["n"] += 1
        return _snap({"user:read"})

    first = await perm_cache.load_snapshot(1, loader)
    second = await perm_cache.load_snapshot(1, loader)

    assert first == second == _snap({"user:read"})
    assert calls["n"] == 1  # second read served from cache, loader not re-run


async def test_invalidate_user_forces_reload(fake_redis: AsyncRedisDouble) -> None:
    calls = {"n": 0}

    async def loader() -> PermissionSnapshot:
        calls["n"] += 1
        return _snap({f"perm:{calls['n']}"})

    first = await perm_cache.load_snapshot(7, loader)
    await perm_cache.invalidate_user(7)
    second = await perm_cache.load_snapshot(7, loader)

    assert first == _snap({"perm:1"})
    assert second == _snap({"perm:2"})  # reloaded after the per-user bump
    assert calls["n"] == 2


async def test_invalidate_user_does_not_affect_other_users(
    fake_redis: AsyncRedisDouble,
) -> None:
    async def loader_a() -> PermissionSnapshot:
        return _snap({"a"})

    async def loader_b() -> PermissionSnapshot:
        return _snap({"b"})

    await perm_cache.load_snapshot(1, loader_a)
    await perm_cache.load_snapshot(2, loader_b)
    await perm_cache.invalidate_user(1)  # bump ONLY user 1

    calls = {"n": 0}

    async def loader_b_again() -> PermissionSnapshot:
        calls["n"] += 1
        return _snap({"b"})

    # User 2's snapshot is untouched — still a hit, loader not re-run.
    again = await perm_cache.load_snapshot(2, loader_b_again)
    assert again == _snap({"b"})
    assert calls["n"] == 0


async def test_invalidate_all_forces_reload_for_every_user(
    fake_redis: AsyncRedisDouble,
) -> None:
    calls = {"u1": 0, "u2": 0}

    async def loader_1() -> PermissionSnapshot:
        calls["u1"] += 1
        return _snap({f"u1:{calls['u1']}"})

    async def loader_2() -> PermissionSnapshot:
        calls["u2"] += 1
        return _snap({f"u2:{calls['u2']}"})

    await perm_cache.load_snapshot(1, loader_1)
    await perm_cache.load_snapshot(2, loader_2)
    await perm_cache.invalidate_all()  # one global bump invalidates everyone

    second_1 = await perm_cache.load_snapshot(1, loader_1)
    second_2 = await perm_cache.load_snapshot(2, loader_2)

    assert second_1 == _snap({"u1:2"})  # both reloaded
    assert second_2 == _snap({"u2:2"})
    assert calls == {"u1": 2, "u2": 2}


async def test_stale_loader_cannot_resurrect_revoked_perms(
    fake_redis: AsyncRedisDouble,
) -> None:
    """The core security property: the cache-aside stale-repopulation race is closed.

    Models the worst-case interleaving: reader A reads the generation, then a
    concurrent permission revocation COMMITS (bumping the user generation) before
    A writes its loaded value. A's stale value is written to the now-orphaned
    old-generation key; reader B, seeing the bumped generation, composes a
    different key, misses, and loads the fresh (revoked) permission set. The stale
    value never resurfaces.
    """
    user_id = 123
    stale = _snap({"old:dangerous:perm"})
    fresh = _snap({"new:safe:perm"})

    async def racing_loader() -> PermissionSnapshot:
        # A revocation commits between A's generation-read and A's cache-set.
        await perm_cache.invalidate_user(user_id)
        return stale

    # Reader A: reads gen (0,0); loader runs and the revocation bumps user gen to
    # 1; A writes `stale` to the orphaned snap:...:0:0 key.
    a = await perm_cache.load_snapshot(user_id, racing_loader)
    assert a == stale  # A returns what it loaded — it cannot see the future

    # Reader B: reads gen (0,1) → composes snap:...:0:1 → MISS → loads fresh.
    fresh_calls = {"n": 0}

    async def fresh_loader() -> PermissionSnapshot:
        fresh_calls["n"] += 1
        return fresh

    b = await perm_cache.load_snapshot(user_id, fresh_loader)
    assert b == fresh  # revoked perms did NOT survive — the race is closed
    assert fresh_calls["n"] == 1  # B genuinely missed and reloaded

    # And the fresh value is now cached under the new key (stable hit).
    c = await perm_cache.load_snapshot(user_id, fresh_loader)
    assert c == fresh
    assert fresh_calls["n"] == 1  # served from cache, no reload


async def test_gen_read_failure_bypasses_cache_to_loader(
    fake_redis: AsyncRedisDouble, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A generation-read (mget) failure degrades to a direct DB load.

    Critically it must NOT serve a stale cached snapshot — Redis being down falls
    back to the source of truth (the pre-cache per-request behaviour), never to a
    stale permission grant.
    """

    async def boom(*_args: object, **_kwargs: object) -> object:
        raise RedisError("redis down")

    monkeypatch.setattr(fake_redis, "mget", boom)
    calls = {"n": 0}

    async def loader() -> PermissionSnapshot:
        calls["n"] += 1
        return _snap({"loaded:from:db"})

    value = await perm_cache.load_snapshot(1, loader)

    assert value == _snap({"loaded:from:db"})  # served from loader, not cache
    assert calls["n"] == 1


async def test_invalidate_failures_fail_open(
    fake_redis: AsyncRedisDouble, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Redis error during invalidation is swallowed (logged), never raised.

    An after-commit hook must not blow up a successful request just because the
    cache bump failed — the short TTL backstop bounds the resulting staleness.
    """

    async def boom(*_args: object, **_kwargs: object) -> object:
        raise RedisError("redis down")

    monkeypatch.setattr(fake_redis, "incr", boom)

    # Neither call raises despite the INCR failing.
    await perm_cache.invalidate_user(1)
    await perm_cache.invalidate_all()


async def test_snapshot_stored_under_versioned_key(
    fake_redis: AsyncRedisDouble,
) -> None:
    """The snapshot lands on the generation-composed key (gen counters start at 0)."""

    async def loader() -> PermissionSnapshot:
        return _snap({"x"})

    await perm_cache.load_snapshot(42, loader)

    # Fresh user: both generations absent → read as 0 → key suffix :0:0.
    raw = await fake_redis.get("auth:perm:snap:42:0:0")
    assert raw is not None
    assert perm_cache.decode(raw) == _snap({"x"})
