"""Regression tests for the request/unit-of-work boundary (src/db/session.py).

The UoW refactor moved the single ``commit()`` out of every service and onto
the request edge. These tests lock the boundary's contract:

- a clean unit of work commits; an exception rolls the whole thing back (atomic);
- after-commit hooks fire **only after** the commit lands, and are **dropped**
  on rollback (so a phantom cache-invalidation / revocation never publishes for
  a write that did not happen);
- :func:`commit_session` rolls back and drops queued hooks if the commit itself
  fails, then re-raises;
- an **independent** unit of work (a distinct factory/engine, as the login-log
  audit write uses) commits on its own and **survives** the outer request's
  rollback — the security-trail-survives-a-failed-request guarantee.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import String, func, select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import Mapped, mapped_column

from src.db.base import BaseEntity
from src.db.session import (
    add_after_commit_hook,
    commit_session,
    unit_of_work,
)

pytestmark = pytest.mark.asyncio


class UoWSample(BaseEntity):
    """SQLite-friendly entity for exercising the unit-of-work boundary."""

    __tablename__ = "uow_sample"

    name: Mapped[str] = mapped_column(String(64))


async def _count(session: AsyncSession) -> int:
    result = await session.execute(select(func.count()).select_from(UoWSample))
    return int(result.scalar_one())


@pytest_asyncio.fixture
async def uow_factory(
    sqlite_engine: AsyncEngine,
    sqlite_session_factory: async_sessionmaker[AsyncSession],
) -> async_sessionmaker[AsyncSession]:
    """Create the sample table on the in-memory engine, return its factory."""
    table = BaseEntity.metadata.tables[UoWSample.__tablename__]
    async with sqlite_engine.begin() as conn:
        await conn.run_sync(
            lambda sync_conn: BaseEntity.metadata.create_all(sync_conn, tables=[table])
        )
    return sqlite_session_factory


async def test_unit_of_work_commits_on_clean_exit(
    uow_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A clean exit commits — the write is visible to a later session."""
    async with unit_of_work(uow_factory) as session:
        session.add(UoWSample(name="kept"))

    async with uow_factory() as verify:
        assert await _count(verify) == 1


