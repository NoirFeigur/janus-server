from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from litellm.integrations.custom_logger import CustomLogger

_DOWNGRADED_HEADER = "X-Gateway-Downgraded"
_SUPPORT_FUNCTIONS = {
    "parallel_tool_calls": "supports_parallel_function_calling",
    "prompt_caching": "supports_prompt_caching",
    "reasoning": "supports_reasoning",
    "response_schema": "supports_response_schema",
    "tool_choice": "supports_tool_choice",
    "tools": "supports_function_calling",
    "vision": "supports_vision",
}
_MODEL_INFO_KEYS = {
    "parallel_tool_calls": ("supports_parallel_function_calling",),
    "prompt_caching": ("supports_prompt_caching",),
    "reasoning": ("supports_reasoning",),
    "response_schema": ("supports_response_schema",),
    "tool_choice": ("supports_tool_choice",),
    "tools": ("supports_function_calling",),
    "vision": ("supports_vision",),
}


def downgraded_header(features: list[str] | None) -> dict[str, str]:
    """Return response headers exposing downgraded features, if any."""
    if not features:
        return {}
    return {_DOWNGRADED_HEADER: ",".join(features)}


def merge_headers(*headers: dict[str, str]) -> dict[str, str]:
    """Merge optional response header dicts."""
    merged: dict[str, str] = {}
    for item in headers:
        merged.update(item)
    return merged


def requested_features_from_params(
    *,
    messages: list[Any] | None = None,
    params: Mapping[str, Any] | None = None,
) -> set[str]:
    """Infer protocol features requested by a caller.

    This intentionally stays conservative: only features we can detect reliably
    are marked, so a header never claims a downgrade for a feature the client did
    not request.
    """
    features: set[str] = set()
    params = params or {}
    if _messages_include_cache_control(messages):
        features.add("prompt_caching")
    if params.get("cachedContent") is not None or params.get("cached_content") is not None:
        features.add("prompt_caching")
    if params.get("thinking") is not None or params.get("reasoning_effort") is not None:
        features.add("reasoning")
    if params.get("response_format") is not None:
        features.add("response_schema")
    if params.get("tools") is not None or params.get("functions") is not None:
        features.add("tools")
    if _messages_include_tools(messages):
        features.add("tools")
    if params.get("tool_choice") is not None or params.get("function_call") is not None:
        features.add("tool_choice")
    if params.get("parallel_tool_calls") is not None:
        features.add("parallel_tool_calls")
    if _messages_include_vision(messages):
        features.add("vision")
    return features


def detect_downgraded_features(
    *,
    requested_features: set[str],
    upstream_model: str | None,
    provider: str | None,
) -> list[str] | None:
    """Return requested features not supported by the selected upstream model.

    Unknown model/provider information is treated as "not enough evidence" and
    therefore does not emit a downgrade marker.
    """
    if not requested_features or not upstream_model:
        return None
    provider = provider or _provider_from_model(upstream_model)
    model = _strip_provider_prefix(upstream_model, provider)
    downgraded: list[str] = []
    for feature in sorted(requested_features):
        supported = _feature_supported(feature, model=model, provider=provider)
        if supported is False:
            downgraded.append(feature)
    return downgraded or None


def filter_deployments_by_requested_features(
    deployments: list[dict[str, Any]],
    requested_features: set[str],
) -> list[dict[str, Any]]:
    """Prefer deployments that explicitly support all requested features.

    This is a soft filter: if no currently healthy deployment is known to support
    the requested feature set, keep the original list so Router fallback and
    high-availability behavior are preserved. Downstream downgrade headers and
    usage records then make that fallback visible.
    """
    if not requested_features or len(deployments) <= 1:
        return deployments
    supported = [
        deployment
        for deployment in deployments
        if _deployment_supports_all_features(deployment, requested_features)
    ]
    return supported or deployments


def ensure_capability_filter_registered() -> None:
    """Register the LiteLLM Router deployment filter once per process."""
    try:
        import litellm

        filter_registered = any(
            isinstance(callback, CapabilityAwareDeploymentFilter)
            for callback in litellm.callbacks
        )
        if filter_registered:
            return
        litellm.logging_callback_manager.add_litellm_callback(
            CapabilityAwareDeploymentFilter()
        )
    except Exception:
        return


