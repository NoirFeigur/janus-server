from __future__ import annotations

import pytest
from cryptography.fernet import Fernet
from pydantic import SecretStr

from src.config import get_settings
from src.core.channel_crypto import encrypt_channel_key
from src.gateway.repository import RouterDeploymentRow
from src.gateway.router_factory import build_router

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _set_fernet_key(monkeypatch: pytest.MonkeyPatch) -> None:
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("JANUS_CHANNEL_ENCRYPTION_KEYS", key)
    monkeypatch.setattr(get_settings(), "channel_encryption_keys", SecretStr(key))
    from src.core import channel_crypto

    channel_crypto._cipher.cache_clear()


def _row(*, api_base: str | None = "https://api.example.test") -> RouterDeploymentRow:
    return RouterDeploymentRow(
        logical_model_name="claude-sonnet",
        logical_model_id=10,
        upstream_model="claude-3-5-sonnet",
        provider="anthropic",
        protocol="anthropic",
        api_base=api_base,
        api_key_encrypted=encrypt_channel_key("sk-plaintext"),
        channel_id=20,
        channel_key_id=30,
        deployment_weight=1,
        deployment_priority=0,
        key_weight=1,
        key_rpm_limit=100,
        key_tpm_limit=1000,
    )


async def test_build_router_creates_deployments() -> None:
    router = build_router([_row(), _row()])

    assert len(router.model_list) == 2
    assert {item["model_name"] for item in router.model_list} == {"claude-sonnet"}


async def test_build_router_decrypts_keys() -> None:
    router = build_router([_row()])

    params = router.model_list[0]["litellm_params"]

    assert params["api_key"] == "sk-plaintext"


async def test_build_router_omits_api_base_when_none() -> None:
    router = build_router([_row(api_base=None)])
    params = router.model_list[0]["litellm_params"]

    assert "api_base" not in params
