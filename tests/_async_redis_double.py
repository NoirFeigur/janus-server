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
        self._lists: dict[str, list[str]] = {}
        self._hashes: dict[str, dict[str, str]] = {}
        self._zsets: dict[str, dict[str, float]] = {}
        self._expiry: dict[str, float] = {}  # key -> monotonic deadline (seconds)

    def _evict_if_expired(self, key: str) -> bool:
        deadline = self._expiry.get(key)
        if deadline is not None and time.monotonic() >= deadline:
            self._data.pop(key, None)
            self._sets.pop(key, None)
            self._lists.pop(key, None)
            self._hashes.pop(key, None)
            self._zsets.pop(key, None)
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

    async def set(self, key: str, value: str, ex: int | None = None, *, nx: bool = False) -> bool | None:
        if nx:
            if not self._evict_if_expired(key) and key in self._data:
                return None
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
            present = self._lists.pop(key, None) is not None or present
            present = self._hashes.pop(key, None) is not None or present
            present = self._zsets.pop(key, None) is not None or present
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
            if key in self._data or key in self._sets or key in self._lists or key in self._hashes or key in self._zsets:
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
        if key not in self._data and key not in self._sets and key not in self._lists and key not in self._hashes and key not in self._zsets:
            return False
        self._expiry[key] = time.monotonic() + seconds
        return True

    async def ttl(self, key: str) -> int:
        # Redis semantics: -2 = no key, -1 = key without expiry, else seconds left.
        if self._evict_if_expired(key) or (
            key not in self._data and key not in self._sets and key not in self._lists and key not in self._hashes and key not in self._zsets
        ):
            return -2
        deadline = self._expiry.get(key)
        if deadline is None:
            return -1
        return max(1, math.ceil(deadline - time.monotonic()))

    async def ping(self) -> bool:
        return True

    def pubsub(self) -> _PubSubDouble:
        return _PubSubDouble()

    async def publish(self, channel: str, message: str) -> int:
        return 0

    # ------------------------------------------------------------------
    # List operations (RPUSH / LPOP / LLEN / LRANGE)
    # ------------------------------------------------------------------

    async def rpush(self, key: str, *values: str) -> int:
        if self._evict_if_expired(key):
            pass
        lst = self._lists.setdefault(key, [])
        for v in values:
            lst.append(v)
        return len(lst)

    async def lpop(self, key: str, count: int | None = None) -> str | list[str] | None:
        if self._evict_if_expired(key):
            return None
        lst = self._lists.get(key)
        if not lst:
            return None
        if count is None:
            return lst.pop(0)
        result = lst[:count]
        del lst[:count]
        if not lst:
            self._lists.pop(key, None)
        return result

    async def llen(self, key: str) -> int:
        if self._evict_if_expired(key):
            return 0
        return len(self._lists.get(key, []))

    async def lrange(self, key: str, start: int, stop: int) -> list[str]:
        if self._evict_if_expired(key):
            return []
        lst = self._lists.get(key, [])
        # Redis LRANGE stop is inclusive
        return lst[start : stop + 1] if stop != -1 else lst[start:]

    # ------------------------------------------------------------------
    # Hash operations (HSET / HGET / HGETALL / HINCRBY / HMGET)
    # ------------------------------------------------------------------

    async def hset(self, key: str, field: str | None = None, value: str | None = None, mapping: dict[str, str] | None = None) -> int:
        if self._evict_if_expired(key):
            pass
        h = self._hashes.setdefault(key, {})
        count = 0
        if field is not None and value is not None:
            is_new = field not in h
            h[field] = str(value)
            count += int(is_new)
        if mapping:
            for k, v in mapping.items():
                is_new = k not in h
                h[k] = str(v)
                count += int(is_new)
        return count

    async def hget(self, key: str, field: str) -> str | None:
        if self._evict_if_expired(key):
            return None
        h = self._hashes.get(key)
        if h is None:
            return None
        return h.get(field)

    async def hmget(self, key: str, *fields: str) -> list[str | None]:
        """HMGET: return values for fields in order."""
        if self._evict_if_expired(key):
            return [None] * len(fields)
        h = self._hashes.get(key, {})
        return [h.get(f) for f in fields]

    async def hgetall(self, key: str) -> dict[str, str]:
        if self._evict_if_expired(key):
            return {}
        return dict(self._hashes.get(key, {}))

    async def hincrby(self, key: str, field: str, amount: int = 1) -> int:
        if self._evict_if_expired(key):
            pass
        h = self._hashes.setdefault(key, {})
        current = int(h.get(field, "0"))
        current += amount
        h[field] = str(current)
        return current

    # ------------------------------------------------------------------
    # Sorted set operations (ZADD / ZREM / ZCARD / ZREMRANGEBYSCORE)
    # ------------------------------------------------------------------

    async def zadd(self, key: str, score_member: dict[str, float] | None = None, **kwargs: float) -> int:
        """Simplified ZADD: zadd(key, {member: score})."""
        if self._evict_if_expired(key):
            pass
        zs = self._zsets.setdefault(key, {})
        mapping = score_member or kwargs
        added = 0
        for member, score in mapping.items():
            if member not in zs:
                added += 1
            zs[member] = float(score)
        return added

    async def zrem(self, key: str, *members: str) -> int:
        if self._evict_if_expired(key):
            return 0
        zs = self._zsets.get(key)
        if zs is None:
            return 0
        removed = 0
        for m in members:
            if m in zs:
                del zs[m]
                removed += 1
        if not zs:
            self._zsets.pop(key, None)
        return removed

    async def zcard(self, key: str) -> int:
        if self._evict_if_expired(key):
            return 0
        return len(self._zsets.get(key, {}))

    async def zremrangebyscore(self, key: str, min_score: float | str, max_score: float | str) -> int:
        if self._evict_if_expired(key):
            return 0
        zs = self._zsets.get(key)
        if zs is None:
            return 0
        min_s = float("-inf") if min_score == "-inf" else float(min_score)
        max_s = float("inf") if max_score == "+inf" else float(max_score)
        to_remove = [m for m, s in zs.items() if min_s <= s <= max_s]
        for m in to_remove:
            del zs[m]
        if not zs:
            self._zsets.pop(key, None)
        return len(to_remove)

    # ------------------------------------------------------------------
    # SET: sismember
    # ------------------------------------------------------------------

    async def sismember(self, key: str, member: str) -> bool:
        if self._evict_if_expired(key):
            return False
        bucket = self._sets.get(key)
        return member in bucket if bucket else False

    # ------------------------------------------------------------------
    # SET with NX (conditional set)
    # ------------------------------------------------------------------

    async def set(self, key: str, value: str, ex: int | None = None, *, nx: bool = False) -> bool | None:  # type: ignore[override]
        if nx:
            if not self._evict_if_expired(key) and key in self._data:
                return None
        self._data[key] = value
        if ex is not None:
            self._expiry[key] = time.monotonic() + ex
        else:
            self._expiry.pop(key, None)
        return True

    # ------------------------------------------------------------------
    # PEXPIRE
    # ------------------------------------------------------------------

    async def pexpire(self, key: str, milliseconds: int) -> bool:
        if self._evict_if_expired(key):
            return False
        present = key in self._data or key in self._sets or key in self._hashes or key in self._zsets or key in self._lists
        if not present:
            return False
        self._expiry[key] = time.monotonic() + (milliseconds / 1000.0)
        return True

    # ------------------------------------------------------------------
    # EVAL (Lua script simulation)
    # ------------------------------------------------------------------

    async def eval(self, script: str, numkeys: int, *args: object) -> list[int]:
        """Minimal Lua eval stub — returns [1, 0, limit] (allowed) by default.

        For test purposes, simulates rate-limit Lua scripts. The test can
        override behavior by manipulating the underlying data directly.
        """
        # Parse key and args
        keys = [str(args[i]) for i in range(numkeys)]
        argv = [str(args[i]) for i in range(numkeys, len(args))]

        # Concurrent script: uses "timeout_ms" variable (unique marker)
        if "timeout_ms" in script:
            key = keys[0]
            now_ms = float(argv[0])
            limit = int(argv[1])
            member = argv[2] if len(argv) > 2 else str(now_ms)
            timeout_ms = float(argv[3]) if len(argv) > 3 else 1800000

            zs = self._zsets.get(key, {})
            cutoff = now_ms - timeout_ms
            zs = {m: s for m, s in zs.items() if s > cutoff}
            count = len(zs)

            if count >= limit:
                self._zsets[key] = zs
                return [0, count, limit]

            zs[member] = now_ms
            self._zsets[key] = zs
            return [1, count + 1, limit]

        # RPM script: uses "window_ms" variable (unique marker)
        if "window_ms" in script:
            key = keys[0]
            now_ms = float(argv[0])
            window_ms = float(argv[1])
            limit = int(argv[2])
            member = argv[3] if len(argv) > 3 else str(now_ms)

            zs = self._zsets.get(key, {})
            cutoff = now_ms - window_ms
            zs = {m: s for m, s in zs.items() if s > cutoff}
            count = len(zs)

            if count >= limit:
                self._zsets[key] = zs
                return [0, count, limit]

            zs[member] = now_ms
            self._zsets[key] = zs
            return [1, count + 1, limit]

        # TPM script detection: HMGET + HSET pattern
        if "HMGET" in script and "HSET" in script:
            key = keys[0]
            now_ms = float(argv[0])
            limit = int(argv[1])
            request_tokens = int(argv[2])
            refill_rate = float(argv[3]) if len(argv) > 3 else float(limit)

            h = self._hashes.get(key, {})
            tokens = float(h.get("tokens", str(limit)))
            last_ts = float(h.get("ts", str(now_ms)))

            elapsed_ms = now_ms - last_ts
            refill = int(elapsed_ms * refill_rate / 60000)
            tokens = min(limit, tokens + refill)

            if tokens < request_tokens:
                return [0, int(tokens), limit]

            tokens = tokens - request_tokens
            self._hashes[key] = {"tokens": str(int(tokens)), "ts": str(int(now_ms))}
            return [1, int(tokens), limit]

        # Default: allow
        return [1, 0, 100]

    # ------------------------------------------------------------------
    # Pipeline support
    # ------------------------------------------------------------------

    def pipeline(self, transaction: bool = True) -> _PipelineDouble:
        return _PipelineDouble(self)

    async def aclose(self) -> None:
        return None


