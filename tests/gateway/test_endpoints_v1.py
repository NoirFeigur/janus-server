"""Integration tests for src/gateway/endpoints_v1.py — /v1/embeddings, /v1/models, /v1/responses."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, Mock, patch

import pytest

from src.auth.service import AuthenticatedUser
from src.enums import ErrorCode, UsageStatus
from src.gateway.endpoints_v1 import (
    EmbeddingsRequest,
    ResponsesRequest,
    _embeddings_impl,
    _parse_body,
    _request_id,
    _responses_impl,
    _rollback_rate_limit_reservations,
    _stream_responses,
    _upstream_error,
    list_models,
)
from src.gateway.quota import QuotaCheckResult, QuotaReservation


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


def _fake_request() -> Mock:
    request = Mock()
    request.state.trace_id = "req-v1"
    return request


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

    @pytest.mark.parametrize(
        "reserved_key",
        [
            "base_url",
            "extra_headers",
            "aws_secret_access_key",
            "vertex_location",
            "azure_ad_token_provider",
            "specific_deployment",
        ],
    )
    def test_rejects_gateway_reserved_params(self, reserved_key: str) -> None:
        with pytest.raises(ValueError, match="gateway-reserved"):
            ResponsesRequest.model_validate(
                {"model": "gpt-4", "input": "hi", reserved_key: "bad"}
            )

    def test_allows_extra_responses_params(self) -> None:
        req = ResponsesRequest.model_validate(
            {
                "model": "gpt-4",
                "input": "hi",
                "tools": [{"type": "function", "function": {"name": "lookup"}}],
                "response_format": {"type": "json_object"},
            }
        )

        dumped = req.model_dump()
        assert dumped["tools"][0]["type"] == "function"
        assert dumped["response_format"]["type"] == "json_object"


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


def test_request_id_from_state() -> None:
    request = Mock()
    request.state.trace_id = "trace-abc"
    assert _request_id(request) == "trace-abc"


def test_request_id_fallback() -> None:
    request = Mock(spec=[])
    request.state = Mock(spec=[])
    rid = _request_id(request)
    assert len(rid) > 0  # UUID fallback


@pytest.mark.asyncio
async def test_parse_body_rejects_oversized_request() -> None:
    from src.config import get_settings

    max_body = get_settings().gateway_max_body_bytes
    request = Mock()

    async def stream():
        yield b"x" * (max_body + 1)

    request.stream = stream

    with pytest.raises(Exception) as exc:
        await _parse_body(request, ResponsesRequest)

    assert exc.value.status_code == 413


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


@pytest.mark.asyncio
async def test_embeddings_uses_rate_limit_quota_reservations_and_finalizer() -> None:
    reservation = QuotaReservation(
        key="quota:u:100:1:2026-06:tokens",
        quota_id=1,
        metric="tokens",
        scope="user",
        enforce=True,
        limit_value=Decimal("1000"),
    )
    service = AsyncMock()
    service.resolve_model.return_value = _fake_logical_model()
    service.get_rate_limit_rules.return_value = [{"id": 1, "tpm_limit": 1000}]
    service.check_quota.return_value = QuotaCheckResult(
        passed=True, reservations=[reservation]
    )
    router = AsyncMock()
    response = {"object": "embedding", "usage": {"prompt_tokens": 7, "total_tokens": 7}}
    router.aembedding.return_value = response

    with (
        patch("src.gateway.endpoints_v1.RouterManager.get_router", return_value=router),
        patch("src.gateway.endpoints_v1.check_rate_limits", new_callable=AsyncMock) as rl,
        patch(
            "src.gateway.endpoints_v1.finalize_gateway_request", new_callable=AsyncMock
        ) as finalize,
    ):
        rl.return_value = Mock(allowed=True)
        resp = await _embeddings_impl(
            request=_fake_request(),
            user=_fake_user(),
            service=service,
            payload=EmbeddingsRequest(model="gpt-4", input="hi"),
        )

    assert resp.status_code == 200
    service.settle_quota.assert_not_called()
    finalize.assert_awaited_once()
    kwargs = finalize.await_args.kwargs
    assert kwargs["quota_reservations"] == [reservation]
    ctx = finalize.await_args.args[0]
    assert ctx.total_tokens == 7
    assert ctx.tpm_estimated_tokens == 100
    assert ctx.status == UsageStatus.success.value


@pytest.mark.asyncio
async def test_embeddings_rolls_back_rate_limit_when_quota_rejects() -> None:
    service = AsyncMock()
    service.resolve_model.return_value = _fake_logical_model()
    service.get_rate_limit_rules.return_value = [{"id": 1, "tpm_limit": 1000}]
    service.check_quota.side_effect = RuntimeError("quota down")

    with (
        patch("src.gateway.endpoints_v1.check_rate_limits", new_callable=AsyncMock) as rl,
        patch("src.gateway.rate_limit.settle_tpm", new_callable=AsyncMock) as settle,
        patch("src.gateway.endpoints_v1.release_concurrent", new_callable=AsyncMock) as release,
        pytest.raises(RuntimeError, match="quota down"),
    ):
        rl.return_value = Mock(allowed=True)
        await _embeddings_impl(
            request=_fake_request(),
            user=_fake_user(),
            service=service,
            payload=EmbeddingsRequest(model="gpt-4", input="hi"),
        )

    settle.assert_awaited_once()
    assert settle.await_args.args[2] == 100
    release.assert_awaited_once()


@pytest.mark.asyncio
async def test_rollback_releases_rpm_tpm_and_concurrent() -> None:
    """Oracle #5: the v1 rollback path must release RPM too, not just TPM and
    concurrent. A request that passed RPM but is rejected by a later gate (quota)
    never runs, so its RPM sliding-window slot must be returned — otherwise the
    leaked slot throttles future admitted requests. router.py already did this;
    endpoints_v1 was missing the ``release_rpm`` step entirely."""
    rules = [{"id": 1, "rpm_limit": 10, "tpm_limit": 1000, "max_concurrent": 2}]
    with (
        patch("src.gateway.rate_limit.settle_tpm", new_callable=AsyncMock) as settle,
        patch("src.gateway.rate_limit.release_rpm", new_callable=AsyncMock) as rpm,
        patch(
            "src.gateway.endpoints_v1.release_concurrent", new_callable=AsyncMock
        ) as conc,
    ):
        await _rollback_rate_limit_reservations(
            request_id="req-1",
            member="m-1",
            rate_limit_rules=rules,
            estimated_tokens=100,
        )

    settle.assert_awaited_once()
    rpm.assert_awaited_once_with("m-1", rules)
    conc.assert_awaited_once_with("m-1", rules)


@pytest.mark.asyncio
async def test_responses_forwards_extra_params_and_records_downgrade() -> None:
    service = AsyncMock()
    service.resolve_model.return_value = _fake_logical_model()
    service.get_rate_limit_rules.return_value = [{"id": 1, "tpm_limit": 1000}]
    service.check_quota.return_value = QuotaCheckResult(passed=True, reservations=[])
    router = AsyncMock()

    async def fake_aresponses(**kwargs):
        meta = kwargs["litellm_metadata"]
        meta["deployment"] = "openai/no-tools"
        meta["model_info"] = {"provider": "openai"}
        return {
            "id": "resp-1",
            "model": "openai/no-tools",
            "usage": {"input_tokens": 5, "output_tokens": 2, "total_tokens": 7},
        }

    router.aresponses.side_effect = fake_aresponses

    with (
        patch("src.gateway.endpoints_v1.RouterManager.get_router", return_value=router),
        patch("src.gateway.endpoints_v1.check_rate_limits", new_callable=AsyncMock) as rl,
        patch(
            "src.gateway.endpoints_v1.finalize_gateway_request", new_callable=AsyncMock
        ) as finalize,
        patch(
            "src.gateway.endpoints_v1.detect_downgraded_features",
            return_value=["tools", "response_schema"],
        ) as detect,
    ):
        rl.return_value = Mock(allowed=True)
        resp = await _responses_impl(
            request=_fake_request(),
            user=_fake_user(),
            service=service,
            payload=ResponsesRequest.model_validate(
                {
                    "model": "gpt-4",
                    "input": "hi",
                    "tools": [{"type": "function", "function": {"name": "lookup"}}],
                    "response_format": {"type": "json_object"},
                }
            ),
        )

    assert resp.status_code == 200
    assert resp.headers["X-Gateway-Downgraded"] == "tools,response_schema"
    router.aresponses.assert_awaited_once()
    forwarded = router.aresponses.await_args.kwargs
    assert forwarded["tools"][0]["type"] == "function"
    assert forwarded["response_format"] == {"type": "json_object"}
    detect.assert_called_once()
    assert detect.call_args.kwargs["requested_features"] == {"tools", "response_schema"}
    ctx = finalize.await_args.args[0]
    assert ctx.downgraded_features == ["tools", "response_schema"]
    assert ctx.total_tokens == 7


class _ClosableEventStream:
    def __init__(self) -> None:
        self.aclose_calls = 0

    def __aiter__(self) -> _ClosableEventStream:
        return self

    async def __anext__(self) -> dict[str, object]:
        raise StopAsyncIteration

    async def aclose(self) -> None:
        self.aclose_calls += 1


@pytest.mark.asyncio
async def test_responses_stream_cleanup_is_shielded_and_releases_slot() -> None:
    upstream = _ClosableEventStream()
    rules = [{"id": 1, "max_concurrent": 1, "subject_type": "user", "subject_id": 100}]

    with (
        patch(
            "src.gateway.router.finalize_gateway_request", new_callable=AsyncMock
        ) as finalize,
        patch(
            "src.gateway.router.release_concurrent", new_callable=AsyncMock
        ) as release,
    ):
        async for _ in _stream_responses(
            response=upstream,
            user=_fake_user(),
            logical_model=_fake_logical_model(),
            service=AsyncMock(),
            started_at=0.0,
            request_id="req-stream",
            member="member-stream",
            channel_id=1,
            upstream_model="openai/gpt-4",
            estimated_tokens=333,
            rate_limit_rules=rules,
        ):
            pass

    finalize.assert_awaited_once()
    ctx = finalize.await_args.args[0]
    assert ctx.tpm_estimated_tokens == 333
    release.assert_awaited_once_with("member-stream", rules)
    assert upstream.aclose_calls == 1


@pytest.mark.asyncio
async def test_list_models_batches_granted_model_lookup() -> None:
    service = AsyncMock()
    model = _fake_logical_model()
    service.repo.get_user_granted_models.return_value = {model.id}
    service.repo.get_logical_models_by_ids.return_value = [model]

    resp = await list_models(user=_fake_user(), service=service)

    assert resp.status_code == 200
    service.repo.get_logical_models_by_ids.assert_awaited_once_with([model.id])
    service.repo.get_logical_model_by_id.assert_not_called()
