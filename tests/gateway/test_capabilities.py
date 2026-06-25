from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from src.gateway.capabilities import (
    CapabilityAwareDeploymentFilter,
    detect_downgraded_features,
    filter_deployments_by_requested_features,
    requested_features_from_params,
)


def test_requested_features_detects_tools_and_tool_choice() -> None:
    features = requested_features_from_params(
        messages=[
            {
                "role": "assistant",
                "tool_calls": [{"id": "call-1", "type": "function"}],
            }
        ],
        params={
            "tools": [{"type": "function", "function": {"name": "lookup"}}],
            "tool_choice": "required",
            "parallel_tool_calls": True,
        },
    )

    assert {"tools", "tool_choice", "parallel_tool_calls"}.issubset(features)


@pytest.mark.parametrize(
    ("model_info", "expected"),
    [
        ({"supports_response_schema": False}, ["response_schema"]),
        ({"supports_response_schema": True}, None),
    ],
)
def test_detect_downgraded_features_uses_model_info(
    model_info: dict[str, Any],
    expected: list[str] | None,
) -> None:
    fake_litellm = SimpleNamespace(
        get_model_info=lambda model, custom_llm_provider=None: model_info
    )

    with patch.dict("sys.modules", {"litellm": fake_litellm}):
        result = detect_downgraded_features(
            requested_features={"response_schema"},
            upstream_model="openai/gpt-test",
            provider=None,
        )

    assert result == expected


def test_filter_deployments_prefers_supported_candidates() -> None:
    deployments = [
        {
            "model_info": {
                "id": "1",
                "provider": "openai",
                "upstream_model": "no-tools",
            }
        },
        {
            "model_info": {
                "id": "2",
                "provider": "openai",
                "upstream_model": "with-tools",
            }
        },
    ]

    def fake_support(feature: str, *, model: str, provider: str | None) -> bool | None:
        return model == "with-tools"

    with patch("src.gateway.capabilities._feature_supported", side_effect=fake_support):
        result = filter_deployments_by_requested_features(deployments, {"tools"})

    assert [item["model_info"]["id"] for item in result] == ["2"]


def test_filter_deployments_preserves_candidates_when_no_support_match() -> None:
    deployments = [
        {"model_info": {"id": "1", "provider": "openai", "upstream_model": "a"}},
        {"model_info": {"id": "2", "provider": "openai", "upstream_model": "b"}},
    ]

    with patch("src.gateway.capabilities._feature_supported", return_value=False):
        result = filter_deployments_by_requested_features(deployments, {"tools"})

    assert result == deployments


@pytest.mark.asyncio
async def test_capability_filter_callback_uses_request_params() -> None:
    callback = CapabilityAwareDeploymentFilter()
    deployments = [
        {"model_info": {"id": "1", "provider": "openai", "upstream_model": "plain"}},
        {"model_info": {"id": "2", "provider": "openai", "upstream_model": "tools"}},
    ]

    def fake_support(feature: str, *, model: str, provider: str | None) -> bool | None:
        return feature == "tools" and model == "tools"

    with patch("src.gateway.capabilities._feature_supported", side_effect=fake_support):
        result = await callback.async_filter_deployments(
            model="gpt",
            healthy_deployments=deployments,
            messages=[],
            request_kwargs={"tools": [{"type": "function"}]},
        )

    assert [item["model_info"]["id"] for item in result] == ["2"]
