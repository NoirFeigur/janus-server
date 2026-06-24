"""Tests for native Anthropic/Gemini streaming — SSE bytes parsing and usage extraction."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
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
    _stream_anthropic_native,
    _stream_gemini_native,
)

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
            channel_id=7,
            upstream_model="anthropic/claude-sonnet-4-20250514",
            rate_limit_rules=rules,
        ):
            pass

    mock_release.assert_awaited_once_with("req-rel", rules)


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
            channel_id=11,
            upstream_model="gemini/gemini-2.0-flash",
        ):
            pass

    mock_release.assert_not_called()


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
            is_stream=True,
        )

    assert result == []
    mock_check.assert_not_called()
