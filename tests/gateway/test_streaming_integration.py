"""Integration tests: FastAPI streaming endpoints return valid SSE via TestClient.

These tests mount the gateway router on a minimal FastAPI app, override auth +
service dependencies, and verify SSE bytes flow correctly through httpx's
async streaming interface.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi import FastAPI

from src.auth.dependencies import get_current_user
from src.auth.service import AuthenticatedUser
from src.gateway.dependencies import get_gateway_service
from src.gateway.quota import QuotaCheckResult
from src.gateway.router import router as gateway_router

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _test_user() -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id=1,
        username="integration_tester",
        department_id=1,
        permissions=frozenset(),
    )


def _build_app() -> FastAPI:
    app = FastAPI()
    app.include_router(gateway_router)

    async def _override_user() -> AuthenticatedUser:
        return _test_user()

    app.dependency_overrides[get_current_user] = _override_user
    return app


async def _fake_aiter(chunks: list[bytes]) -> AsyncIterator[bytes]:
    for chunk in chunks:
        yield chunk


# ---------------------------------------------------------------------------
# Anthropic streaming integration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_anthropic_stream_returns_sse_bytes() -> None:
    """POST /v1/messages with stream=true returns text/event-stream SSE."""
    app = _build_app()

    sse_chunks = [
        b"event: message_start\ndata: "
        b'{"type":"message_start","message":{"usage":'
        b'{"input_tokens":5,"output_tokens":0}}}\n\n',
        b"event: content_block_delta\ndata: "
        b'{"type":"content_block_delta","index":0,'
        b'"delta":{"type":"text_delta","text":"Hi"}}\n\n',
        b"event: message_delta\ndata: "
        b'{"type":"message_delta","delta":{"stop_reason":"end_turn"},'
        b'"usage":{"output_tokens":1}}\n\n',
        b'event: message_stop\ndata: {"type":"message_stop"}\n\n',
    ]

    fake_service = AsyncMock()
    fake_service.resolve_model = AsyncMock()
    fake_service.resolve_model.return_value = AsyncMock(
        id=1, name="claude-sonnet", price_input=None, price_output=None
    )
    fake_service.check_quota = AsyncMock(
        return_value=QuotaCheckResult(passed=True)
    )
    fake_service.settle_quota = AsyncMock()
    fake_service.repo = AsyncMock()
    fake_service.repo.get_active_quotas = AsyncMock(return_value=[])
    fake_service.quota = AsyncMock()

    app.dependency_overrides[get_gateway_service] = lambda: fake_service

    fake_router = AsyncMock()
    fake_router.aanthropic_messages = AsyncMock(
        return_value=_fake_aiter(sse_chunks)
    )

    with patch(
        "src.gateway.router.RouterManager.get_router", return_value=fake_router
    ), patch(
        "src.gateway.router.settle_quota_independent", new_callable=AsyncMock
    ), patch(
        "src.gateway.router.record_usage", new_callable=AsyncMock
    ):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/v1/messages",
                json={
                    "model": "claude-sonnet",
                    "messages": [{"role": "user", "content": "hi"}],
                    "max_tokens": 100,
                    "stream": True,
                },
                headers={"Authorization": "Bearer fake-jwt"},
            )

    assert response.status_code == 200
    assert "text/event-stream" in response.headers["content-type"]
    body = response.content.decode("utf-8")
    assert "event: message_start" in body
    assert "event: content_block_delta" in body
    assert "Hi" in body
    assert "event: message_stop" in body


# ---------------------------------------------------------------------------
# Gemini streaming integration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gemini_stream_returns_sse_bytes() -> None:
    """POST /v1beta/models/{model}:streamGenerateContent returns SSE."""
    app = _build_app()

    sse_chunks = [
        b"data: "
        b'{"candidates":[{"content":{"parts":[{"text":"Hello"}]}}],'
        b'"usageMetadata":{"promptTokenCount":3,'
        b'"candidatesTokenCount":2,"totalTokenCount":5}}\n\n',
    ]

    fake_service = AsyncMock()
    fake_service.resolve_model = AsyncMock()
    fake_service.resolve_model.return_value = AsyncMock(
        id=2, name="gemini-flash", price_input=None, price_output=None
    )
    fake_service.check_quota = AsyncMock(
        return_value=QuotaCheckResult(passed=True)
    )
    fake_service.settle_quota = AsyncMock()
    fake_service.repo = AsyncMock()
    fake_service.repo.get_active_quotas = AsyncMock(return_value=[])
    fake_service.quota = AsyncMock()

    app.dependency_overrides[get_gateway_service] = lambda: fake_service

    fake_router = AsyncMock()
    fake_router.agenerate_content_stream = AsyncMock(
        return_value=_fake_aiter(sse_chunks)
    )

    with patch(
        "src.gateway.router.RouterManager.get_router", return_value=fake_router
    ), patch(
        "src.gateway.router.settle_quota_independent", new_callable=AsyncMock
    ), patch(
        "src.gateway.router.record_usage", new_callable=AsyncMock
    ):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/v1beta/models/gemini-flash:streamGenerateContent",
                json={
                    "contents": [
                        {"role": "user", "parts": [{"text": "hello"}]}
                    ],
                },
                headers={"Authorization": "Bearer fake-jwt"},
            )

    assert response.status_code == 200
    assert "text/event-stream" in response.headers["content-type"]
    body = response.content.decode("utf-8")
    assert "Hello" in body
    assert "totalTokenCount" in body


# ---------------------------------------------------------------------------
# OpenAI streaming integration
# ---------------------------------------------------------------------------


class _FakeOpenAIChunk:
    """Mimics a litellm streaming chunk (OpenAI format)."""

    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data
        # litellm chunks expose model_dump / dict-like access
        for k, v in data.items():
            setattr(self, k, v)

    def model_dump(self, **_: Any) -> dict[str, Any]:
        return self._data


async def _fake_openai_stream(
    chunks: list[dict[str, Any]],
) -> AsyncIterator[_FakeOpenAIChunk]:
    for data in chunks:
        yield _FakeOpenAIChunk(data)


@pytest.mark.asyncio
async def test_openai_stream_returns_sse_text() -> None:
    """POST /v1/chat/completions with stream=true returns SSE text."""
    app = _build_app()

    openai_chunks = [
        {
            "id": "chatcmpl-1",
            "object": "chat.completion.chunk",
            "choices": [
                {"index": 0, "delta": {"role": "assistant", "content": "Hi"}}
            ],
        },
        {
            "id": "chatcmpl-1",
            "object": "chat.completion.chunk",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            "usage": {
                "prompt_tokens": 5,
                "completion_tokens": 1,
                "total_tokens": 6,
            },
        },
    ]

    fake_service = AsyncMock()
    fake_service.resolve_model = AsyncMock()
    fake_service.resolve_model.return_value = AsyncMock(
        id=3, name="gpt-4o", price_input=None, price_output=None
    )
    fake_service.check_quota = AsyncMock(
        return_value=QuotaCheckResult(passed=True)
    )
    fake_service.settle_quota = AsyncMock()
    fake_service.repo = AsyncMock()
    fake_service.repo.get_active_quotas = AsyncMock(return_value=[])
    fake_service.quota = AsyncMock()

    app.dependency_overrides[get_gateway_service] = lambda: fake_service

    fake_router = AsyncMock()
    fake_router.acompletion = AsyncMock(
        return_value=_fake_openai_stream(openai_chunks)
    )

    with patch(
        "src.gateway.router.RouterManager.get_router", return_value=fake_router
    ), patch(
        "src.gateway.router.settle_quota_independent", new_callable=AsyncMock
    ), patch(
        "src.gateway.router.record_usage", new_callable=AsyncMock
    ):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/v1/chat/completions",
                json={
                    "model": "gpt-4o",
                    "messages": [{"role": "user", "content": "hi"}],
                    "stream": True,
                },
                headers={"Authorization": "Bearer fake-jwt"},
            )

    assert response.status_code == 200
    assert "text/event-stream" in response.headers["content-type"]
    body = response.content.decode("utf-8")
    assert "Hi" in body
