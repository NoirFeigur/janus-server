"""Direct unit tests for CatalogService (service layer, no HTTP)."""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet
from pydantic import SecretStr, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from src.admin.catalog.schemas import (
    ChannelKeyCreate,
    LogicalModelCreate,
    LogicalModelUpdate,
    ModelDeploymentCreate,
    ModelDeploymentUpdate,
    UpstreamChannelCreate,
)
from src.admin.catalog.service import CatalogService
from src.auth.constants import SUPERADMIN_ROLE_CODE
from src.auth.service import AuthenticatedUser
from src.config import get_settings
from src.core.channel_crypto import _cipher
from src.core.query import ListQuery
from src.exceptions import AppError

pytestmark = pytest.mark.asyncio

TEST_FERNET_KEY = Fernet.generate_key().decode()


@pytest.fixture(autouse=True)
def _set_fernet_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set a test encryption key with auto-restore teardown.

    ``monkeypatch`` reverts both the env var and the cached settings attribute
    after each test, so this module no longer leaks ``JANUS_CHANNEL_ENCRYPTION_KEYS``
    into the rest of the suite (which previously broke the real-DB lifespan test).
    """
    monkeypatch.setenv("JANUS_CHANNEL_ENCRYPTION_KEYS", TEST_FERNET_KEY)
    monkeypatch.setattr(
        get_settings(), "channel_encryption_keys", SecretStr(TEST_FERNET_KEY)
    )
    _cipher.cache_clear()

ACTOR = AuthenticatedUser(
    user_id=1000,
    username="admin",
    department_id=10,
    permissions=frozenset({"*:*:*"}),
    role_codes=frozenset({SUPERADMIN_ROLE_CODE}),
)

# Holds a non-wildcard catalog perm (read-only) but NOT ``ai:catalog:*`` and is
# not superadmin, so it fails the catalog-wildcard gate on every write. Used to
# prove model/deployment writes require ``ai:catalog:*``.
NO_WILDCARD_ACTOR = AuthenticatedUser(
    user_id=2000,
    username="readonly",
    department_id=20,
    permissions=frozenset({"ai:catalog:list"}),
    role_codes=frozenset(),
)


def _channel_payload(**overrides: object) -> UpstreamChannelCreate:
    defaults: dict[str, object] = {
        "name": "anthropic-official",
        "provider": "anthropic",
        "protocol": "anthropic",
        "api_base": "https://api.anthropic.com",
    }
    defaults.update(overrides)
    return UpstreamChannelCreate(**defaults)


def _model_payload(**overrides: object) -> LogicalModelCreate:
    defaults: dict[str, object] = {
        "name": "claude-sonnet",
        "display_name": "Claude Sonnet",
        "category": "code",
        "context_length": 200000,
    }
    defaults.update(overrides)
    return LogicalModelCreate(**defaults)


async def test_create_channel_and_get_channel(admin_session: AsyncSession) -> None:
    svc = CatalogService(admin_session)
    channel = await svc.create_channel(_channel_payload(), actor=ACTOR)

    fetched = await svc.get_channel(channel.id)

    assert fetched.id == channel.id
    assert fetched.name == "anthropic-official"


async def test_create_channel_duplicate_name_rejected(
    admin_session: AsyncSession,
) -> None:
    svc = CatalogService(admin_session)
    await svc.create_channel(_channel_payload(), actor=ACTOR)

    with pytest.raises(AppError) as exc:
        await svc.create_channel(_channel_payload(), actor=ACTOR)

    assert exc.value.status_code == 400


async def test_create_key_encrypts_plaintext(admin_session: AsyncSession) -> None:
    svc = CatalogService(admin_session)
    channel = await svc.create_channel(_channel_payload(), actor=ACTOR)
    plaintext = "sk-upstream-secret"

    key = await svc.create_key(
        ChannelKeyCreate(
            channel_id=channel.id,
            alias="primary",
            api_key=plaintext,
        ),
        actor=ACTOR,
    )

    assert key.key_hint
    assert key.key_hint.endswith(plaintext[-4:])
    assert key.api_key_encrypted != plaintext


async def test_list_channels_pagination(admin_session: AsyncSession) -> None:
    svc = CatalogService(admin_session)
    await svc.create_channel(_channel_payload(name="a-channel"), actor=ACTOR)
    await svc.create_channel(_channel_payload(name="b-channel"), actor=ACTOR)
    await svc.create_channel(_channel_payload(name="c-channel"), actor=ACTOR)

    result = await svc.list_channels(query=ListQuery(limit=2, offset=1))

    assert result.total == 3
    assert len(result.items) == 2
    assert result.items[0].name == "b-channel"


async def test_create_model_duplicate_name_rejected(
    admin_session: AsyncSession,
) -> None:
    svc = CatalogService(admin_session)
    model = await svc.create_model(_model_payload(), actor=ACTOR)
    assert model.name == "claude-sonnet"

    with pytest.raises(AppError) as exc:
        await svc.create_model(_model_payload(display_name="Duplicate"), actor=ACTOR)

    assert exc.value.status_code == 400


async def test_logical_model_status_rejects_invalid_values() -> None:
    with pytest.raises(ValidationError):
        LogicalModelCreate(
            name="bad-model",
            display_name="Bad Model",
            status="paused",
        )

    with pytest.raises(ValidationError):
        LogicalModelUpdate(status="paused")


async def test_create_deployment_requires_active_model_and_channel(
    admin_session: AsyncSession,
) -> None:
    svc = CatalogService(admin_session)
    channel = await svc.create_channel(_channel_payload(), actor=ACTOR)
    model = await svc.create_model(_model_payload(), actor=ACTOR)

    deployment = await svc.create_deployment(
        ModelDeploymentCreate(
            logical_model_id=model.id,
            channel_id=channel.id,
            upstream_model="claude-3-5-sonnet",
        ),
        actor=ACTOR,
    )

    assert deployment.logical_model_id == model.id
    assert deployment.channel_id == channel.id


async def test_create_deployment_duplicate_model_channel_rejected(
    admin_session: AsyncSession,
) -> None:
    svc = CatalogService(admin_session)
    channel = await svc.create_channel(_channel_payload(), actor=ACTOR)
    model = await svc.create_model(_model_payload(), actor=ACTOR)
    payload = ModelDeploymentCreate(
        logical_model_id=model.id,
        channel_id=channel.id,
        upstream_model="claude-3-5-sonnet",
    )
    await svc.create_deployment(payload, actor=ACTOR)

    with pytest.raises(AppError) as exc:
        await svc.create_deployment(payload, actor=ACTOR)

    assert exc.value.status_code == 400


async def test_delete_channel_soft_deletes(admin_session: AsyncSession) -> None:
    svc = CatalogService(admin_session)
    channel = await svc.create_channel(_channel_payload(), actor=ACTOR)

    await svc.delete_channel(channel.id, actor=ACTOR)

    with pytest.raises(AppError) as exc:
        await svc.get_channel(channel.id)

    assert exc.value.status_code == 404


async def test_create_model_without_wildcard_forbidden(
    admin_session: AsyncSession,
) -> None:
    svc = CatalogService(admin_session)

    with pytest.raises(AppError) as exc:
        await svc.create_model(_model_payload(), actor=NO_WILDCARD_ACTOR)

    assert exc.value.status_code == 403


async def test_update_model_without_wildcard_forbidden(
    admin_session: AsyncSession,
) -> None:
    svc = CatalogService(admin_session)
    model = await svc.create_model(_model_payload(), actor=ACTOR)

    with pytest.raises(AppError) as exc:
        await svc.update_model(
            model.id, LogicalModelUpdate(display_name="x"), actor=NO_WILDCARD_ACTOR
        )

    assert exc.value.status_code == 403


async def test_delete_model_without_wildcard_forbidden(
    admin_session: AsyncSession,
) -> None:
    svc = CatalogService(admin_session)
    model = await svc.create_model(_model_payload(), actor=ACTOR)

    with pytest.raises(AppError) as exc:
        await svc.delete_model(model.id, actor=NO_WILDCARD_ACTOR)

    assert exc.value.status_code == 403


async def test_create_deployment_without_wildcard_forbidden(
    admin_session: AsyncSession,
) -> None:
    svc = CatalogService(admin_session)
    channel = await svc.create_channel(_channel_payload(), actor=ACTOR)
    model = await svc.create_model(_model_payload(), actor=ACTOR)

    with pytest.raises(AppError) as exc:
        await svc.create_deployment(
            ModelDeploymentCreate(
                logical_model_id=model.id,
                channel_id=channel.id,
                upstream_model="claude-3-5-sonnet",
            ),
            actor=NO_WILDCARD_ACTOR,
        )

    assert exc.value.status_code == 403


async def test_update_deployment_without_wildcard_forbidden(
    admin_session: AsyncSession,
) -> None:
    svc = CatalogService(admin_session)
    channel = await svc.create_channel(_channel_payload(), actor=ACTOR)
    model = await svc.create_model(_model_payload(), actor=ACTOR)
    deployment = await svc.create_deployment(
        ModelDeploymentCreate(
            logical_model_id=model.id,
            channel_id=channel.id,
            upstream_model="claude-3-5-sonnet",
        ),
        actor=ACTOR,
    )

    with pytest.raises(AppError) as exc:
        await svc.update_deployment(
            deployment.id,
            ModelDeploymentUpdate(weight=5),
            actor=NO_WILDCARD_ACTOR,
        )

    assert exc.value.status_code == 403


async def test_delete_deployment_without_wildcard_forbidden(
    admin_session: AsyncSession,
) -> None:
    svc = CatalogService(admin_session)
    channel = await svc.create_channel(_channel_payload(), actor=ACTOR)
    model = await svc.create_model(_model_payload(), actor=ACTOR)
    deployment = await svc.create_deployment(
        ModelDeploymentCreate(
            logical_model_id=model.id,
            channel_id=channel.id,
            upstream_model="claude-3-5-sonnet",
        ),
        actor=ACTOR,
    )

    with pytest.raises(AppError) as exc:
        await svc.delete_deployment(deployment.id, actor=NO_WILDCARD_ACTOR)

    assert exc.value.status_code == 403
