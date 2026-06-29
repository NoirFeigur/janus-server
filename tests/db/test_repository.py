"""Tests for BaseRepository CRUD + soft-delete semantics (src/db/repository.py).

Uses a SQLite-compatible throwaway model built on ``BaseEntity`` so the generic
repository behavior is exercised without the PG-specific production tables
(JSONB columns, partial indexes).
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest_asyncio
from sqlalchemy import String
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import Mapped, mapped_column

from src.db.base import BaseEntity
from src.db.repository import BaseRepository


class SampleEntity(BaseEntity):
    """SQLite-friendly entity for exercising BaseRepository."""

    __tablename__ = "sample_entity"

    name: Mapped[str] = mapped_column(String(64))


class SampleRepository(BaseRepository[SampleEntity]):
    model = SampleEntity


@pytest_asyncio.fixture
async def session(
    sqlite_engine: AsyncEngine,
    sqlite_session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """Create only the sample table, yield a session."""
    table = BaseEntity.metadata.tables[SampleEntity.__tablename__]
    async with sqlite_engine.begin() as conn:
        await conn.run_sync(
            lambda sync_conn: BaseEntity.metadata.create_all(sync_conn, tables=[table])
        )
    async with sqlite_session_factory() as s:
        yield s


async def test_create_assigns_snowflake_id(session: AsyncSession) -> None:
    repo = SampleRepository(session)
    obj = await repo.create(SampleEntity(name="alice"))
    assert obj.id is not None
    assert obj.id > 0  # snowflake default fired on flush


async def test_get_returns_created_row(session: AsyncSession) -> None:
    repo = SampleRepository(session)
    created = await repo.create(SampleEntity(name="bob"))
    fetched = await repo.get(created.id)
    assert fetched is not None
    assert fetched.name == "bob"


async def test_get_missing_returns_none(session: AsyncSession) -> None:
    repo = SampleRepository(session)
    assert await repo.get(999_999) is None


async def test_update_mutates_fields(session: AsyncSession) -> None:
    repo = SampleRepository(session)
    obj = await repo.create(SampleEntity(name="carol"))
    await repo.update(obj, name="carol-renamed")
    refetched = await repo.get(obj.id)
    assert refetched is not None
    assert refetched.name == "carol-renamed"


async def test_soft_delete_excluded_from_get(session: AsyncSession) -> None:
    repo = SampleRepository(session)
    obj = await repo.create(SampleEntity(name="dave"))
    await repo.soft_delete(obj)
    assert obj.is_deleted is True
    # default get excludes soft-deleted...
    assert await repo.get(obj.id) is None
    # ...but include_deleted surfaces it
    revived = await repo.get(obj.id, include_deleted=True)
    assert revived is not None
    assert revived.is_deleted is True


async def test_list_excludes_soft_deleted_by_default(session: AsyncSession) -> None:
    repo = SampleRepository(session)
    keep = await repo.create(SampleEntity(name="keep"))
    drop = await repo.create(SampleEntity(name="drop"))
    await repo.soft_delete(drop)

    active = await repo.list()
    active_ids = {row.id for row in active}
    assert keep.id in active_ids
    assert drop.id not in active_ids

    everything = await repo.list(include_deleted=True)
    assert {keep.id, drop.id} <= {row.id for row in everything}


async def test_list_limit_and_offset(session: AsyncSession) -> None:
    repo = SampleRepository(session)
    for i in range(5):
        await repo.create(SampleEntity(name=f"row-{i}"))
    page = await repo.list(limit=2, offset=1)
    assert len(page) == 2


async def test_soft_delete_many_empty_ids_noop(session: AsyncSession) -> None:
    repo = SampleRepository(session)
    row = await repo.create(SampleEntity(name="keep"))

    affected, skipped_ids = await repo.soft_delete_many([])

    assert affected == 0
    assert skipped_ids == []
    kept = await repo.get(row.id)
    assert kept is not None
    assert kept.is_deleted is False


async def test_soft_delete_many_unrestricted_deletes_all(session: AsyncSession) -> None:
    repo = SampleRepository(session)
    first = await repo.create(SampleEntity(name="first"))
    second = await repo.create(SampleEntity(name="second"))

    affected, skipped_ids = await repo.soft_delete_many([first.id, second.id])

    assert affected == 2
    assert skipped_ids == []

    deleted_rows = await repo.list(include_deleted=True)
    assert {row.id for row in deleted_rows} == {first.id, second.id}
    assert all(row.is_deleted is True for row in deleted_rows)


async def test_soft_delete_many_already_deleted_counted_as_skipped(
    session: AsyncSession,
) -> None:
    repo = SampleRepository(session)
    already_deleted = await repo.create(SampleEntity(name="already-deleted"))
    active = await repo.create(SampleEntity(name="active"))
    await repo.soft_delete(already_deleted)

    affected, skipped_ids = await repo.soft_delete_many([already_deleted.id, active.id])

    assert affected == 1
    assert skipped_ids == [already_deleted.id]

    rows = await repo.list(include_deleted=True)
    assert {row.id for row in rows} == {already_deleted.id, active.id}
    assert all(row.is_deleted is True for row in rows)