async def test_unit_of_work_rolls_back_on_exception(
    uow_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Any exception rolls the whole unit of work back — nothing persists."""
    with pytest.raises(RuntimeError):
        async with unit_of_work(uow_factory) as session:
            session.add(UoWSample(name="doomed"))
            await session.flush()  # row exists in the transaction…
            raise RuntimeError("boom")  # …but the boundary must roll it back

    async with uow_factory() as verify:
        assert await _count(verify) == 0


async def test_after_commit_hook_fires_only_after_commit(
    uow_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The hook runs after the commit lands — it observes the committed row.

    The hook opens its own session and counts: seeing the row proves the commit
    already happened by the time the hook fired (ordering, not just "it ran").
    """
    observed: list[int] = []

    async def _hook() -> None:
        async with uow_factory() as hook_session:
            observed.append(await _count(hook_session))

    async with unit_of_work(uow_factory) as session:
        session.add(UoWSample(name="committed"))
        add_after_commit_hook(session, _hook)
        assert observed == []  # hook has NOT fired mid-transaction

    assert observed == [1]  # fired once, after commit, seeing the committed row


async def test_after_commit_hook_dropped_on_rollback(
    uow_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A rolled-back unit of work never fires its queued hooks."""
    fired: list[str] = []

    async def _hook() -> None:
        fired.append("fired")

    with pytest.raises(RuntimeError):
        async with unit_of_work(uow_factory) as session:
            session.add(UoWSample(name="doomed"))
            add_after_commit_hook(session, _hook)
            raise RuntimeError("boom")

    assert fired == []  # hook dropped — no phantom side effect
    async with uow_factory() as verify:
        assert await _count(verify) == 0


async def test_commit_session_fires_hooks_after_commit(
    uow_factory: async_sessionmaker[AsyncSession],
) -> None:
    """``commit_session`` commits then fires hooks in registration order."""
    order: list[str] = []

    async def _first() -> None:
        order.append("first")

    async def _second() -> None:
        order.append("second")

    async with uow_factory() as session:
        session.add(UoWSample(name="row"))
        add_after_commit_hook(session, _first)
        add_after_commit_hook(session, _second)
        await commit_session(session)

    assert order == ["first", "second"]
    async with uow_factory() as verify:
        assert await _count(verify) == 1


async def test_commit_session_drops_hooks_when_commit_fails(
    uow_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the commit itself fails, hooks are dropped and the error propagates.

    ``session.commit`` is patched to raise (a real DB-level commit failure is
    awkward to force on SQLite, which checks most constraints at statement time,
    not at commit). The contract under test is :func:`commit_session`'s: on a
    failed commit it rolls back, drops the queued hooks (the write never landed),
    and re-raises — fail-closed, the hook's side effect never fires.
    """
    fired: list[str] = []

    async def _hook() -> None:
        fired.append("fired")

    async with uow_factory() as session:
        session.add(UoWSample(name="row"))
        add_after_commit_hook(session, _hook)

        async def _boom() -> None:
            raise RuntimeError("commit failed")

        monkeypatch.setattr(session, "commit", _boom)
        with pytest.raises(RuntimeError, match="commit failed"):
            await commit_session(session)

    assert fired == []  # commit failed → hook dropped, side effect never fired
    async with uow_factory() as verify:
        assert await _count(verify) == 0  # the doomed write did not land


async def test_independent_uow_survives_outer_rollback(tmp_path: Path) -> None:
    """An independent unit of work (distinct engine) survives an outer rollback.

    Mirrors the login-log audit write on a *failed* login: the audit row is
    written through a separate engine / connection and commits on its own; the
    request's own unit of work then raises and rolls back. The audit row must
    persist (the security trail survives a failed request). A file-backed SQLite
    DB is used (not ``:memory:``) so the two engines hold genuinely independent
    connections; the audit UoW commits and releases its write lock *before* the
    request writes, faithful to the real path (read user → log attempt → raise,
    with no pending request write at the moment the audit fires) and avoiding
    SQLite's single-writer file lock that PostgreSQL would not impose.
    """
    db_path = tmp_path / "uow_isolation.sqlite"
    url = f"sqlite+aiosqlite:///{db_path.as_posix()}"
    outer_engine = create_async_engine(url)
    audit_engine = create_async_engine(url)
    outer_factory = async_sessionmaker(
        bind=outer_engine, expire_on_commit=False, autoflush=False
    )
    audit_factory = async_sessionmaker(
        bind=audit_engine, expire_on_commit=False, autoflush=False
    )
    try:
        table = BaseEntity.metadata.tables[UoWSample.__tablename__]
        async with outer_engine.begin() as conn:
            await conn.run_sync(
                lambda sync_conn: BaseEntity.metadata.create_all(
                    sync_conn, tables=[table]
                )
            )

        with pytest.raises(RuntimeError):
            async with unit_of_work(outer_factory) as request_session:
                # The audit write happens in its OWN unit of work (separate
                # connection) and commits + releases independently — exactly as
                # ``AuthService._record_login`` does for the security trail,
                # before the request flushes any of its own writes.
                async with unit_of_work(audit_factory) as audit_session:
                    audit_session.add(UoWSample(name="audit-write"))
                # Now the request does its work and fails: its write rolls back.
                request_session.add(UoWSample(name="request-write"))
                await request_session.flush()
                raise RuntimeError("request failed after audit logged")

        async with audit_factory() as verify:
            names = (
                await verify.execute(select(UoWSample.name).order_by(UoWSample.name))
            ).scalars().all()
        # The audit row survived; the request row was rolled back.
        assert names == ["audit-write"]
    finally:
        await outer_engine.dispose()
        await audit_engine.dispose()
