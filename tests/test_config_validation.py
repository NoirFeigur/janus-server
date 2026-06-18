"""Tests for production config fail-fast (src/config.validate_runtime).

``local`` is a free pass (dev convenience). Any non-local environment must
satisfy every safety invariant or the process refuses to start; the checks
accumulate so an operator sees all problems at once.
"""

from __future__ import annotations

import pytest
from pydantic import SecretStr

from src.config import ConfigError, Settings, validate_runtime

# A throwaway RS256-shaped placeholder; validate_runtime only checks presence,
# never parses the key (signing does that, lazily).
_FAKE_KEY = SecretStr("-----BEGIN PRIVATE KEY-----\nx\n-----END PRIVATE KEY-----")


def _prod(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "environment": "production",
        "debug": False,
        "platform_jwt_private_key": _FAKE_KEY,
        "cors_allow_origins": ["https://admin.example.com"],
        "trusted_proxy_count": 1,
        "oss_access_key": SecretStr("AK"),
        "oss_secret_key": SecretStr("SK"),
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def test_local_skips_all_checks() -> None:
    # Even with every prod-unsafe value, local is exempt by design.
    settings = Settings(
        environment="local",
        debug=True,
        platform_jwt_private_key=None,
        cors_allow_origins=["*"],
        trusted_proxy_count=0,
    )
    validate_runtime(settings)  # must not raise


def test_production_happy_path_passes() -> None:
    validate_runtime(_prod())  # must not raise


def test_missing_jwt_key_rejected() -> None:
    with pytest.raises(ConfigError, match="JWT_PRIVATE_KEY"):
        validate_runtime(_prod(platform_jwt_private_key=None))


def test_debug_true_rejected() -> None:
    with pytest.raises(ConfigError, match="debug"):
        validate_runtime(_prod(debug=True))


def test_wildcard_cors_rejected() -> None:
    with pytest.raises(ConfigError, match="cors"):
        validate_runtime(_prod(cors_allow_origins=["*"]))


def test_zero_trusted_proxy_rejected() -> None:
    with pytest.raises(ConfigError, match="trusted_proxy_count"):
        validate_runtime(_prod(trusted_proxy_count=0))


def test_missing_oss_access_key_rejected() -> None:
    # Upload endpoints are always mounted; absent storage credentials only surface
    # as a first-upload 500, so demand them at startup.
    with pytest.raises(ConfigError, match="OSS_ACCESS_KEY"):
        validate_runtime(_prod(oss_access_key=None))


def test_missing_oss_secret_key_rejected() -> None:
    with pytest.raises(ConfigError, match="OSS_SECRET_KEY"):
        validate_runtime(_prod(oss_secret_key=None))


def test_blank_oss_credential_rejected() -> None:
    # An empty-string secret is as useless as an absent one — get_object_storage
    # treats both as "not configured" and raises, so the check must too.
    from pydantic import SecretStr

    with pytest.raises(ConfigError, match="OSS_ACCESS_KEY"):
        validate_runtime(_prod(oss_access_key=SecretStr("")))


def test_all_problems_collected_in_one_raise() -> None:
    # Every check fails at once → the message names all of them (single-pass fix).
    with pytest.raises(ConfigError) as exc:
        validate_runtime(
            _prod(
                platform_jwt_private_key=None,
                debug=True,
                cors_allow_origins=["*"],
                trusted_proxy_count=0,
                oss_access_key=None,
                oss_secret_key=None,
            )
        )
    msg = str(exc.value)
    assert "JWT_PRIVATE_KEY" in msg
    assert "debug" in msg
    assert "cors" in msg
    assert "trusted_proxy_count" in msg
    assert "OSS_ACCESS_KEY" in msg
    assert "OSS_SECRET_KEY" in msg
