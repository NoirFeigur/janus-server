"""Snowflake worker-id lease over Redis (data-model §0.2).

Every replica needs a unique 10-bit worker-id (0..1023) or snowflake primary
keys collide across replicas. This module atomically leases a free id from Redis
at startup, keeps it alive with a background heartbeat, and releases it on
shutdown so the id returns to the pool.

Design:
- **Atomic claim**: ``SET worker_id:{n} {token} NX EX {ttl}`` walks 0..1023 and
  takes the first id whose key does not exist. The random ``token`` proves
  ownership so a stale heartbeat from a crashed-then-restarted process can never
  renew an id another replica now holds.
- **Heartbeat**: a background task renews the TTL every ``ttl/3`` seconds via a
  compare-and-renew Lua script (renew only if the value still equals our token).
- **Crash safety**: if a replica dies, its heartbeat stops, the key's TTL lapses,
  and the id frees for reuse — no manual cleanup, no permanent leak.
- **Release**: on graceful shutdown a compare-and-delete Lua script drops the key
  iff we still own it (never deletes an id a successor already re-leased).

``environment == "local"`` falls back to worker-id 0 when Redis is unreachable
(single-process dev); any other environment fails fast (§0.2: a multi-replica
deployment that cannot guarantee a unique id MUST NOT start).
"""

from __future__ import annotations

import asyncio
import contextlib
import secrets
import signal
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from src.config import get_settings
from src.core.logging import get_logger
from src.core.redis import AsyncRedis, get_redis
from src.core.snowflake import _MAX_WORKER_ID, set_worker_id

_log = get_logger(__name__)

_KEY_PREFIX = "snowflake:worker_id:"


def _monotonic() -> float:
    """Monotonic clock wrapper (patchable in tests for deterministic timing).

    Wraps :func:`time.monotonic` behind a module function so tests can drive the
    heartbeat's "have we been unable to renew for a full TTL?" decision without
    real elapsed time.
    """
    return time.monotonic()


def _signal_self_terminate() -> None:
    """Default lease-lost action: ask this process to shut down gracefully.

    Raising SIGTERM in-process triggers the ASGI server's graceful-shutdown path
    (lifespan teardown runs, in-flight requests drain) and the orchestrator then
    restarts the replica, which re-leases a fresh worker-id. We do NOT hard-exit:
    a half-released lease or a torn-down event loop is worse than a clean signal.
    """
    signal.raise_signal(signal.SIGTERM)

# Renew the lease iff we still own it (value matches our token). Returns 1 on
# renew, 0 if the key vanished or was taken over (we lost the lease).
_RENEW_LUA = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('expire', KEYS[1], ARGV[2])
else
    return 0
end
"""

# Delete the lease iff we still own it (compare-and-delete; never drop a key a
# successor already re-leased after our TTL lapsed).
_RELEASE_LUA = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
else
    return 0
end
"""


class WorkerIdUnavailableError(RuntimeError):
    """Raised when no free worker-id could be leased (pool exhausted / Redis down)."""