class CapabilityAwareDeploymentFilter(CustomLogger):
    """LiteLLM callback that implements G13 capability-aware soft routing."""

    async def async_filter_deployments(
        self,
        model: str,
        healthy_deployments: list[dict[str, Any]],
        messages: list[Any] | None,
        request_kwargs: dict[str, Any] | None = None,
        parent_otel_span: Any | None = None,
    ) -> list[dict[str, Any]]:
        del model, parent_otel_span
        requested_features = requested_features_from_params(
            messages=messages,
            params=request_kwargs or {},
        )
        return filter_deployments_by_requested_features(
            healthy_deployments,
            requested_features,
        )


def _messages_include_cache_control(messages: list[Any] | None) -> bool:
    if not isinstance(messages, list):
        return False
    return any(_contains_key(message, "cache_control") for message in messages)


def _messages_include_tools(messages: list[Any] | None) -> bool:
    if not isinstance(messages, list):
        return False
    for message in messages:
        if not isinstance(message, Mapping):
            continue
        if message.get("role") == "tool":
            return True
        if message.get("tool_calls") is not None or message.get("function_call") is not None:
            return True
    return False


def _messages_include_vision(messages: list[Any] | None) -> bool:
    if not isinstance(messages, list):
        return False
    for message in messages:
        if not isinstance(message, Mapping):
            continue
        content = message.get("content")
        if isinstance(content, list):
            for part in content:
                if not isinstance(part, Mapping):
                    continue
                if part.get("type") in {"image_url", "input_image"}:
                    return True
                if part.get("image_url") is not None or part.get("source") is not None:
                    return True
    return False


def _contains_key(value: Any, key: str) -> bool:
    if isinstance(value, Mapping):
        return key in value or any(_contains_key(item, key) for item in value.values())
    if isinstance(value, list):
        return any(_contains_key(item, key) for item in value)
    return False


def _strip_provider_prefix(model: str, provider: str | None) -> str:
    if provider and model.startswith(f"{provider}/"):
        return model[len(provider) + 1 :]
    if "/" in model:
        return model.split("/", 1)[1]
    return model


def _provider_from_model(model: str) -> str | None:
    if "/" not in model:
        return None
    provider, _model = model.split("/", 1)
    return provider or None


def _deployment_supports_all_features(
    deployment: Mapping[str, Any],
    requested_features: set[str],
) -> bool:
    provider, upstream_model = _deployment_provider_and_model(deployment)
    if upstream_model is None:
        return False
    for feature in requested_features:
        if _feature_supported(feature, model=upstream_model, provider=provider) is not True:
            return False
    return True


def _deployment_provider_and_model(deployment: Mapping[str, Any]) -> tuple[str | None, str | None]:
    model_info = deployment.get("model_info")
    litellm_params = deployment.get("litellm_params")
    if not isinstance(model_info, Mapping):
        model_info = {}
    if not isinstance(litellm_params, Mapping):
        litellm_params = {}
    provider = model_info.get("provider")
    upstream_model = model_info.get("upstream_model")
    if not isinstance(provider, str):
        provider = None
    if not isinstance(upstream_model, str):
        raw_model = litellm_params.get("model")
        upstream_model = raw_model if isinstance(raw_model, str) else None
    if upstream_model is None:
        return provider, None
    provider = provider or _provider_from_model(upstream_model)
    return provider, _strip_provider_prefix(upstream_model, provider)


def _feature_supported(feature: str, *, model: str, provider: str | None) -> bool | None:
    try:
        import litellm

        for key in _MODEL_INFO_KEYS.get(feature, ()):
            value = _model_info_value(litellm, model=model, provider=provider, key=key)
            if isinstance(value, bool):
                return value
        fn = _support_function(litellm, _SUPPORT_FUNCTIONS.get(feature))
        if fn is not None:
            return True if fn(model, custom_llm_provider=provider) else None
    except Exception:
        return None
    return None


def _support_function(litellm: Any, name: str | None) -> Any | None:
    if name is None:
        return None
    fn = getattr(litellm, name, None)
    if fn is not None:
        return fn
    utils = getattr(litellm, "utils", None)
    if utils is not None:
        return getattr(utils, name, None)
    return None


def _model_info_value(
    litellm: Any,
    *,
    model: str,
    provider: str | None,
    key: str,
) -> Any:
    info = litellm.get_model_info(model=model, custom_llm_provider=provider)
    if isinstance(info, Mapping):
        return info.get(key)
    return getattr(info, key, None)
