from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from litellm import Router

from src.config import get_settings
from src.core.channel_crypto import decrypt_channel_key
from src.gateway.repository import RouterDeploymentRow


def build_router(rows: list[RouterDeploymentRow]) -> Router:
    """Build a LiteLLM router from active gateway deployment rows."""
    settings = get_settings()
    model_list: list[dict[str, Any]] = []
    for row in rows:
        litellm_params: dict[str, Any] = {
            "model": f"{row.provider}/{row.upstream_model}",
            "api_key": decrypt_channel_key(row.api_key_encrypted),
        }
        if row.api_base is not None:
            litellm_params["api_base"] = row.api_base
        if row.key_rpm_limit is not None:
            litellm_params["rpm"] = row.key_rpm_limit
        if row.key_tpm_limit is not None:
            litellm_params["tpm"] = row.key_tpm_limit
        model_list.append(
            {
                "model_name": row.logical_model_name,
                "litellm_params": litellm_params,
                "model_info": {"id": str(row.channel_key_id)},
                "weight": row.deployment_weight * row.key_weight,
                "priority": row.deployment_priority,
            }
        )
    return Router(
        model_list=model_list,
        routing_strategy="usage-based-routing-v2",
        enable_pre_call_checks=True,
        redis_host=_parse_redis_host(settings.redis_url),
        redis_port=_parse_redis_port(settings.redis_url),
        redis_password=_parse_redis_password(settings.redis_url),
        num_retries=2,
        timeout=120,
        cooldown_time=10,
        allowed_fails=3,
        retry_after=5,
    )


def _parse_redis_host(url: str) -> str | None:
    """Extract host from redis://[:password@]host:port/db URL."""
    parsed = urlparse(url)
    return parsed.hostname


def _parse_redis_port(url: str) -> int | None:
    parsed = urlparse(url)
    return parsed.port or 6379


def _parse_redis_password(url: str) -> str | None:
    parsed = urlparse(url)
    return parsed.password
