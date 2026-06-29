"""Postgres migration smoke test (README: 迁移 — Alembic upgrade/downgrade 可逆).

The unit suite runs on in-memory SQLite, which **cannot** exercise the
PG-specific parts of our migrations (JSONB columns, ``postgresql_where`` partial
unique indexes, ``NULLS NOT DISTINCT``). Those run zero-coverage on SQLite, so a
migration that is green in CI could still explode on a real Postgres.

This test closes that gap: when a disposable Postgres URL is provided via
``JANUS_TEST_PG_URL`` it drives the real Alembic stack ``base → head → base →
head`` against it, proving the migrations apply, fully reverse, and re-apply on
the production dialect. With no URL set it **skips** (so the default CI run on a
machine without Postgres stays green) — point it at a throwaway database (a
testcontainer, a CI service container, or a local ephemeral PG) to activate it.

NEVER point ``JANUS_TEST_PG_URL`` at a shared/real database: this drops every
table (``downgrade base``).
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from alembic import command

_PG_URL = os.environ.get("JANUS_TEST_PG_URL")

pytestmark = pytest.mark.skipif(
    not _PG_URL,
    reason="JANUS_TEST_PG_URL not set (provide a DISPOSABLE Postgres to run "
    "the migration smoke test; it drops all tables)",
)


def _sync_url(url: str) -> str:
    """Normalise an async PG URL to the sync psycopg2 driver for the inspector.

    The Alembic env runs async (asyncpg); the post-migration assertions use a
    plain sync engine, so strip any ``+asyncpg`` to let SQLAlchemy pick the
    default sync driver.
    """
    return url.replace("+asyncpg", "")


@pytest.fixture
def alembic_config(monkeypatch: pytest.MonkeyPatch) -> Iterator[Config]:
    """Alembic Config wired to the test PG, with a clean slate before/after.

    The env reads the URL from ``get_settings().database_url``; we point that at
    the test PG via the env var and clear the settings cache so the override
    takes effect. Tables are dropped (``downgrade base``) on entry AND exit so a
    half-migrated leftover from a prior crashed run never poisons the test.
    """
    assert _PG_URL is not None
    monkeypatch.setenv("JANUS_DATABASE_URL", _PG_URL)

    import src.config

    src.config.get_settings.cache_clear()

    cfg = Config("alembic.ini")
    cfg.set_main_option("script_location", "alembic")

    command.downgrade(cfg, "base")  # clean slate
    try:
        yield cfg
    finally:
        command.downgrade(cfg, "base")
        src.config.get_settings.cache_clear()


def test_migrations_upgrade_downgrade_reupgrade_are_reversible(
    alembic_config: Config,
) -> None:
    """base → head → base → head must round-trip cleanly on real Postgres."""
    assert _PG_URL is not None
    sync_engine = create_engine(_sync_url(_PG_URL))

    # 1) Full upgrade to head.
    command.upgrade(alembic_config, "head")
    with sync_engine.connect() as conn:
        tables_after_upgrade = set(inspect(conn).get_table_names())
    # Core tables from the initial schema must exist (sample a few load-bearing
    # ones rather than asserting an exact set — that would be a change-detector).
    assert {"users", "role", "channel_key", "upstream_channel"} <= (
        tables_after_upgrade
    )
    assert "alembic_version" in tables_after_upgrade

    # 2) Full downgrade to base — every domain table must be gone (fully reversible).
    command.downgrade(alembic_config, "base")
    with sync_engine.connect() as conn:
        tables_after_downgrade = set(inspect(conn).get_table_names())
    assert "users" not in tables_after_downgrade
    assert "channel_key" not in tables_after_downgrade

    # 3) Re-upgrade — proves downgrade left no residue that blocks a clean re-apply.
    command.upgrade(alembic_config, "head")
    with sync_engine.connect() as conn:
        tables_after_reupgrade = set(inspect(conn).get_table_names())
    assert {"users", "channel_key"} <= tables_after_reupgrade

    sync_engine.dispose()


def test_pg_specific_constraints_materialize(alembic_config: Config) -> None:
    """The PG-only DDL SQLite can't model must actually exist after upgrade:
    a JSONB column and a partial unique index (postgresql_where)."""
    assert _PG_URL is not None
    command.upgrade(alembic_config, "head")
    sync_engine = create_engine(_sync_url(_PG_URL))

    with sync_engine.connect() as conn:
        # JSONB column on upstream_channel.extra_config (data-model §2.1).
        jsonb_type = conn.execute(
            text(
                "SELECT data_type FROM information_schema.columns "
                "WHERE table_name = 'upstream_channel' "
                "AND column_name = 'extra_config'"
            )
        ).scalar()
        assert jsonb_type == "jsonb"

        # Partial unique index (postgresql_where = is_deleted false) on channel name.
        partial_index = conn.execute(
            text(
                "SELECT indexdef FROM pg_indexes "
                "WHERE indexname = 'uq_channel_name_active'"
            )
        ).scalar()
        assert partial_index is not None
        assert "is_deleted" in partial_index.lower()

    sync_engine.dispose()