class _PipelineDouble:
    """Minimal pipeline double that buffers commands and executes sequentially."""

    def __init__(self, redis: AsyncRedisDouble) -> None:
        self._redis = redis
        self._commands: list[tuple[str, tuple[object, ...], dict[str, object]]] = []

    def hincrby(self, key: str, field: str, amount: int = 1) -> _PipelineDouble:
        self._commands.append(("hincrby", (key, field, amount), {}))
        return self

    def hget(self, key: str, field: str) -> _PipelineDouble:
        self._commands.append(("hget", (key, field), {}))
        return self

    def expire(self, key: str, seconds: int) -> _PipelineDouble:
        self._commands.append(("expire", (key, seconds), {}))
        return self

    def pexpire(self, key: str, milliseconds: int) -> _PipelineDouble:
        self._commands.append(("pexpire", (key, milliseconds), {}))
        return self

    async def execute(self) -> list[object]:
        results: list[object] = []
        for cmd, args, kwargs in self._commands:
            method = getattr(self._redis, cmd)
            result = await method(*args, **kwargs)
            results.append(result)
        self._commands.clear()
        return results


class _PubSubDouble:
    """Minimal pub/sub double that immediately yields nothing."""

    async def subscribe(self, *channels: str) -> None:
        return None

    async def unsubscribe(self, *channels: str) -> None:
        return None

    async def get_message(
        self, *, ignore_subscribe_messages: bool = False, timeout: float = 0.0
    ) -> dict[str, str] | None:
        # Never delivers a message in tests — the poll loop will just spin
        # until cancelled by shutdown.
        import asyncio

        await asyncio.sleep(timeout or 0.1)
        return None

    async def aclose(self) -> None:
        return None
