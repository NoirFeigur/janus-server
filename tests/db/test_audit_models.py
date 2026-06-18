"""Audit log model invariants and SQLite persistence checks."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from src.db.base import Base
from src.db.models.audit import LoginLog, OperLog
from src.enums import AuditOutcome


def test_oper_log_is_append_only_logentity() -> None:
    """Audit logs inherit LogEntity: snowflake id + created_at, no soft-delete."""
    for model in (OperLog, LoginLog):
        columns = model.__table__.columns
        names = set(columns.keys())
        assert {"id", "created_at"} <= names
        assert columns["id"].primary_key is True
        assert columns["id"].autoincrement is False
        assert columns["id"].default is not None
        assert not hasattr(model, "is_deleted")
        assert "is_deleted" not in names
        assert "created_by" not in names
        assert "updated_at" not in names


async def test_oper_log_persists_jsonb_before_after(
    sqlite_engine: AsyncEngine,
    sqlite_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """JSONB audit snapshots round-trip as dictionaries on the SQLite test engine."""
    async with sqlite_engine.begin() as conn:
        await conn.run_sync(
            lambda sync_conn: Base.metadata.create_all(
                sync_conn,
                tables=[OperLog.__table__],
            )
        )

    before: dict[str, Any] = {"name": "old", "roles": ["viewer"]}
    after: dict[str, Any] = {"name": "new", "roles": ["viewer", "admin"]}
    async with sqlite_session_factory() as session:
        row = OperLog(
            actor_id=1000,
            actor_name="admin",
            module="user",
            action="update",
            method="PUT",
            path="/admin/users/42",
            target_id="42",
            request_ip="2001:db8::1",
            user_agent="pytest",
            trace_id="trace-jsonb",
            before_value=before,
            after_value=after,
            status=AuditOutcome.success.value,
            latency_ms=18,
        )
        session.add(row)
        await session.commit()
        row_id = row.id

    async with sqlite_session_factory() as session:
        fetched = await session.scalar(select(OperLog).where(OperLog.id == row_id))

    assert fetched is not None
    assert fetched.before_value == before
    assert fetched.after_value == after
