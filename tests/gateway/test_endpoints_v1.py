"""Integration tests for src/gateway/endpoints_v1.py — /v1/embeddings, /v1/models, /v1/responses."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, Mock, patch

import pytest

from src.auth.service import AuthenticatedUser
from src.gateway.endpoints_v1 import (
    EmbeddingsRequest,
    ResponsesRequest,
    _latency_ms,
    _request_id,
    _upstream_error,
)
from src.enums import ErrorCode
from tests._async_redis_double import AsyncRedisDouble


def _fake_user() -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id=100,
        username="test_user",
        department_id=10,
        api_key_id=None,
        permissions=frozenset({"ai:gateway:use"}),
    )


def _fake_logical_model() -> Mock:
    model = Mock()
    model.id = 1
    model.name = "gpt-4"
    model.display_name = "GPT-4"
    model.price_input = None
    model.price_output = None
    return model


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------


class TestEmbeddingsRequestSchema:
    def test_valid_string_input(self) -> None:
        req = EmbeddingsRequest(model="text-embedding-3-small", input="hello world")
        assert req.model == "text-embedding-3-small"
        assert req.input == "hello world"

    def test_valid_list_input(self) -> None:
        req = EmbeddingsRequest(model="text-embedding-3-small", input=["a", "b"])
        assert req.input == ["a", "b"]

    def test_optional_fields(self) -> None:
        req = EmbeddingsRequest(model="m", input="x")
        assert req.encoding_format is None
        assert req.dimensions is None


class TestResponsesRequestSchema:
    def test_defaults(self) -> None:
        req = ResponsesRequest(model="gpt-4", input="hi")
        assert req.stream is False
        assert req.max_output_tokens is None
        assert req.temperature is None
        assert req.instructions is None


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


def test_latency_ms() -> None:
    from time import monotonic

    start = monotonic() - 0.1
    ms = _latency_ms(start)
    assert ms >= 90  # ~100ms


def test_request_id_from_state() -> None:
    request = Mock()
    request.state.trace_id = "trace-abc"
    assert _request_id(request) == "trace-abc"


def test_request_id_fallback() -> None:
    request = Mock(spec=[])
    request.state = Mock(spec=[])
    rid = _request_id(request)
    assert len(rid) > 0  # UUID fallback


def test_upstream_error_rate_limit() -> None:
    import litellm

    exc = litellm.RateLimitError("rate limited", model="gpt-4", llm_provider="openai")
    err = _upstream_error(exc)
    assert err.code == ErrorCode.upstream_rate_limited


def test_upstream_error_timeout() -> None:
    import litellm

    exc = litellm.Timeout("timeout", model="gpt-4", llm_provider="openai")
    err = _upstream_error(exc)
    assert err.code == ErrorCode.upstream_timeout


def test_upstream_error_generic() -> None:
    err = _upstream_error(RuntimeError("something"))
    assert err.code == ErrorCode.upstream_error
