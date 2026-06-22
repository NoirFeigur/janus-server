from __future__ import annotations

from typing import Any

from litellm import Router

from src.core.channel_crypto import decrypt_channel_key
from src.gateway.repository import RouterDeploymentRow


def build_router(rows: list[RouterDeploymentRow]) -> Router:
    """Build a LiteLLM router from active gateway deployment rows."""
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
            }
        )
    return Router(
        model_list=model_list,
        routing_strategy="usage-based-routing-v2",
        enable_pre_call_checks=True,
    )
