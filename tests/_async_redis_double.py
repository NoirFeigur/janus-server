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

Surface is intentionally tiny: the production code only calls ``get`` / ``set``
/ ``delete`` (see ``src/core/cache.py`` + ``src/core/redis.py``); ``ttl`` /
``ping`` / ``aclose`` exist for the cache-TTL and client-lifecycle tests.
"""

from __future__ import annotations

import math
import time


class AsyncRedisDouble:
    """Minimal async, single-loop Redis stand-in (``decode_responses`` semantics).

    Stored values are ``str`` in / ``str`` out, matching the production client's
    ``decode_responses=True`` configuration. Per-key TTLs are tracked against a
    monotonic clock so expired keys read back as absent.
    """

    def __init__(self, *, decode_responses: bool = True) -> None:
        # decode_responses is accepted for drop-in parity with FakeRedis; values
        # are always stored/returned as str, which is the only mode we use.
        self._decode_responses = decode_responses
        self._data: dict[str, str] = {}
        self._expiry: dict[str, float] = {}  # key -> monotonic deadline (seconds)

    def _evict_if_expired(self, key: str) -> bool:
        deadline = self._expiry.get(key)
        if deadline is not None and time.monotonic() >= deadline:
            self._data.pop(key, None)
            self._expiry.pop(key, None)
            return True
        return False

    async def get(self, key: str) -> str | None:
        if self._evict_if_expired(key):
            return None
        return self._data.get(key)

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
            if self._data.pop(key, None) is not None:
                removed += 1
            self._expiry.pop(key, None)
        return removed

    async def ttl(self, key: str) -> int:
        # Redis semantics: -2 = no key, -1 = key without expiry, else seconds left.
        if key not in self._data or self._evict_if_expired(key):
            return -2
        deadline = self._expiry.get(key)
        if deadline is None:
            return -1
        return max(1, math.ceil(deadline - time.monotonic()))

    async def ping(self) -> bool:
        return True

    async def aclose(self) -> None:
        return None
