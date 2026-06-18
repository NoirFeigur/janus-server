"""Pure-async in-memory Redis double for tests.

Why this exists (and why we don't use ``fakeredis`` here):
``fakeredis.aioredis.FakeRedis`` resolves its awaits via a background-thread
server backend. On CPython 3.11 (no PEP 669 ``sys.monitoring``), coverage.py's
C tracer does **not** re-arm a coroutine frame's ``f_trace`` after a
cross-thread resume, so every source line that executes *after* an
``await redis.*`` call is silently dropped from coverage — making cache/redis
code paths (and any request that touches the dept-tree cache) look untested
even though the behaviour is exercised and asserted. This double awaits nothing
off the running loop, so the tracer stays armed and coverage reflects reality.

Surface is intentionally tiny but covers two production consumers:
- cache/redis primitives (``get`` / ``set`` / ``delete`` / ``ttl`` / ``ping`` /
  ``aclose``) — see ``src/core/cache.py`` + ``src/core/redis.py``.
- the session store (``exists`` / ``getdel`` / ``sadd`` / ``srem`` /
  ``smembers`` / ``expire`` on top of ``get`` / ``set`` / ``delete``) — the
  allowlist + opaque-refresh-rotation model. ``getdel`` is the atomic
  consume-old-refresh serialization point (no Lua needed: first caller reads +
  removes, a racing caller reads ``None`` and loses).
- the login throttle (``incr`` + ``expire`` + ``get`` / ``set`` / ``delete``) —
  per-username failure counting with lockout and per-IP sliding-window limiting.
"""

from __future__ import annotations

import builtins
import math
import time


class AsyncRedisDouble:
    """Minimal async, single-loop Redis stand-in (``decode_responses`` semantics).

    Stored values are ``str`` in / ``str`` out, matching the production client's
    ``decode_responses=True`` configuration. String values and set values live
    in separate maps (a key is one or the other, never both — matching Redis's
    WRONGTYPE model is out of scope; tests never alias a key across types).
    Per-key TTLs are tracked against a monotonic clock so expired keys read back
    as absent, for both string and set keys.
    """

    def __init__(self, *, decode_responses: bool = True) -> None:
        # decode_responses is accepted for drop-in parity with FakeRedis; values
        # are always stored/returned as str, which is the only mode we use.
        self._decode_responses = decode_responses
        self._data: dict[str, str] = {}
        self._sets: dict[str, set[str]] = {}
        self._expiry: dict[str, float] = {}  # key -> monotonic deadline (seconds)

    def _evict_if_expired(self, key: str) -> bool:
        deadline = self._expiry.get(key)
        if deadline is not None and time.monotonic() >= deadline:
            self._data.pop(key, None)
            self._sets.pop(key, None)
            self._expiry.pop(key, None)
            return True
        return False

    async def get(self, key: str) -> str | None:
        if self._evict_if_expired(key):
            return None
        return self._data.get(key)

    async def mget(self, *keys: str) -> list[str | None]:
        # Batched get: one value per key in order, ``None`` for missing/expired
        # (matching the production client used by perm_cache's generation read).
        return [await self.get(key) for key in keys]

    async def set(self, key: str, value: str, ex: int | None = None) -> bool:
        self._data[key] = value
        if ex is not None:
            self._expiry[key] = time.monotonic() + ex
        else:
            self._expiry.pop(key, None)
        return True

    async def delete(self, *keys: str) -> int:
        removed = 0
        for key in keys:
            present = self._data.pop(key, None) is not None
            present = self._sets.pop(key, None) is not None or present
            if present:
                removed += 1
            self._expiry.pop(key, None)
        return removed

    async def exists(self, *keys: str) -> int:
        # Redis counts each argument's presence (so a repeated key counts twice).
        count = 0
        for key in keys:
            if self._evict_if_expired(key):
                continue
            if key in self._data or key in self._sets:
                count += 1
        return count

    async def getdel(self, key: str) -> str | None:
        # Atomic get-and-delete: the serialization point for refresh rotation.
        if self._evict_if_expired(key):
            return None
        value = self._data.pop(key, None)
        if value is not None:
            self._expiry.pop(key, None)
        return value

    async def incr(self, key: str) -> int:
        # INCR: missing key starts at 0 → returns 1. An existing key's TTL is
        # preserved (only SET/EXPIRE touch expiry), matching Redis semantics.
        self._evict_if_expired(key)
        current = int(self._data.get(key, "0"))
        current += 1
        self._data[key] = str(current)
        return current

    async def sadd(self, key: str, *members: str) -> int:
        if self._evict_if_expired(key):
            pass
        bucket = self._sets.setdefault(key, set())
        added = 0
        for member in members:
            if member not in bucket:
                bucket.add(member)
                added += 1
        return added

    async def srem(self, key: str, *members: str) -> int:
        if self._evict_if_expired(key):
            return 0
        bucket = self._sets.get(key)
        if bucket is None:
            return 0
        removed = 0
        for member in members:
            if member in bucket:
                bucket.discard(member)
                removed += 1
        if not bucket:  # Redis drops a set key once its last member is removed.
            self._sets.pop(key, None)
            self._expiry.pop(key, None)
        return removed

    async def smembers(self, key: str) -> builtins.set[str]:
        if self._evict_if_expired(key):
            return set()
        return set(self._sets.get(key, set()))

    async def expire(self, key: str, seconds: int) -> bool:
        if self._evict_if_expired(key):
            return False
        if key not in self._data and key not in self._sets:
            return False
        self._expiry[key] = time.monotonic() + seconds
        return True

    async def ttl(self, key: str) -> int:
        # Redis semantics: -2 = no key, -1 = key without expiry, else seconds left.
        if self._evict_if_expired(key) or (
            key not in self._data and key not in self._sets
        ):
            return -2
        deadline = self._expiry.get(key)
        if deadline is None:
            return -1
        return max(1, math.ceil(deadline - time.monotonic()))

    async def ping(self) -> bool:
        return True

    async def aclose(self) -> None:
        return None
