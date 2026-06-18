"""Tests for the platform-config runtime accessor (src/core/config_accessor.py).

Two surfaces:
- ``parse_config_value`` — pure type parsing (string/int/bool/json + rejects).
- ``get_str``/``get_int``/``get_bool``/``get_json`` — cache-aside typed reads over
  a seeded ``sys_config`` table, backed by the in-memory SQLite engine + the
  autouse Redis double. The accessor opens its own session via the module-level
  ``async_session_factory``; tests monkeypatch it onto the SQLite factory so no
  shared instance is touched.

Behaviour contracts, not snapshots: present key parses to the typed value, a
missing key returns the caller's default, a value that does not parse under the
requested type degrades to the default, and a miss-then-hit runs the DB loader
exactly once.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from src.core import config_accessor
from src.db.base import Base
from src.db.models.sys_config import SysConfig
from src.enums import ConfigValueType
from tests._async_redis_double import AsyncRedisDouble

# ---- parse_config_value: pure type parsing --------------------------------


def test_parse_string_returns_raw() -> None:
    assert config_accessor.parse_config_value("hello", ConfigValueType.string) == "hello"


def test_parse_int_ok() -> None:
    assert config_accessor.parse_config_value("42", ConfigValueType.int) == 42


def test_parse_int_rejects_non_numeric() -> None:
    with pytest.raises(ValueError):
        config_accessor.parse_config_value("abc", ConfigValueType.int)


@pytest.mark.parametrize("truthy", ["true", "TRUE", "1", "yes", "on"])
def test_parse_bool_truthy(truthy: str) -> None:
    assert config_accessor.parse_config_value(truthy, ConfigValueType.bool) is True


@pytest.mark.parametrize("falsy", ["false", "FALSE", "0", "no", "off"])
def test_parse_bool_falsy(falsy: str) -> None:
    assert config_accessor.parse_config_value(falsy, ConfigValueType.bool) is False


def test_parse_bool_rejects_garbage() -> None:
    with pytest.raises(ValueError):
        config_accessor.parse_config_value("maybe", ConfigValueType.bool)


def test_parse_json_object() -> None:
    parsed = config_accessor.parse_config_value('{"a": 1}', ConfigValueType.json)
    assert parsed == {"a": 1}


def test_parse_json_rejects_invalid() -> None:
    with pytest.raises(ValueError):
        config_accessor.parse_config_value("{not json}", ConfigValueType.json)


# ---- cached typed getters -------------------------------------------------


@pytest_asyncio.fixture
async def seeded_factory(
    sqlite_engine: AsyncEngine,
    sqlite_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Create the sys_config table, patch the accessor's session factory onto it."""
    table = Base.metadata.tables[SysConfig.__tablename__]
    async with sqlite_engine.begin() as conn:
        await conn.run_sync(lambda sync_conn: table.create(sync_conn))
    monkeypatch.setattr(config_accessor, "async_session_factory", sqlite_session_factory)
    yield sqlite_session_factory


async def _seed(
    factory: async_sessionmaker[AsyncSession],
    *,
    key: str,
    value: str,
    value_type: ConfigValueType,
) -> None:
    async with factory() as session:
        session.add(
            SysConfig(
                config_key=key,
                config_value=value,
                value_type=value_type,
                config_name=key,
            )
        )
        await session.commit()


async def test_get_str_present(
    seeded_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed(seeded_factory, key="app.name", value="janus", value_type=ConfigValueType.string)
    assert await config_accessor.get_str("app.name") == "janus"


async def test_get_str_missing_returns_default(
    seeded_factory: async_sessionmaker[AsyncSession],
) -> None:
    assert await config_accessor.get_str("nope", default="fallback") == "fallback"


async def test_get_int_present(
    seeded_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed(seeded_factory, key="auth.max", value="5", value_type=ConfigValueType.int)
    assert await config_accessor.get_int("auth.max") == 5


async def test_get_int_unparseable_returns_default(
    seeded_factory: async_sessionmaker[AsyncSession],
) -> None:
    # Stored as string type but read as int → does not parse → default.
    await _seed(seeded_factory, key="bad.int", value="abc", value_type=ConfigValueType.string)
    assert await config_accessor.get_int("bad.int", default=99) == 99


async def test_get_bool_present(
    seeded_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed(seeded_factory, key="feat.on", value="true", value_type=ConfigValueType.bool)
    assert await config_accessor.get_bool("feat.on") is True


async def test_get_json_present(
    seeded_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed(
        seeded_factory,
        key="cfg.json",
        value='{"k": [1, 2]}',
        value_type=ConfigValueType.json,
    )
    assert await config_accessor.get_json("cfg.json") == {"k": [1, 2]}


async def test_missing_returns_none_default(
    seeded_factory: async_sessionmaker[AsyncSession],
) -> None:
    assert await config_accessor.get_int("absent") is None
    assert await config_accessor.get_json("absent") is None


async def test_value_is_cached_loader_runs_once(
    seeded_factory: async_sessionmaker[AsyncSession],
    fake_redis: AsyncRedisDouble,
) -> None:
    """Second read hits Redis — confirmed by mutating the DB and seeing the stale value."""
    await _seed(seeded_factory, key="cached.key", value="first", value_type=ConfigValueType.string)
    assert await config_accessor.get_str("cached.key") == "first"

    # Mutate the underlying row directly, bypassing invalidate_config().
    async with seeded_factory() as session:
        await session.execute(
            SysConfig.__table__.update()
            .where(SysConfig.config_key == "cached.key")
            .values(config_value="second")
        )
        await session.commit()

    # Within TTL and without invalidation, the cached "first" must still be served.
    assert await config_accessor.get_str("cached.key") == "first"


async def test_invalidate_forces_reload(
    seeded_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed(seeded_factory, key="inv.key", value="old", value_type=ConfigValueType.string)
    assert await config_accessor.get_str("inv.key") == "old"

    # Update the row, then invalidate → next read reflects the new value.
    async with seeded_factory() as session:
        await session.execute(
            SysConfig.__table__.update()
            .where(SysConfig.config_key == "inv.key")
            .values(config_value="new")
        )
        await session.commit()

    await config_accessor.invalidate_config("inv.key")
    assert await config_accessor.get_str("inv.key") == "new"


async def test_missing_key_is_cached(
    seeded_factory: async_sessionmaker[AsyncSession],
    fake_redis: AsyncRedisDouble,
) -> None:
    """A missing key caches the sentinel so repeated reads don't re-hit the DB."""
    assert await config_accessor.get_str("ghost") is None
    # The sentinel is stored under the namespaced cache key.
    raw = await fake_redis.get("sys-config:ghost")
    assert raw is not None  # sentinel cached, not a None gap
