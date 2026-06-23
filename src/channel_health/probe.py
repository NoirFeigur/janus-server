"""Recovery probe for degraded channels.

Directly calls LiteLLM acompletion bypassing Router/quota/usage to test if an
upstream channel is reachable.  Lightweight: ``max_tokens=1``, short timeout.
"""

from __future__ import annotations

from typing import Any

import litellm

from src.core.logging import get_logger

_log = get_logger(__name__)

_PROBE_TIMEOUT_SECONDS = 10
_PROBE_MESSAGES = [{"role": "user", "content": "ping"}]


async def probe_channel(
    *,
    provider: str,
    upstream_model: str,
    api_key: str,
    api_base: str | None = None,
) -> bool:
    """Send a minimal completion request to test channel liveness.

    Returns True if the upstream responded successfully, False otherwise.
    Does NOT consume Janus quota or record usage.
    """
    model_name = f"{provider}/{upstream_model}" if provider else upstream_model
    kwargs: dict[str, Any] = {
        "model": model_name,
        "messages": _PROBE_MESSAGES,
        "max_tokens": 1,
        "temperature": 0,
        "timeout": _PROBE_TIMEOUT_SECONDS,
        "api_key": api_key,
    }
    if api_base:
        kwargs["api_base"] = api_base

    try:
        await litellm.acompletion(**kwargs)
        return True
    except Exception as exc:
        _log.debug(
            "channel_health.probe_failed",
            provider=provider,
            upstream_model=upstream_model,
            error=str(exc),
        )
        return False
