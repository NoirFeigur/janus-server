from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.ext.compiler import compiles
from cryptography.fernet import Fernet
from pydantic import SecretStr

from src.core.channel_crypto import encrypt_channel_key
from src.config import get_settings
from src.db.base import Base
from src.db.models.grant import UserModelGrant
from src.db.models.model_catalog import (
    ChannelKey,
    LogicalModel,
    ModelDeployment,
    UpstreamChannel,
)
from src.db.models.quota import Quota
from src.db.models.usage import UsageRecord
from src.enums import (
    ActiveStatus,
    ChannelKeyStatus,
    ChannelStatus,
    GrantScope,
)


@compiles(JSONB, "sqlite")
def _compile_jsonb_sqlite(*_: object, **__: object) -> str:
    return "JSON"


_TABLES = [
    Base.metadata.tables[model.__tablename__]
    for model in (
        LogicalModel,
        UpstreamChannel,
        ChannelKey,
        ModelDeployment,
        UserModelGrant,
        Quota,
        UsageRecord,
    )
]


@pytest.fixture(autouse=True)
def _set_fernet_key(monkeypatch: pytest.MonkeyPatch) -> None:
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("JANUS_CHANNEL_ENCRYPTION_KEYS", key)
    monkeypatch.setattr(get_settings(), "channel_encryption_keys", SecretStr(key))
    from src.core import channel_crypto

    channel_crypto._cipher.cache_clear()


@pytest_asyncio.fixture
async def gateway_session(
    sqlite_engine: AsyncEngine,
    sqlite_session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    async with sqlite_engine.begin() as conn:
        await conn.run_sync(
            lambda sync_conn: Base.metadata.create_all(sync_conn, tables=_TABLES)
        )
    session = sqlite_session_factory()
    try:
        yield session
    finally:
        await session.close()


@pytest_asyncio.fixture
async def seed_logical_model(
    gateway_session: AsyncSession,
) -> Callable[..., Awaitable[LogicalModel]]:
    async def _seed(
        *,
        name: str = "claude-sonnet",
        status: str = ActiveStatus.active.value,
        price_input: Decimal | None = Decimal("3.0"),
        price_output: Decimal | None = Decimal("15.0"),
    ) -> LogicalModel:
        model = LogicalModel(
            name=name,
            display_name=name,
            status=status,
            price_input=price_input,
            price_output=price_output,
        )
        gateway_session.add(model)
        await gateway_session.flush()
        return model

    return _seed


@pytest_asyncio.fixture
async def seed_channel(
    gateway_session: AsyncSession,
) -> Callable[..., Awaitable[tuple[UpstreamChannel, list[ChannelKey]]]]:
    async def _seed(
        *,
        name: str = "anthropic-official",
        provider: str = "anthropic",
        protocol: str = "anthropic",
        api_base: str | None = "https://api.example.test",
        channel_status: str = ChannelStatus.active.value,
        key_count: int = 1,
        key_status: str = ChannelKeyStatus.active.value,
    ) -> tuple[UpstreamChannel, list[ChannelKey]]:
        channel = UpstreamChannel(
            name=name,
            provider=provider,
            protocol=protocol,
            api_base=api_base,
            status=channel_status,
        )
        gateway_session.add(channel)
        await gateway_session.flush()
        keys = [
            ChannelKey(
                channel_id=channel.id,
                alias=f"{name}-key-{index}",
                api_key_encrypted=encrypt_channel_key(f"sk-test-{index}"),
                key_hint=f"...{index}",
                status=key_status,
                weight=index + 1,
                priority=index,
                rpm_limit=100 + index,
                tpm_limit=1000 + index,
            )
            for index in range(key_count)
        ]
        gateway_session.add_all(keys)
        await gateway_session.flush()
        return channel, keys

    return _seed


@pytest_asyncio.fixture
async def seed_deployment(
    gateway_session: AsyncSession,
) -> Callable[..., Awaitable[ModelDeployment]]:
    async def _seed(
        *,
        model: LogicalModel,
        channel: UpstreamChannel,
        upstream_model: str = "claude-3-5-sonnet",
        status: str = ActiveStatus.active.value,
        is_deleted: bool = False,
    ) -> ModelDeployment:
        deployment = ModelDeployment(
            logical_model_id=model.id,
            channel_id=channel.id,
            upstream_model=upstream_model,
            status=status,
            is_deleted=is_deleted,
            weight=7,
            priority=2,
        )
        gateway_session.add(deployment)
        await gateway_session.flush()
        return deployment

    return _seed


@pytest_asyncio.fixture
async def seed_grant(
    gateway_session: AsyncSession,
) -> Callable[..., Awaitable[UserModelGrant]]:
    async def _seed(
        *,
        logical_model_id: int,
        scope: str = GrantScope.user.value,
        scope_id: int = 100,
        is_default: bool = False,
        is_deleted: bool = False,
    ) -> UserModelGrant:
        grant = UserModelGrant(
            scope=scope,
            scope_id=scope_id,
            logical_model_id=logical_model_id,
            is_default=is_default,
            is_deleted=is_deleted,
        )
        gateway_session.add(grant)
        await gateway_session.flush()
        return grant

    return _seed
