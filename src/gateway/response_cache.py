"""Exact-match response cache for non-streaming LLM completions.

Caches successful non-streaming responses keyed by a deterministic fingerprint
of (model + messages + key params).  On hit, the cached response is returned
directly (skipping the upstream LLM call) but usage and quota are still
recorded with ``cache_hit=True``.

Rules:
- Only non-streaming successful completions are cached.
- Skip: tool_use responses, n>1, empty responses, errors.
- TTL: per-model configurable (default 300s / 5 minutes).
- Key embeds catalog generation so model config changes invalidate.
"""

from __future__ import annotations

import hashlib
import json
from contextlib import suppress
from typing import Any

from redis.exceptions import RedisError

from src.core.logging import get_logger
from src.core.redis import get_redis
from src.gateway.cache import CATALOG_GEN_KEY, get_generation

_log = get_logger(__name__)

_DEFAULT_RESPONSE_CACHE_TTL = 300  # 5 minutes


# ---------------------------------------------------------------------------
# Fingerprint computation
# ---------------------------------------------------------------------------


def compute_fingerprint(
    model: str,
    messages: list[Any],
    params: dict[str, Any],
) -> str:
    """Compute a deterministic hash of the request for cache keying.

    Includes model name, messages content, and key parameters that affect output
    (temperature, max_tokens, top_p, seed).  Excludes stream, user metadata, etc.
    """
    canonical: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": params.get("temperature"),
        "max_tokens": params.get("max_tokens"),
        "top_p": params.get("top_p"),
        "seed": params.get("seed"),
    }
    raw = json.dumps(canonical, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


# ---------------------------------------------------------------------------
# Eligibility check
# ---------------------------------------------------------------------------


def is_cacheable_request(
    *,
    stream: bool,
    response_cache_enabled: bool,
    params: dict[str, Any],
) -> bool:
    """Check if this request is eligible for response caching."""
    if stream:
        return False
    if not response_cache_enabled:
        return False
    # n > 1 produces multiple choices — don't cache
    if params.get("n", 1) > 1:
        return False
    # Tool calls are context-dependent — don't cache
    return not (params.get("tools") or params.get("tool_choice"))


def is_cacheable_response(response: Any) -> bool:
    """Check if this response is eligible for caching (post-call)."""
    # Must have choices with content
    choices = getattr(response, "choices", None)
    if not choices:
        if isinstance(response, dict):
            choices = response.get("choices", [])
        if not choices:
            return False

    # Check first choice has content and no tool_calls
    first = choices[0] if choices else None
    if first is None:
        return False

    # Check for tool_calls
    message = getattr(first, "message", None) or (
        first.get("message") if isinstance(first, dict) else None
    )
    if message is not None:
        tool_calls = getattr(message, "tool_calls", None) or (
            message.get("tool_calls") if isinstance(message, dict) else None
        )
        if tool_calls:
            return False

    return True


# ---------------------------------------------------------------------------
# Cache operations
# ---------------------------------------------------------------------------


def _response_cache_key(catalog_gen: int, model_id: int, fingerprint: str) -> str:
    return f"janus:gw:v1:resp:{catalog_gen}:m:{model_id}:h:{fingerprint}"


async def get_cached_response(
    model_id: int, fingerprint: str
) -> dict[str, Any] | None:
    """Look up a cached response by fingerprint. Returns None on miss."""
    try:
        redis = get_redis()
        catalog_gen = await get_generation(CATALOG_GEN_KEY)
        key = _response_cache_key(catalog_gen, model_id, fingerprint)
        raw = await redis.get(key)
        if raw is None:
            return None
        return json.loads(raw)
    except (RedisError, json.JSONDecodeError, ValueError):
        return None


async def set_cached_response(
    model_id: int,
    fingerprint: str,
    response_data: dict[str, Any],
    *,
    ttl_seconds: int | None = None,
) -> None:
    """Store a response in cache (fail-open)."""
    with suppress(Exception):
        redis = get_redis()
        catalog_gen = await get_generation(CATALOG_GEN_KEY)
        key = _response_cache_key(catalog_gen, model_id, fingerprint)
        ttl = ttl_seconds or _DEFAULT_RESPONSE_CACHE_TTL
        await redis.set(key, json.dumps(response_data, default=str), ex=ttl)
