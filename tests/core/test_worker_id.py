"""Behavior tests for the Snowflake worker-id Redis lease (src/core/worker_id.py).

Drives ``acquire_worker_id`` / ``WorkerIdLease`` against a tiny in-memory Redis
fake that mirrors exactly the two primitives the lease uses — ``SET NX EX`` for
the atomic claim and ``eval`` for the compare-and-renew / compare-and-delete Lua
scripts. The shared ``AsyncRedisDouble`` deliberately models neither, so coupling
the lease's Lua semantics into it (consumed by many other suites) is avoided.

Asserts the contracts that matter for correctness across replicas:
- claims the first free id and pins it on the snowflake generator;
- skips ids already held by another replica;
- fail-fast outside ``local`` on pool exhaustion / Redis failure, fall-back-to-0
  inside ``local``;
- release is compare-and-delete (never drops an id a successor re-leased);
- the heartbeat renews only while we still own the id, and surfaces a lost lease.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass

import pytest

from src.core import snowflake
from src.core import worker_id as worker_id_module
from src.core.worker_id import (
    WorkerIdLease,
    WorkerIdUnavailableError,
    acquire_worker_id,
)

pytestmark = pytest.mark.asyncio


@dataclass
class _Settings:
    environment: str = "local"
    snowflake_worker_id_ttl_seconds: int = 30


class _LeaseRedis:
    """In-memory stand-in for the exact Redis surface the lease touches.

    ``set(nx=, ex=)`` models the atomic claim; ``eval`` interprets the two Lua
    scripts by their distinguishing op (``del`` = release, else renew), both
    gated on the caller's token still matching the stored value (compare-and-X).
    ``fail=True`` makes ``set`` raise to model an unreachable Redis at startup.
    """

    def __init__(
        self, *, fail: bool = False, preset: dict[str, str] | None = None
    ) -> None:
        self._data: dict[str, str] = dict(preset or {})
        self._fail = fail

    async def set(
        self, key: str, value: str, *, nx: bool = False, ex: int | None = None
    ) -> bool | None:
        if self._fail:
            raise RuntimeError("redis unreachable")
        if nx and key in self._data:
            return None
        self._data[key] = value
        return True

    async def eval(self, script: str, numkeys: int, *args: str) -> int:
        key, token = args[0], args[1]
        owns = self._data.get(key) == token
        if "del" in script:  # _RELEASE_LUA
            if owns:
                self._data.pop(key, None)
                return 1
            return 0
        # _RENEW_LUA
        return 1 if owns else 0


class _RecordingLog:
    """Captures structlog-style event calls so lease-loss can be asserted."""

    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []

    def info(self, event: str, **_: object) -> None:
        self.events.append(("info", event))

    def warning(self, event: str, **_: object) -> None:
        self.events.append(("warning", event))

    def error(self, event: str, **_: object) -> None:
        self.events.append(("error", event))


@pytest.fixture(autouse=True)
def _restore_worker_id() -> object:
    """Save/restore the process-global snowflake worker-id (test isolation)."""
    saved = snowflake._worker_id
    yield
    snowflake._worker_id = saved


def _key(worker_id: int) -> str:
    return f"{worker_id_module._KEY_PREFIX}{worker_id}"


def _install(
    monkeypatch: pytest.MonkeyPatch,
    redis: _LeaseRedis,
    *,
    environment: str = "local",
    max_worker_id: int | None = None,
) -> None:
    monkeypatch.setattr(worker_id_module, "get_redis", lambda: redis)
    monkeypatch.setattr(
        worker_id_module, "get_settings", lambda: _Settings(environment=environment)
    )
    if max_worker_id is not None:
        monkeypatch.setattr(worker_id_module, "_MAX_WORKER_ID", max_worker_id)


async def test_acquire_claims_first_free_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = _LeaseRedis()
    _install(monkeypatch, redis)
    lease = await acquire_worker_id()
    assert lease is not None
    assert lease.worker_id == 0
    assert snowflake._worker_id == 0
    # The id's key is now held under the lease's token.
    assert redis._data[_key(0)] == lease._token


async def test_acquire_skips_ids_held_by_others(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = _LeaseRedis(preset={_key(0): "other-a", _key(1): "other-b"})
    _install(monkeypatch, redis)
    lease = await acquire_worker_id()
    assert lease is not None
    assert lease.worker_id == 2
    assert snowflake._worker_id == 2


async def test_pool_exhausted_local_falls_back_to_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    preset = {_key(i): f"other-{i}" for i in range(3)}
    redis = _LeaseRedis(preset=preset)
    _install(monkeypatch, redis, environment="local", max_worker_id=2)
    lease = await acquire_worker_id()
    assert lease is None  # no lease to manage
    assert snowflake._worker_id == 0  # local fallback


async def test_pool_exhausted_non_local_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    preset = {_key(i): f"other-{i}" for i in range(3)}
    redis = _LeaseRedis(preset=preset)
    _install(monkeypatch, redis, environment="production", max_worker_id=2)
    with pytest.raises(WorkerIdUnavailableError):
        await acquire_worker_id()


async def test_redis_error_local_falls_back_to_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = _LeaseRedis(fail=True)
    _install(monkeypatch, redis, environment="local")
    lease = await acquire_worker_id()
    assert lease is None
    assert snowflake._worker_id == 0


async def test_redis_error_non_local_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = _LeaseRedis(fail=True)
    _install(monkeypatch, redis, environment="production")
    with pytest.raises(WorkerIdUnavailableError):
        await acquire_worker_id()


async def test_release_compare_and_delete_when_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = _LeaseRedis()
    _install(monkeypatch, redis)
    lease = await acquire_worker_id()
    assert lease is not None
    assert _key(0) in redis._data
    await lease.release()
    # We owned it → the id key is gone (returned to the pool).
    assert _key(0) not in redis._data


async def test_release_does_not_delete_id_owned_by_successor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = _LeaseRedis()
    # A stale lease whose id was re-leased by a successor (different token).
    redis._data[_key(5)] = "successor-token"
    lease = WorkerIdLease(
        worker_id=5, _redis=redis, _token="our-old-token", _ttl_seconds=30  # type: ignore[arg-type]
    )
    await lease.release()
    # Compare-and-delete must NOT drop the successor's key.
    assert redis._data[_key(5)] == "successor-token"


async def _run_heartbeat_iterations(
    lease: WorkerIdLease, monkeypatch: pytest.MonkeyPatch, iterations: int
) -> None:
    """Run the heartbeat loop a fixed number of renew cycles, then cancel it.

    Patches the module's ``asyncio.sleep`` so the loop advances deterministically
    without real time: it returns for ``iterations`` cycles, then raises
    ``CancelledError`` to break the ``while True`` (mirrors task cancellation).
    """
    calls = {"n": 0}

    async def fake_sleep(_seconds: float) -> None:
        calls["n"] += 1
        if calls["n"] > iterations:
            raise asyncio.CancelledError

    monkeypatch.setattr(worker_id_module.asyncio, "sleep", fake_sleep)
    with contextlib.suppress(asyncio.CancelledError):
        await lease._run_heartbeat()


async def test_heartbeat_renews_while_owned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rec = _RecordingLog()
    monkeypatch.setattr(worker_id_module, "_log", rec)
    redis = _LeaseRedis()
    token = "mine"
    redis._data[_key(3)] = token
    lease = WorkerIdLease(
        worker_id=3, _redis=redis, _token=token, _ttl_seconds=30  # type: ignore[arg-type]
    )
    await _run_heartbeat_iterations(lease, monkeypatch, iterations=2)
    # Still ours, renew succeeded each tick → no lease-lost error surfaced.
    assert ("error", "worker_id.lease_lost") not in rec.events
    assert redis._data[_key(3)] == token


async def test_heartbeat_surfaces_lost_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rec = _RecordingLog()
    monkeypatch.setattr(worker_id_module, "_log", rec)
    redis = _LeaseRedis()
    # Our id was taken over (TTL lapsed, re-leased) → renew compare fails.
    redis._data[_key(7)] = "successor-token"
    lease = WorkerIdLease(
        worker_id=7, _redis=redis, _token="our-token", _ttl_seconds=30  # type: ignore[arg-type]
    )
    await _run_heartbeat_iterations(lease, monkeypatch, iterations=1)
    assert ("error", "worker_id.lease_lost") in rec.events


async def test_start_heartbeat_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = _LeaseRedis()
    _install(monkeypatch, redis)
    lease = await acquire_worker_id()
    assert lease is not None
    lease.start_heartbeat()
    first = lease._heartbeat
    lease.start_heartbeat()  # second call must not replace the running task
    assert lease._heartbeat is first
    await lease.release()  # cancels the heartbeat + releases the id
    assert lease._heartbeat is None