@dataclass
class WorkerIdLease:
    """A held worker-id lease with a running heartbeat. Release via ``release()``."""

    worker_id: int
    _redis: AsyncRedis
    _token: str
    _ttl_seconds: int
    _heartbeat: asyncio.Task[None] | None = None
    # Invoked once if the heartbeat detects the lease was lost (either the id was
    # re-leased by another replica, or we could not renew for a full TTL so the
    # key has provably expired). Defaults to signalling this process to shut down
    # gracefully; injectable so tests can assert the fail-fast fires without
    # actually terminating the test runner.
    _on_lease_lost: Callable[[], None] = field(default=_signal_self_terminate)
    # Monotonic timestamp of the last *successful* renew (or lease acquisition).
    # Drives the partition self-fence: if we go a full TTL without renewing, the
    # Redis key has expired and another replica may already hold our id, so we
    # must stop minting snowflake ids under it. Set on first heartbeat tick.
    _last_renew_monotonic: float | None = None

    @property
    def _key(self) -> str:
        return f"{_KEY_PREFIX}{self.worker_id}"

    def start_heartbeat(self) -> None:
        """Spawn the background TTL-renewal task (idempotent)."""
        if self._heartbeat is None:
            self._heartbeat = asyncio.create_task(self._run_heartbeat())

    async def _run_heartbeat(self) -> None:
        interval = max(1, self._ttl_seconds // 3)
        # Seed the renew clock at start: the lease was just acquired (or renewed),
        # so the partition window is measured from now.
        self._last_renew_monotonic = _monotonic()
        while True:
            await asyncio.sleep(interval)
            try:
                renewed = await self._redis.eval(
                    _RENEW_LUA, 1, self._key, self._token, str(self._ttl_seconds)
                )
            except Exception:  # noqa: BLE001 — transient Redis blip; retry, but
                # a *sustained* partition is not transient: once we have been
                # unable to renew for a full TTL, the Redis key has expired and
                # another replica may already hold our worker-id. Continuing to
                # mint snowflake ids under it would collide primary keys across
                # replicas, so self-fence exactly as if the renew compare failed.
                last = (
                    self._last_renew_monotonic
                    if self._last_renew_monotonic is not None
                    else _monotonic()
                )
                elapsed = _monotonic() - last
                if elapsed >= self._ttl_seconds:
                    _log.error(
                        "worker_id.lease_lost_partition",
                        worker_id=self.worker_id,
                        elapsed_seconds=round(elapsed, 1),
                    )
                    self._on_lease_lost()
                    return
                _log.warning("worker_id.heartbeat_error", worker_id=self.worker_id)
                continue
            if not renewed:
                # We lost the lease (TTL lapsed during a Redis partition and the
                # id was re-leased by another replica). Continuing would mint
                # snowflake ids under a worker-id we no longer own → primary-key
                # collisions across replicas. Fail fast: trigger graceful
                # shutdown and STOP the heartbeat (do not keep renewing a lease we
                # lost, and do not keep the process alive minting colliding ids).
                _log.error("worker_id.lease_lost", worker_id=self.worker_id)
                self._on_lease_lost()
                return
            # Renew landed → reset the partition window.
            self._last_renew_monotonic = _monotonic()

    async def release(self) -> None:
        """Stop the heartbeat and release the id (compare-and-delete)."""
        if self._heartbeat is not None:
            self._heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._heartbeat
            self._heartbeat = None
        try:
            await self._redis.eval(_RELEASE_LUA, 1, self._key, self._token)
        except Exception:  # noqa: BLE001 — best-effort; TTL reclaims it anyway.
            _log.warning("worker_id.release_error", worker_id=self.worker_id)


async def _try_claim(redis: AsyncRedis, worker_id: int, token: str, ttl: int) -> bool:
    """Atomically claim ``worker_id`` iff its key is unset (SET NX EX)."""
    acquired = await redis.set(
        f"{_KEY_PREFIX}{worker_id}", token, nx=True, ex=ttl
    )
    return bool(acquired)


async def acquire_worker_id() -> WorkerIdLease | None:
    """Lease a free worker-id from Redis and pin it on the snowflake generator.

    Walks 0..1023 and takes the first free id. On success pins it via
    ``set_worker_id`` and returns a lease whose heartbeat the caller must start.
    On exhaustion/Redis failure: fails fast (raises) outside ``local``; in
    ``local`` falls back to worker-id 0 and returns ``None`` (no lease to manage).
    """
    settings = get_settings()
    ttl = settings.snowflake_worker_id_ttl_seconds
    token = secrets.token_hex(16)
    redis = get_redis()

    try:
        for candidate in range(_MAX_WORKER_ID + 1):
            if await _try_claim(redis, candidate, token, ttl):
                set_worker_id(candidate)
                lease = WorkerIdLease(
                    worker_id=candidate,
                    _redis=redis,
                    _token=token,
                    _ttl_seconds=ttl,
                )
                _log.info("worker_id.leased", worker_id=candidate, ttl_seconds=ttl)
                return lease
    except Exception as exc:  # noqa: BLE001 — Redis unreachable at startup.
        if settings.environment == "local":
            _log.warning("worker_id.redis_unavailable_local_fallback", error=str(exc))
            set_worker_id(0)
            return None
        raise WorkerIdUnavailableError(
            "could not reach Redis to lease a snowflake worker-id"
        ) from exc

    # Walked the whole space without claiming one → every id is held.
    if settings.environment == "local":
        _log.warning("worker_id.pool_exhausted_local_fallback")
        set_worker_id(0)
        return None
    raise WorkerIdUnavailableError(
        f"no free snowflake worker-id in 0..{_MAX_WORKER_ID} (pool exhausted)"
    )
