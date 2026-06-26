"""Idempotent bulk insert primitive (shared by the durable batch writers).

The usage and gateway-log flushers claim Redis events, then bulk-insert them
into Postgres. Under concurrency (multiple ARQ workers + stale-claim recovery
re-enqueueing), the *same* ``request_id`` can be flushed twice. A non-atomic
"SELECT existing then INSERT new" check cannot prevent that race — two workers
both SELECT-miss, both INSERT, and the row is double-written (double-billed).

The durable guard is a DB unique index on ``request_id`` plus
``INSERT ... ON CONFLICT DO NOTHING``: the second writer's conflicting rows are
silently dropped by the database, so a duplicate flush is a safe no-op instead
of a crash or a double insert. This module dispatches the dialect-specific
``ON CONFLICT`` construct (Postgres in production, SQLite under test).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession


async def insert_ignore_conflicts(
    session: AsyncSession,
    model: type[Any],
    rows: list[dict[str, Any]],
    *,
    index_elements: list[str],
) -> int:
    """Bulk-insert ``rows``, skipping any that collide on ``index_elements``.

    Returns the number of rows *actually* inserted (conflicting rows are
    dropped via ``ON CONFLICT DO NOTHING`` and not counted). Rows whose
    conflict-target columns are ``NULL`` never collide (NULLs are distinct in a
    unique index) and are always inserted. Python-side column defaults (e.g. the
    snowflake primary key) fire per row because rows are passed as executemany
    parameters.
    """
    if not rows:
        return 0

    bind = session.get_bind()
    dialect = bind.dialect.name
    primary_key = model.__mapper__.primary_key[0]

    stmt: Any
    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        stmt = (
            pg_insert(model)
            .on_conflict_do_nothing(index_elements=index_elements)
            .returning(primary_key)
        )
    elif dialect == "sqlite":
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        stmt = (
            sqlite_insert(model)
            .on_conflict_do_nothing(index_elements=index_elements)
            .returning(primary_key)
        )
    else:  # pragma: no cover - only PG (prod) and SQLite (tests) are supported
        raise RuntimeError(f"insert_ignore_conflicts: unsupported dialect {dialect!r}")

    result = await session.execute(stmt, rows)
    return len(result.fetchall())
