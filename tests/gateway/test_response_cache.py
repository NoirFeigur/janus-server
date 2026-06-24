"""Unit tests for src/gateway/response_cache.py — exact-match response cache."""

from __future__ import annotations

import pytest

from src.gateway.cache import bump_catalog_generation
from src.gateway.response_cache import (
    compute_fingerprint,
    get_cached_response,
    is_cacheable_request,
    is_cacheable_response,
    set_cached_response,
)
from tests._async_redis_double import AsyncRedisDouble

# ---------------------------------------------------------------------------
# Fingerprint
# ---------------------------------------------------------------------------


def test_fingerprint_deterministic() -> None:
    msgs = [{"role": "user", "content": "hello"}]
    params = {"temperature": 0.7, "max_tokens": 100}
    fp1 = compute_fingerprint("gpt-4", msgs, params)
    fp2 = compute_fingerprint("gpt-4", msgs, params)
    assert fp1 == fp2
    assert len(fp1) == 32


def test_fingerprint_differs_on_model() -> None:
    msgs = [{"role": "user", "content": "hello"}]
    params = {"temperature": 0.7}
    fp1 = compute_fingerprint("gpt-4", msgs, params)
    fp2 = compute_fingerprint("gpt-3.5-turbo", msgs, params)
    assert fp1 != fp2


def test_fingerprint_differs_on_temperature() -> None:
    msgs = [{"role": "user", "content": "hello"}]
    fp1 = compute_fingerprint("gpt-4", msgs, {"temperature": 0.0})
    fp2 = compute_fingerprint("gpt-4", msgs, {"temperature": 1.0})
    assert fp1 != fp2


# ---------------------------------------------------------------------------
# Eligibility
# ---------------------------------------------------------------------------


def test_is_cacheable_request_streaming_false() -> None:
    assert is_cacheable_request(stream=True, response_cache_enabled=True, params={}) is False


def test_is_cacheable_request_disabled() -> None:
    assert is_cacheable_request(stream=False, response_cache_enabled=False, params={}) is False


def test_is_cacheable_request_n_greater_than_1() -> None:
    assert is_cacheable_request(stream=False, response_cache_enabled=True, params={"n": 2}) is False


def test_is_cacheable_request_tools() -> None:
    assert (
        is_cacheable_request(
            stream=False, response_cache_enabled=True, params={"tools": [{"type": "function"}]}
        )
        is False
    )


def test_is_cacheable_request_eligible() -> None:
    assert is_cacheable_request(stream=False, response_cache_enabled=True, params={}) is True


def test_is_cacheable_request_temperature_positive_false() -> None:
    # temperature > 0 → non-deterministic sampling, must not cache
    assert (
        is_cacheable_request(
            stream=False, response_cache_enabled=True, params={"temperature": 0.7}
        )
        is False
    )


def test_is_cacheable_request_temperature_zero_true() -> None:
    # temperature == 0 → deterministic, eligible for caching
    assert (
        is_cacheable_request(
            stream=False, response_cache_enabled=True, params={"temperature": 0.0}
        )
        is True
    )


def test_is_cacheable_request_temperature_unset_true() -> None:
    # unset temperature → treated as deterministic-eligible (matches prior behavior)
    assert is_cacheable_request(stream=False, response_cache_enabled=True, params={}) is True


def test_is_cacheable_response_with_content() -> None:
    class FakeMessage:
        tool_calls = None

    class FakeChoice:
        message = FakeMessage()

    class FakeResponse:
        choices = [FakeChoice()]

    assert is_cacheable_response(FakeResponse()) is True


def test_is_cacheable_response_with_tool_calls() -> None:
    class FakeMessage:
        tool_calls = [{"id": "call_1"}]

    class FakeChoice:
        message = FakeMessage()

    class FakeResponse:
        choices = [FakeChoice()]

    assert is_cacheable_response(FakeResponse()) is False


def test_is_cacheable_response_empty_choices() -> None:
    class FakeResponse:
        choices = []

    assert is_cacheable_response(FakeResponse()) is False


def test_is_cacheable_response_dict_format() -> None:
    resp = {"choices": [{"message": {"content": "hi", "tool_calls": None}}]}
    assert is_cacheable_response(resp) is True


# ---------------------------------------------------------------------------
# Cache operations
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cache_miss(fake_redis: AsyncRedisDouble) -> None:
    result = await get_cached_response(model_id=1, fingerprint="abc123")
    assert result is None


@pytest.mark.asyncio
async def test_cache_hit(fake_redis: AsyncRedisDouble) -> None:
    data = {"id": "chatcmpl-1", "choices": [{"message": {"content": "hi"}}]}
    await set_cached_response(model_id=1, fingerprint="abc123", response_data=data)
    result = await get_cached_response(model_id=1, fingerprint="abc123")
    assert result == data


@pytest.mark.asyncio
async def test_cache_invalidated_by_catalog_generation(fake_redis: AsyncRedisDouble) -> None:
    data = {"id": "chatcmpl-1"}
    await set_cached_response(model_id=1, fingerprint="abc123", response_data=data)

    await bump_catalog_generation()

    result = await get_cached_response(model_id=1, fingerprint="abc123")
    assert result is None


@pytest.mark.asyncio
async def test_cache_custom_ttl(fake_redis: AsyncRedisDouble) -> None:
    data = {"id": "chatcmpl-2"}
    await set_cached_response(model_id=2, fingerprint="def456", response_data=data, ttl_seconds=60)
    result = await get_cached_response(model_id=2, fingerprint="def456")
    assert result == data
