"""Tests for native Anthropic/Gemini streaming — SSE bytes parsing and usage extraction."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import suppress
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from src.auth.service import AuthenticatedUser
from src.db.models.model_catalog import LogicalModel
from src.gateway.router import (
    _accumulate_anthropic_usage,
    _accumulate_gemini_usage,
    _channel_id_from_metadata,
    _check_rate_limits,
    _empty_usage,
    _extract_anthropic_usage,
    _extract_gemini_usage_from_response,
    _extract_routing_info,
    _provider_from_response,
    _stream_anthropic_native,
    _stream_gemini_native,
    _stream_openai,
)
from src.gateway.schemas import OpenAIRequest

# --- helpers ---


def _make_anthropic_sse(*events: tuple[str, dict[str, Any]]) -> list[bytes]:
    """Build raw Anthropic SSE byte chunks from (event_type, data) pairs."""
    chunks: list[bytes] = []
    for event_type, data in events:
        sse = f"event: {event_type}\ndata: {json.dumps(data)}\n\n"
        chunks.append(sse.encode("utf-8"))
    return chunks


def _make_gemini_sse(*payloads: dict[str, Any]) -> list[bytes]:
    """Build raw Gemini SSE byte chunks."""
    chunks: list[bytes] = []
    for payload in payloads:
        sse = f"data: {json.dumps(payload)}\n\n"
        chunks.append(sse.encode("utf-8"))
    return chunks


def _fake_user() -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id=100,
        username="test_user",
        department_id=10,
        permissions=frozenset(),
    )


def _fake_logical_model() -> Any:
    from unittest.mock import Mock

    model = Mock(spec=LogicalModel)
    model.id = 1
    model.name = "claude-sonnet"
    model.display_name = "Claude Sonnet"
    model.price_input = None
    model.price_output = None
    return model


async def _fake_byte_stream(chunks: list[bytes]) -> AsyncIterator[bytes]:
    for chunk in chunks:
        yield chunk


# Backward-compatible aliases used by all stream tests below.
FakeUser = _fake_user
FakeLogicalModel = _fake_logical_model


# ---------------------------------------------------------------------------
# Unit tests: _accumulate_anthropic_usage
# ---------------------------------------------------------------------------


class TestAccumulateAnthropicUsage:
    def test_message_start_input_tokens(self) -> None:
        usage = _empty_usage()
        raw = _make_anthropic_sse(
            ("message_start", {
                "type": "message_start",
                "message": {
                    "usage": {"input_tokens": 42, "output_tokens": 0},
                },
            }),
        )[0]
        _accumulate_anthropic_usage(raw, usage)
        assert usage["prompt_tokens"] == 42
        assert usage["total_tokens"] == 42

    def test_message_delta_output_tokens(self) -> None:
        usage = {"prompt_tokens": 10, "completion_tokens": 0, "total_tokens": 10}
        raw = _make_anthropic_sse(
            ("message_delta", {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn"},
                "usage": {"output_tokens": 25},
            }),
        )[0]
        _accumulate_anthropic_usage(raw, usage)
        assert usage["completion_tokens"] == 25
        assert usage["total_tokens"] == 35

    def test_content_block_delta_no_usage(self) -> None:
        usage = _empty_usage()
        raw = _make_anthropic_sse(
            ("content_block_delta", {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "hello"},
            }),
        )[0]
        _accumulate_anthropic_usage(raw, usage)
        assert usage == _empty_usage()

    def test_invalid_bytes_no_crash(self) -> None:
        usage = _empty_usage()
        _accumulate_anthropic_usage(b"\xff\xfe invalid", usage)
        assert usage == _empty_usage()

    def test_non_sse_line_skipped(self) -> None:
        usage = _empty_usage()
        _accumulate_anthropic_usage(b"event: ping\n\n", usage)
        assert usage == _empty_usage()


# ---------------------------------------------------------------------------
# Unit tests: _accumulate_gemini_usage
# ---------------------------------------------------------------------------


class TestAccumulateGeminiUsage:
    def test_usage_metadata_extracted(self) -> None:
        usage = _empty_usage()
        raw = _make_gemini_sse({
            "candidates": [{"content": {"parts": [{"text": "hi"}]}}],
            "usageMetadata": {
                "promptTokenCount": 15,
                "candidatesTokenCount": 8,
                "totalTokenCount": 23,
            },
        })[0]
        _accumulate_gemini_usage(raw, usage)
        assert usage["prompt_tokens"] == 15
        assert usage["completion_tokens"] == 8
        assert usage["total_tokens"] == 23

    def test_chunk_without_usage(self) -> None:
        usage = _empty_usage()
        raw = _make_gemini_sse({
            "candidates": [{"content": {"parts": [{"text": "hi"}]}}],
        })[0]
        _accumulate_gemini_usage(raw, usage)
        assert usage == _empty_usage()

    def test_invalid_bytes_no_crash(self) -> None:
        usage = _empty_usage()
        _accumulate_gemini_usage(b"\xff\xfe invalid", usage)
        assert usage == _empty_usage()


# ---------------------------------------------------------------------------
# Unit tests: _channel_id_from_metadata
# ---------------------------------------------------------------------------


class TestChannelIdFromMetadata:
    def test_extracts_from_model_info(self) -> None:
        meta: dict[str, Any] = {"model_info": {"id": "42"}}
        assert _channel_id_from_metadata(meta) == 42

    def test_returns_none_for_empty(self) -> None:
        assert _channel_id_from_metadata({}) is None

    def test_returns_none_for_non_numeric(self) -> None:
        meta: dict[str, Any] = {"model_info": {"id": "not-a-number"}}
        assert _channel_id_from_metadata(meta) is None

    def test_int_id(self) -> None:
        meta: dict[str, Any] = {"model_info": {"id": 99}}
        assert _channel_id_from_metadata(meta) == 99


@pytest.mark.parametrize(
    "reserved_key",
    [
        "base_url",
        "extra_headers",
        "aws_access_key_id",
        "vertex_project",
        "azure_ad_token",
        "specific_deployment",
    ],
)
def test_openai_request_rejects_gateway_reserved_params(reserved_key: str) -> None:
    with pytest.raises(ValueError, match="gateway-reserved"):
        OpenAIRequest.model_validate(
            {
                "model": "gpt-4o",
                "messages": [{"role": "user", "content": "hi"}],
                reserved_key: "bad",
            }
        )


def test_openai_request_rejects_reserved_headers() -> None:
    for reserved_key in ("headers", "default_headers"):
        with pytest.raises(ValueError, match="gateway-reserved"):
            OpenAIRequest.model_validate(
                {
                    "model": "gpt-4o",
                    "messages": [{"role": "user", "content": "hi"}],
                    reserved_key: "bad",
                }
            )


def test_extract_routing_info_reads_provider_and_upstream_from_model_info() -> None:
    class Resp:
        model = "gpt-4o"
        _hidden_params = {
            "model_info": {
                "id": "42",
                "provider": "openai",
                "upstream_model": "gpt-4o-2024-08-06",
            }
        }

    channel_id, upstream_model = _extract_routing_info(Resp())
    assert channel_id == 42
    assert upstream_model == "openai/gpt-4o-2024-08-06"
    assert _provider_from_response(Resp()) == "openai"


# ---------------------------------------------------------------------------
# Unit tests: _extract_anthropic_usage (non-streaming response)
# ---------------------------------------------------------------------------


class TestExtractAnthropicUsage:
    def test_from_dict(self) -> None:
        resp = {"usage": {"input_tokens": 10, "output_tokens": 20}}
        usage = _extract_anthropic_usage(resp)
        assert usage == {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30}

    def test_empty_response(self) -> None:
        usage = _extract_anthropic_usage({})
        assert usage == _empty_usage()


# ---------------------------------------------------------------------------
# Unit tests: _extract_gemini_usage_from_response
# ---------------------------------------------------------------------------


class TestExtractGeminiUsage:
    def test_from_dict(self) -> None:
        resp = {
            "usageMetadata": {
                "promptTokenCount": 5,
                "candidatesTokenCount": 10,
                "totalTokenCount": 15,
            }
        }
        usage = _extract_gemini_usage_from_response(resp)
        assert usage == {"prompt_tokens": 5, "completion_tokens": 10, "total_tokens": 15}

    def test_empty_response(self) -> None:
        usage = _extract_gemini_usage_from_response({})
        assert usage == _empty_usage()


# ---------------------------------------------------------------------------
# Integration tests: _stream_anthropic_native
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_anthropic_native_pipes_bytes_and_extracts_usage() -> None:
    """Native Anthropic stream pipes raw SSE bytes and extracts usage."""
    sse_chunks = _make_anthropic_sse(
        ("message_start", {
            "type": "message_start",
            "message": {
                "id": "msg_123",
                "type": "message",
                "role": "assistant",
                "model": "claude-sonnet-4-20250514",
                "content": [],
                "usage": {"input_tokens": 10, "output_tokens": 0},
            },
        }),
        ("content_block_start", {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "text", "text": ""},
        }),
        ("content_block_delta", {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": "Hello world"},
        }),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        ("message_delta", {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn"},
            "usage": {"output_tokens": 5},
        }),
        ("message_stop", {"type": "message_stop"}),
    )

    fake_service = AsyncMock()
    fake_service.repo = AsyncMock()
    fake_service.repo.get_active_quotas = AsyncMock(return_value=[])
    fake_service.quota = AsyncMock()

    collected: list[bytes] = []
    with patch(
        "src.gateway.router.finalize_gateway_request", new_callable=AsyncMock
    ):
        async for chunk in _stream_anthropic_native(
            response=_fake_byte_stream(sse_chunks),
            user=FakeUser(),
            logical_model=FakeLogicalModel(),
            service=fake_service,
            started_at=0.0,
            request_id="req-1",
            member="m-1",
            channel_id=42,
            upstream_model="anthropic/claude-sonnet-4-20250514",
        ):
            collected.append(chunk)

    # All original bytes passed through unchanged
    assert collected == sse_chunks

    # Verify the bytes are valid SSE (parseable)
    full_text = b"".join(collected).decode("utf-8")
    assert "event: message_start" in full_text
    assert "event: content_block_delta" in full_text
    assert "Hello world" in full_text
    assert "event: message_stop" in full_text


@pytest.mark.asyncio
async def test_stream_anthropic_native_records_usage() -> None:
    """Usage is extracted from SSE events and carried on ctx into finalize."""
    sse_chunks = _make_anthropic_sse(
        ("message_start", {
            "type": "message_start",
            "message": {"usage": {"input_tokens": 20, "output_tokens": 0}},
        }),
        ("content_block_delta", {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": "hi"},
        }),
        ("message_delta", {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn"},
            "usage": {"output_tokens": 12},
        }),
    )

    fake_service = AsyncMock()
    fake_service.repo = AsyncMock()
    fake_service.repo.get_active_quotas = AsyncMock(return_value=[])
    fake_service.quota = AsyncMock()

    with patch(
        "src.gateway.router.finalize_gateway_request", new_callable=AsyncMock
    ) as mock_finalize:
        async for _ in _stream_anthropic_native(
            response=_fake_byte_stream(sse_chunks),
            user=FakeUser(),
            logical_model=FakeLogicalModel(),
            service=fake_service,
            started_at=0.0,
            request_id="req-u",
            member="m-u",
            channel_id=7,
            upstream_model="anthropic/claude-sonnet-4-20250514",
        ):
            pass

    # Finalize is the single usage/log path; ctx carries the extracted usage.
    mock_finalize.assert_called_once()
    ctx = mock_finalize.call_args[0][0]
    assert ctx.prompt_tokens == 20
    assert ctx.completion_tokens == 12
    assert ctx.total_tokens == 32  # 20 + 12


# ---------------------------------------------------------------------------
# Integration tests: _stream_gemini_native
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_gemini_native_pipes_bytes_and_extracts_usage() -> None:
    """Native Gemini stream pipes raw SSE bytes and extracts usage."""
    sse_chunks = _make_gemini_sse(
        {
            "candidates": [{
                "content": {"role": "model", "parts": [{"text": "Hello"}]},
                "index": 0,
            }],
        },
        {
            "candidates": [{
                "content": {"role": "model", "parts": [{"text": " world"}]},
                "index": 0,
                "finishReason": "STOP",
            }],
            "usageMetadata": {
                "promptTokenCount": 8,
                "candidatesTokenCount": 4,
                "totalTokenCount": 12,
            },
        },
    )

    fake_service = AsyncMock()
    fake_service.repo = AsyncMock()
    fake_service.repo.get_active_quotas = AsyncMock(return_value=[])
    fake_service.quota = AsyncMock()

    collected: list[bytes] = []
    with patch(
        "src.gateway.router.finalize_gateway_request", new_callable=AsyncMock
    ):
        async for chunk in _stream_gemini_native(
            response=_fake_byte_stream(sse_chunks),
            user=FakeUser(),
            logical_model=FakeLogicalModel(),
            service=fake_service,
            started_at=0.0,
            request_id="req-g",
            member="m-g",
            channel_id=99,
            upstream_model="gemini/gemini-2.0-flash",
        ):
            collected.append(chunk)

    # Bytes passed through unchanged
    assert collected == sse_chunks

    # Parse to verify correctness
    full_text = b"".join(collected).decode("utf-8")
    assert "Hello" in full_text
    assert "STOP" in full_text


@pytest.mark.asyncio
async def test_stream_gemini_native_records_usage() -> None:
    """Usage from usageMetadata is extracted and recorded."""
    sse_chunks = _make_gemini_sse(
        {
            "candidates": [{"content": {"parts": [{"text": "ok"}]}}],
            "usageMetadata": {
                "promptTokenCount": 5,
                "candidatesTokenCount": 3,
                "totalTokenCount": 8,
            },
        },
    )

    fake_service = AsyncMock()
    fake_service.repo = AsyncMock()
    fake_service.repo.get_active_quotas = AsyncMock(return_value=[])
    fake_service.quota = AsyncMock()

    with patch(
        "src.gateway.router.finalize_gateway_request", new_callable=AsyncMock
    ) as mock_finalize:
        async for _ in _stream_gemini_native(
            response=_fake_byte_stream(sse_chunks),
            user=FakeUser(),
            logical_model=FakeLogicalModel(),
            service=fake_service,
            started_at=0.0,
            request_id="req-gu",
            member="m-gu",
            channel_id=11,
            upstream_model="gemini/gemini-2.0-flash",
        ):
            pass

    mock_finalize.assert_called_once()
    ctx = mock_finalize.call_args[0][0]
    assert ctx.prompt_tokens == 5
    assert ctx.completion_tokens == 3
    assert ctx.total_tokens == 8


# ---------------------------------------------------------------------------
# H2: concurrent semaphore release in stream finally
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_anthropic_releases_concurrent_slot() -> None:
    """When rate_limit_rules are passed, the slot is released in finally."""
    sse_chunks = _make_anthropic_sse(
        ("message_start", {
            "type": "message_start",
            "message": {"usage": {"input_tokens": 5, "output_tokens": 0}},
        }),
        ("message_stop", {"type": "message_stop"}),
    )

    fake_service = AsyncMock()
    fake_service.repo = AsyncMock()
    fake_service.repo.get_active_quotas = AsyncMock(return_value=[])
    fake_service.quota = AsyncMock()

    rules = [{"id": 1, "max_concurrent": 5, "subject_type": "user", "subject_id": 100}]

    with patch(
        "src.gateway.router.finalize_gateway_request", new_callable=AsyncMock
    ), patch(
        "src.gateway.router.release_concurrent", new_callable=AsyncMock
    ) as mock_release:
        async for _ in _stream_anthropic_native(
            response=_fake_byte_stream(sse_chunks),
            user=FakeUser(),
            logical_model=FakeLogicalModel(),
            service=fake_service,
            started_at=0.0,
            request_id="req-rel",
            member="m-rel",
            channel_id=7,
            upstream_model="anthropic/claude-sonnet-4-20250514",
            rate_limit_rules=rules,
        ):
            pass

    mock_release.assert_awaited_once_with("m-rel", rules)


# ---------------------------------------------------------------------------
# C: upstream iterator is closed in the stream finally (connection-leak guard)
# ---------------------------------------------------------------------------


class _ClosableByteStream:
    """An async byte iterator that records whether ``aclose()`` was called.

    Mirrors litellm's CustomStreamWrapper surface: it is its own async iterator
    and exposes ``aclose()``. Used to prove the stream finally closes the
    upstream so an abandoned stream cannot leak the upstream connection.
    """

    def __init__(self, chunks: list[bytes], *, abort_after: int | None = None) -> None:
        self._chunks = chunks
        self._index = 0
        self._abort_after = abort_after
        self.aclose_calls = 0

    def __aiter__(self) -> _ClosableByteStream:
        return self

    async def __anext__(self) -> bytes:
        if self._abort_after is not None and self._index >= self._abort_after:
            raise RuntimeError("simulated upstream failure mid-stream")
        if self._index >= len(self._chunks):
            raise StopAsyncIteration
        chunk = self._chunks[self._index]
        self._index += 1
        return chunk

    async def aclose(self) -> None:
        self.aclose_calls += 1


@pytest.mark.asyncio
async def test_stream_anthropic_closes_upstream_on_normal_completion() -> None:
    """The upstream iterator is closed once the stream drains normally."""
    sse_chunks = _make_anthropic_sse(
        ("message_start", {
            "type": "message_start",
            "message": {"usage": {"input_tokens": 5, "output_tokens": 0}},
        }),
        ("message_stop", {"type": "message_stop"}),
    )
    upstream = _ClosableByteStream(sse_chunks)

    fake_service = AsyncMock()
    fake_service.repo = AsyncMock()
    fake_service.repo.get_active_quotas = AsyncMock(return_value=[])
    fake_service.quota = AsyncMock()

    with patch(
        "src.gateway.router.finalize_gateway_request", new_callable=AsyncMock
    ):
        async for _ in _stream_anthropic_native(
            response=upstream,
            user=FakeUser(),
            logical_model=FakeLogicalModel(),
            service=fake_service,
            started_at=0.0,
            request_id="req-close",
            member="m-close",
            channel_id=7,
            upstream_model="anthropic/claude-sonnet-4-20250514",
        ):
            pass

    assert upstream.aclose_calls == 1


@pytest.mark.asyncio
async def test_stream_gemini_closes_upstream_on_abort() -> None:
    """An upstream that fails mid-stream is still closed in the finally block.

    Regression for bug C: without ``_aclose_response`` in the finally, an aborted
    stream (idle timeout, max-duration cap, client disconnect, upstream error)
    abandons the upstream iterator and leaks the connection.
    """
    sse_chunks = _make_gemini_sse(
        {"candidates": [{"content": {"parts": [{"text": "partial"}]}}]},
        {"candidates": [{"content": {"parts": [{"text": "never reached"}]}}]},
    )
    upstream = _ClosableByteStream(sse_chunks, abort_after=1)

    fake_service = AsyncMock()
    fake_service.repo = AsyncMock()
    fake_service.repo.get_active_quotas = AsyncMock(return_value=[])
    fake_service.quota = AsyncMock()

    collected: list[bytes] = []
    with (
        patch(
            "src.gateway.router.finalize_gateway_request", new_callable=AsyncMock
        ),
        pytest.raises(Exception),  # noqa: B017 - re-raised as upstream error
    ):
        async for chunk in _stream_gemini_native(
            response=upstream,
            user=FakeUser(),
            logical_model=FakeLogicalModel(),
            service=fake_service,
            started_at=0.0,
            request_id="req-abort",
            member="m-abort",
            channel_id=11,
            upstream_model="gemini/gemini-2.0-flash",
        ):
            collected.append(chunk)

    # One chunk made it through before the abort, and the upstream was closed.
    assert collected == sse_chunks[:1]
    assert upstream.aclose_calls == 1


@pytest.mark.asyncio
async def test_stream_gemini_no_release_without_rules() -> None:
    """No rate_limit_rules (non-stream-limited) means no release call."""
    sse_chunks = _make_gemini_sse(
        {
            "candidates": [{"content": {"parts": [{"text": "ok"}]}}],
            "usageMetadata": {
                "promptTokenCount": 5,
                "candidatesTokenCount": 3,
                "totalTokenCount": 8,
            },
        },
    )

    fake_service = AsyncMock()
    fake_service.repo = AsyncMock()
    fake_service.repo.get_active_quotas = AsyncMock(return_value=[])
    fake_service.quota = AsyncMock()

    with patch(
        "src.gateway.router.finalize_gateway_request", new_callable=AsyncMock
    ), patch(
        "src.gateway.router.release_concurrent", new_callable=AsyncMock
    ) as mock_release:
        async for _ in _stream_gemini_native(
            response=_fake_byte_stream(sse_chunks),
            user=FakeUser(),
            logical_model=FakeLogicalModel(),
            service=fake_service,
            started_at=0.0,
            request_id="req-gu2",
            member="m-gu2",
            channel_id=11,
            upstream_model="gemini/gemini-2.0-flash",
        ):
            pass

    mock_release.assert_not_called()


@pytest.mark.asyncio
async def test_stream_openai_records_estimated_tpm_and_downgrade() -> None:
    """Streaming finalizer carries real TPM reservation + downgrade markers."""
    chunks = [
        {
            "id": "chunk-1",
            "model": "openai/gpt-4o",
            "choices": [{"index": 0, "delta": {"content": "hi"}}],
            "_hidden_params": {
                "model_info": {
                    "id": "8",
                    "provider": "openai",
                    "upstream_model": "gpt-4o",
                }
            },
        },
        {
            "id": "chunk-2",
            "model": "openai/gpt-4o",
            "choices": [{"index": 0, "delta": {}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
            "_hidden_params": {
                "model_info": {
                    "id": "8",
                    "provider": "openai",
                    "upstream_model": "gpt-4o",
                }
            },
        },
    ]

    class Chunk:
        def __init__(self, data: dict[str, Any]) -> None:
            self._data = data
            self.model = data.get("model")
            self.usage = data.get("usage")
            self._hidden_params = data.get("_hidden_params")

        def model_dump(self) -> dict[str, Any]:
            return self._data

    async def response() -> AsyncIterator[Chunk]:
        for item in chunks:
            yield Chunk(item)

    with (
        patch("src.gateway.router.finalize_gateway_request", new_callable=AsyncMock) as finalize,
        patch(
            "src.gateway.router.detect_downgraded_features",
            return_value=["response_schema"],
        ),
    ):
        async for _ in _stream_openai(
            response=response(),
            user=FakeUser(),
            logical_model=FakeLogicalModel(),
            service=AsyncMock(),
            started_at=0.0,
            request_id="req-openai",
            member="member-openai",
            requested_features={"response_schema"},
            estimated_tokens=1234,
        ):
            pass

    finalize.assert_awaited_once()
    ctx = finalize.await_args.args[0]
    assert ctx.tpm_estimated_tokens == 1234
    assert ctx.channel_id == 8
    assert ctx.provider == "openai"
    assert ctx.downgraded_features == ["response_schema"]


# ---------------------------------------------------------------------------
# H3: shared rate-limit helper used by all three protocol endpoints
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_check_rate_limits_returns_rules_when_allowed() -> None:
    """Helper returns the applicable rules so the caller can release the slot."""
    from src.gateway.rate_limit import RateLimitCheckResult

    rules = [{"id": 1, "max_concurrent": 5, "subject_type": "user", "subject_id": 100}]
    fake_service = AsyncMock()
    fake_service.get_rate_limit_rules = AsyncMock(return_value=rules)

    with patch(
        "src.gateway.router.check_rate_limits", new_callable=AsyncMock
    ) as mock_check:
        mock_check.return_value = RateLimitCheckResult(allowed=True)
        result = await _check_rate_limits(
            service=fake_service,
            user=FakeUser(),
            logical_model_id=1,
            request_id="req-rl",
            member="m-rl",
            is_stream=True,
        )

    assert result == rules
    mock_check.assert_awaited_once()
    assert mock_check.await_args.kwargs["is_stream"] is True


@pytest.mark.asyncio
async def test_check_rate_limits_raises_429_when_denied() -> None:
    """Helper raises rate_limit_exceeded (429) when any limit is exceeded."""
    from src.enums import ErrorCode
    from src.exceptions import AppError
    from src.gateway.rate_limit import RateLimitCheckResult

    rules = [{"id": 1, "rpm_limit": 1, "subject_type": "user", "subject_id": 100}]
    fake_service = AsyncMock()
    fake_service.get_rate_limit_rules = AsyncMock(return_value=rules)

    with patch(
        "src.gateway.router.check_rate_limits", new_callable=AsyncMock
    ) as mock_check:
        mock_check.return_value = RateLimitCheckResult(
            allowed=False, denied_reason="rpm_exceeded"
        )
        with pytest.raises(AppError) as exc:
            await _check_rate_limits(
                service=fake_service,
                user=FakeUser(),
                logical_model_id=1,
                request_id="req-rl",
                member="m-rl",
                is_stream=False,
            )

    assert exc.value.code == ErrorCode.rate_limit_exceeded
    assert exc.value.status_code == 429


@pytest.mark.asyncio
async def test_check_rate_limits_skips_check_when_no_rules() -> None:
    """No rules configured means no check call and an empty list back."""
    fake_service = AsyncMock()
    fake_service.get_rate_limit_rules = AsyncMock(return_value=[])

    with patch(
        "src.gateway.router.check_rate_limits", new_callable=AsyncMock
    ) as mock_check:
        result = await _check_rate_limits(
            service=fake_service,
            user=FakeUser(),
            logical_model_id=1,
            request_id="req-rl",
            member="m-rl",
            is_stream=True,
        )

    assert result == []
    mock_check.assert_not_called()


# ---------------------------------------------------------------------------
# #7: a finalize failure must not strand the concurrent slot or upstream conn
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_finalize_failure_still_releases_slot_and_closes_upstream() -> None:
    """Oracle #7: if ``finalize_gateway_request`` raises, the concurrent slot
    release AND the upstream close MUST still happen.

    The old ``_finalize_stream`` ran the three cleanup steps in sequence inside a
    single shielded block, so a billing/quota exception in the (slow, DB-touching)
    finalize step skipped both ``release_concurrent`` and ``_aclose_response`` —
    permanently leaking the concurrent semaphore slot and the upstream HTTP
    connection. Resource cleanup must be independent of finalize success."""
    sse_chunks = _make_anthropic_sse(
        ("message_start", {
            "type": "message_start",
            "message": {"usage": {"input_tokens": 5, "output_tokens": 0}},
        }),
        ("message_stop", {"type": "message_stop"}),
    )
    upstream = _ClosableByteStream(sse_chunks)

    fake_service = AsyncMock()
    fake_service.repo = AsyncMock()
    fake_service.repo.get_active_quotas = AsyncMock(return_value=[])
    fake_service.quota = AsyncMock()

    rules = [{"id": 1, "max_concurrent": 5, "subject_type": "user", "subject_id": 100}]

    with (
        patch(
            "src.gateway.router.finalize_gateway_request", new_callable=AsyncMock
        ) as finalize,
        patch(
            "src.gateway.router.release_concurrent", new_callable=AsyncMock
        ) as mock_release,
    ):
        finalize.side_effect = RuntimeError("billing backend down")
        async for _ in _stream_anthropic_native(
            response=upstream,
            user=FakeUser(),
            logical_model=FakeLogicalModel(),
            service=fake_service,
            started_at=0.0,
            request_id="req-finalize-fail",
            member="m-finalize-fail",
            channel_id=7,
            upstream_model="anthropic/claude-sonnet-4-20250514",
            rate_limit_rules=rules,
        ):
            pass

    # Finalize was attempted and blew up, but neither resource leaked.
    finalize.assert_awaited_once()
    mock_release.assert_awaited_once_with("m-finalize-fail", rules)
    assert upstream.aclose_calls == 1


# ---------------------------------------------------------------------------
# #7 part 2: lifespan drains in-flight finalizers before Redis/DB teardown
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_spawn_finalizer_registers_and_auto_removes() -> None:
    """A spawned finalizer is tracked while running and removed on completion."""
    import asyncio

    from src.gateway.router import _inflight_finalizers, _spawn_finalizer

    started = asyncio.Event()
    release = asyncio.Event()

    async def _work() -> None:
        started.set()
        await release.wait()

    task = _spawn_finalizer(_work())
    await started.wait()
    # Registered while in-flight.
    assert task in _inflight_finalizers

    release.set()
    await task
    # Auto-removed via the done-callback once finished.
    assert task not in _inflight_finalizers


@pytest.mark.asyncio
async def test_drain_finalizers_waits_for_inflight_then_returns_zero() -> None:
    """Oracle #7 part 2: a still-running finalizer is awaited to completion by
    the drain, so its billing/quota settle lands before Redis/DB teardown."""
    import asyncio

    from src.gateway.router import _spawn_finalizer, drain_finalizers

    settled = False

    async def _settle() -> None:
        nonlocal settled
        await asyncio.sleep(0.05)
        settled = True

    _spawn_finalizer(_settle())
    unfinished = await drain_finalizers(timeout=1.0)

    assert unfinished == 0
    assert settled is True


@pytest.mark.asyncio
async def test_drain_finalizers_reports_stragglers_past_timeout() -> None:
    """A finalizer that overruns the drain timeout is counted, not awaited
    forever — shutdown must stay bounded."""
    import asyncio

    from src.gateway.router import _inflight_finalizers, _spawn_finalizer, drain_finalizers

    async def _wedged() -> None:
        await asyncio.sleep(10)

    task = _spawn_finalizer(_wedged())
    try:
        unfinished = await drain_finalizers(timeout=0.05)
        assert unfinished == 1
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        _inflight_finalizers.discard(task)


@pytest.mark.asyncio
async def test_drain_finalizers_empty_returns_zero() -> None:
    """No in-flight finalizers drains instantly to zero."""
    from src.gateway.router import drain_finalizers

    assert await drain_finalizers(timeout=0.01) == 0


# ---------------------------------------------------------------------------
# #12: a cancelled stream (client disconnect) must close the upstream connection
# BEFORE running the slow DB-touching finalize. If finalize ran first, a client
# disconnect mid-finalize would leave the upstream connection leaked while the
# settle blocks. Close-first returns the connection to the pool unconditionally.
# ---------------------------------------------------------------------------


class _OrderRecordingStream:
    """A byte stream that hangs after its chunks and records aclose ordering."""

    def __init__(self, chunks: list[bytes], order: list[str]) -> None:
        self._chunks = chunks
        self._index = 0
        self._order = order

    def __aiter__(self) -> _OrderRecordingStream:
        return self

    async def __anext__(self) -> bytes:
        import asyncio

        if self._index >= len(self._chunks):
            await asyncio.sleep(3600)  # hang → lets the consumer cancel us
            raise StopAsyncIteration
        chunk = self._chunks[self._index]
        self._index += 1
        return chunk

    async def aclose(self) -> None:
        self._order.append("aclose")


@pytest.mark.asyncio
async def test_cancelled_stream_closes_upstream_before_finalize() -> None:
    """Oracle #12: on cancellation the upstream is closed before finalize runs.

    Drives an Anthropic stream as a task, cancels it mid-flight (client
    disconnect), and asserts the recorded order is aclose THEN finalize — the
    upstream connection is returned to the pool before the slow settle."""
    import asyncio

    order: list[str] = []
    sse_chunks = _make_anthropic_sse(
        ("message_start", {"type": "message_start",
                           "message": {"usage": {"input_tokens": 5, "output_tokens": 0}}}),
    )
    upstream = _OrderRecordingStream(sse_chunks, order)

    async def _record_finalize(*_args: Any, **_kwargs: Any) -> None:
        order.append("finalize")

    async def _consume() -> None:
        async for _ in _stream_anthropic_native(
            response=upstream,
            user=FakeUser(),
            logical_model=FakeLogicalModel(),
            service=AsyncMock(),
            started_at=0.0,
            request_id="req-cancel-order",
            member="m-cancel-order",
            channel_id=7,
            upstream_model="claude-sonnet",
        ):
            pass

    with patch(
        "src.gateway.router.finalize_gateway_request",
        new=AsyncMock(side_effect=_record_finalize),
    ):
        task = asyncio.ensure_future(_consume())
        # Let the stream yield its first chunk and then block on the hang.
        await asyncio.sleep(0.05)
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    assert order == ["aclose", "finalize"]


# ---------------------------------------------------------------------------
# #11: on idle/duration timeout a stream must NOT emit its normal success
# terminator (OpenAI `data: [DONE]`) — that signals a clean completion and the
# client treats the truncated stream as whole. It must emit an error event so
# the client knows the response was cut short.
# ---------------------------------------------------------------------------


async def _hang_after(chunks: list[Any]) -> AsyncIterator[Any]:
    """Yield the given chunks, then hang forever (simulates an idle upstream)."""
    import asyncio

    for chunk in chunks:
        yield chunk
    await asyncio.sleep(3600)


@pytest.mark.asyncio
async def test_stream_openai_timeout_emits_error_not_done() -> None:
    """Oracle #11: an idle-timed-out OpenAI stream emits an SSE error event and
    NOT `data: [DONE]` — otherwise the client thinks the stream completed."""

    class Chunk:
        def __init__(self, data: dict[str, Any]) -> None:
            self._data = data
            self.model = data.get("model")
            self.usage = data.get("usage")
            self._hidden_params = data.get("_hidden_params")

        def model_dump(self) -> dict[str, Any]:
            return self._data

    first = Chunk({"id": "c1", "model": "openai/gpt-4o",
                   "choices": [{"index": 0, "delta": {"content": "hi"}}]})

    collected: list[str] = []
    with (
        patch("src.gateway.router.finalize_gateway_request", new_callable=AsyncMock),
        patch("src.gateway.router._STREAM_IDLE_TIMEOUT_SECONDS", 0.05),
    ):
        async for chunk in _stream_openai(
            response=_hang_after([first]),
            user=FakeUser(),
            logical_model=FakeLogicalModel(),
            service=AsyncMock(),
            started_at=0.0,
            request_id="req-openai-timeout",
            member="member-openai-timeout",
        ):
            collected.append(chunk if isinstance(chunk, str) else chunk.decode("utf-8"))

    body = "".join(collected)
    assert "[DONE]" not in body
    assert "error" in body.lower()


@pytest.mark.asyncio
async def test_stream_anthropic_timeout_emits_error_event() -> None:
    """An idle-timed-out Anthropic stream emits an SSE `error` event."""
    first = _make_anthropic_sse(
        ("message_start", {"type": "message_start",
                           "message": {"usage": {"input_tokens": 5, "output_tokens": 0}}}),
    )[0]

    collected: list[bytes] = []
    with (
        patch("src.gateway.router.finalize_gateway_request", new_callable=AsyncMock),
        patch("src.gateway.router._STREAM_IDLE_TIMEOUT_SECONDS", 0.05),
    ):
        async for chunk in _stream_anthropic_native(
            response=_hang_after([first]),
            user=FakeUser(),
            logical_model=FakeLogicalModel(),
            service=AsyncMock(),
            started_at=0.0,
            request_id="req-anthropic-timeout",
            member="member-anthropic-timeout",
            channel_id=1,
            upstream_model="claude-sonnet",
        ):
            collected.append(chunk if isinstance(chunk, bytes) else chunk.encode("utf-8"))

    body = b"".join(collected).decode("utf-8")
    assert "event: error" in body
    assert "error" in body.lower()


@pytest.mark.asyncio
async def test_stream_gemini_timeout_emits_error_event() -> None:
    """An idle-timed-out Gemini stream emits an SSE error payload."""
    first = _make_gemini_sse(
        {"candidates": [{"content": {"parts": [{"text": "hi"}]}}]},
    )[0]

    collected: list[bytes] = []
    with (
        patch("src.gateway.router.finalize_gateway_request", new_callable=AsyncMock),
        patch("src.gateway.router._STREAM_IDLE_TIMEOUT_SECONDS", 0.05),
    ):
        async for chunk in _stream_gemini_native(
            response=_hang_after([first]),
            user=FakeUser(),
            logical_model=FakeLogicalModel(),
            service=AsyncMock(),
            started_at=0.0,
            request_id="req-gemini-timeout",
            member="member-gemini-timeout",
            channel_id=1,
            upstream_model="gemini-2.0",
        ):
            collected.append(chunk if isinstance(chunk, bytes) else chunk.encode("utf-8"))

    body = b"".join(collected).decode("utf-8")
    assert "error" in body.lower()


# ---------------------------------------------------------------------------
# B1 + B2: the /v1/responses streaming path (endpoints_v1._stream_responses) was
# the odd one out — a bare `async for` with NO idle/duration timeout (B2), and a
# finalizer that ran the DB settle FIRST and was never registered for drain (B1).
# It now reuses router._finalize_stream via _spawn_finalizer and bounds each pull
# with an idle timeout, at parity with the chat/native providers above.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_responses_stream_idle_timeout_emits_error_event() -> None:
    """B2: a wedged upstream on /v1/responses now idle-times-out and emits an SSE
    error event instead of hanging forever holding the concurrent slot + TPM
    reservation. The Responses API has no `[DONE]` sentinel, so a truncated
    stream is signalled purely by the error event."""
    from src.gateway.endpoints_v1 import _stream_responses

    first = {"type": "response.output_text.delta", "delta": "hi"}

    collected: list[str] = []
    with (
        patch("src.gateway.router.finalize_gateway_request", new_callable=AsyncMock),
        patch("src.gateway.endpoints_v1._STREAM_IDLE_TIMEOUT_SECONDS", 0.05),
    ):
        async for chunk in _stream_responses(
            response=_hang_after([first]),
            user=FakeUser(),
            logical_model=FakeLogicalModel(),
            service=AsyncMock(),
            started_at=0.0,
            request_id="req-responses-timeout",
            member="member-responses-timeout",
            channel_id=1,
            upstream_model="gpt-4o",
        ):
            collected.append(chunk if isinstance(chunk, str) else chunk.decode("utf-8"))

    body = "".join(collected)
    assert "stream_timeout" in body
    assert "error" in body.lower()
    assert "[DONE]" not in body


@pytest.mark.asyncio
async def test_responses_stream_finalizer_drainable_and_closes_before_settle() -> None:
    """B1: the /v1/responses finalizer now (a) goes through _spawn_finalizer so the
    lifespan can drain it on shutdown, and (b) frees held resources (upstream
    close + concurrent slot) BEFORE the DB-touching billing settle. The old path
    ran finalize first inside a non-drainable shield."""
    import src.gateway.endpoints_v1 as ep
    from src.gateway.endpoints_v1 import _stream_responses

    calls: list[str] = []

    class _RecordingResponse:
        _done = False

        def __aiter__(self) -> Any:
            return self

        async def __anext__(self) -> dict[str, Any]:
            if not self._done:
                self._done = True
                return {"type": "x", "usage": {"input_tokens": 5, "output_tokens": 7}}
            raise StopAsyncIteration

        async def aclose(self) -> None:
            calls.append("aclose")

    async def _fake_finalize(*_a: Any, **_k: Any) -> None:
        calls.append("finalize")

    spawn_seen: list[Any] = []
    real_spawn = ep._spawn_finalizer

    def _spy_spawn(coro: Any) -> Any:
        task = real_spawn(coro)
        spawn_seen.append(task)
        return task

    rules = [
        {
            "id": 1,
            "subject_type": "user",
            "subject_id": 10,
            "logical_model_id": 1,
            "max_concurrent": 5,
        }
    ]
    with (
        patch("src.gateway.router.finalize_gateway_request", side_effect=_fake_finalize),
        patch("src.gateway.router.release_concurrent", new_callable=AsyncMock) as rel,
        patch.object(ep, "_spawn_finalizer", _spy_spawn),
    ):
        async for _ in _stream_responses(
            response=_RecordingResponse(),
            user=FakeUser(),
            logical_model=FakeLogicalModel(),
            service=AsyncMock(),
            started_at=0.0,
            request_id="req-b1",
            member="member-b1",
            channel_id=1,
            upstream_model="gpt-4o",
            rate_limit_rules=rules,
        ):
            pass

    # Routed through the drainable finalizer (registered in _inflight_finalizers).
    assert len(spawn_seen) == 1
    # Held resources freed before the billing settle.
    assert calls.index("aclose") < calls.index("finalize")
    rel.assert_awaited_once()


@pytest.mark.asyncio
async def test_responses_stream_usage_captured_when_client_disconnects_after_usage_event() -> None:
    """B2 follow-up regression: usage is parsed BEFORE the event is yielded, so a
    client that disconnects right after receiving the final usage-bearing event
    (breaking the consumer loop, which closes the generator mid-yield) still has
    its tokens recorded. Parsing after the yield would lose them and refund the
    full reservation — a revenue/limit hole. Mirrors router._stream_openai."""
    from src.gateway.endpoints_v1 import _stream_responses

    captured: dict[str, int] = {}

    async def _capture_finalize(ctx: Any, **_k: Any) -> None:
        captured["prompt"] = ctx.prompt_tokens
        captured["completion"] = ctx.completion_tokens
        captured["total"] = ctx.total_tokens

    class _UsageThenHang:
        _sent = False

        def __aiter__(self) -> Any:
            return self

        async def __anext__(self) -> dict[str, Any]:
            if not self._sent:
                self._sent = True
                return {"type": "response.completed",
                        "usage": {"input_tokens": 11, "output_tokens": 23}}
            import asyncio
            await asyncio.sleep(3600)  # would hang; consumer breaks before this

        async def aclose(self) -> None:
            return

    with (
        patch("src.gateway.router.finalize_gateway_request", side_effect=_capture_finalize),
        patch("src.gateway.router.release_concurrent", new_callable=AsyncMock),
    ):
        gen = _stream_responses(
            response=_UsageThenHang(),
            user=FakeUser(),
            logical_model=FakeLogicalModel(),
            service=AsyncMock(),
            started_at=0.0,
            request_id="req-b2-disconnect",
            member="member-b2-disconnect",
            channel_id=1,
            upstream_model="gpt-4o",
        )
        # Consume exactly the first (usage-bearing) chunk, then simulate a client
        # disconnect by closing the generator — its `finally` runs finalize.
        async for _chunk in gen:
            break
        await gen.aclose()

    # Usage was captured pre-yield, despite the disconnect right after it.
    assert captured["prompt"] == 11
    assert captured["completion"] == 23
    assert captured["total"] == 34
