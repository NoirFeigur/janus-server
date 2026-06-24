"""Crash regression for admin observability log listing (M3-7).

``ObservabilityService.list_logs`` called ``resolve_sort`` with the *old*
positional signature (column map passed as ``query``, a column object as
``default``). Against the current ``resolve_sort(query, *, allowed, default)``
this dereferences ``query.sort_by`` on a ``dict`` → crash on every
``GET /observability/logs`` request. This test drives the real query path on the
in-memory engine; it raises against the pre-fix code and passes once the caller
uses the keyword API returning ``(column, is_desc)``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from src.admin.observability.service import ObservabilityService
from src.auth.constants import SUPERADMIN_ROLE_CODE
from src.auth.service import AuthenticatedUser
from src.core.query import ListQuery
from src.db.base import Base
from src.db.models.gateway_observability import GatewayRequestLog
from src.db.models.identity import Role, User, UserRole

pytestmark = pytest.mark.asyncio

_TABLES = [
    Base.metadata.tables[GatewayRequestLog.__tablename__],
    Base.metadata.tables[Role.__tablename__],
    Base.metadata.tables[UserRole.__tablename__],
    Base.metadata.tables[User.__tablename__],
]

# Superadmin → unrestricted scope, no filter (preserves the M3-7 crash tests).
SUPER_ACTOR = AuthenticatedUser(
    user_id=1,
    username="admin",
    department_id=10,
    permissions=frozenset({"*:*:*"}),
    role_codes=frozenset({SUPERADMIN_ROLE_CODE}),
)

# No roles → resolve_data_scope yields self-only (include_self, no dept). Sees
# only its own gateway logs (M3-3).
SELF_ONLY_ACTOR = AuthenticatedUser(
    user_id=2,
    username="restricted",
    department_id=20,
    permissions=frozenset({"ai:observability:list"}),
    role_codes=frozenset(),
)


@pytest_asyncio.fixture
async def db_session(
    sqlite_engine: AsyncEngine,
    sqlite_session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    async with sqlite_engine.begin() as conn:
        await conn.run_sync(
            lambda sync_conn: Base.metadata.create_all(sync_conn, tables=_TABLES)
        )
    async with sqlite_session_factory() as session:
        yield session


def _seed(
    session: AsyncSession,
    request_id: str,
    status_code: int = 200,
    *,
    user_id: int = 1,
) -> None:
    session.add(
        GatewayRequestLog(
            request_id=request_id,
            user_id=user_id,
            model="gpt-4o",
            status_code=status_code,
            tokens_in=10,
            tokens_out=20,
        )
    )


async def test_list_logs_does_not_crash_on_default_sort(
    db_session: AsyncSession,
) -> None:
    """M3-7: listing logs must not raise from a stale resolve_sort signature."""
    service = ObservabilityService(db_session)
    _seed(db_session, "req-1")
    await db_session.flush()

    result = await service.list_logs(ListQuery(), actor=SUPER_ACTOR)

    assert result.total == 1
    assert len(result.items) == 1


async def test_list_logs_honours_desc_sort(db_session: AsyncSession) -> None:
    """M3-7: desc ordering must flow through resolve_sort's (col, is_desc) tuple."""
    service = ObservabilityService(db_session)
    _seed(db_session, "req-1")
    _seed(db_session, "req-2")
    await db_session.flush()

    asc = await service.list_logs(
        ListQuery(sort_by="id", sort_order="asc"), actor=SUPER_ACTOR
    )
    desc = await service.list_logs(
        ListQuery(sort_by="id", sort_order="desc"), actor=SUPER_ACTOR
    )

    asc_ids = [r.id for r in asc.items]
    desc_ids = [r.id for r in desc.items]
    assert asc_ids == sorted(asc_ids)
    assert desc_ids == sorted(desc_ids, reverse=True)


async def test_list_logs_restricted_actor_sees_only_own(
    db_session: AsyncSession,
) -> None:
    """M3-3: a self-only actor must not see other users' gateway logs."""
    service = ObservabilityService(db_session)
    _seed(db_session, "mine", user_id=SELF_ONLY_ACTOR.user_id)
    _seed(db_session, "theirs", user_id=999)
    await db_session.flush()

    result = await service.list_logs(ListQuery(), actor=SELF_ONLY_ACTOR)

    assert result.total == 1
    assert [r.request_id for r in result.items] == ["mine"]


async def test_list_logs_superadmin_sees_all(db_session: AsyncSession) -> None:
    """M3-3: an unrestricted actor still sees every user's logs."""
    service = ObservabilityService(db_session)
    _seed(db_session, "u1", user_id=1)
    _seed(db_session, "u2", user_id=999)
    await db_session.flush()

    result = await service.list_logs(ListQuery(), actor=SUPER_ACTOR)

    assert result.total == 2


async def test_get_log_by_request_id_restricted_actor_cannot_read_others(
    db_session: AsyncSession,
) -> None:
    """M3-3: detail lookup must also enforce scope (no cross-user read)."""
    service = ObservabilityService(db_session)
    _seed(db_session, "theirs", user_id=999)
    await db_session.flush()

    log = await service.get_log_by_request_id("theirs", actor=SELF_ONLY_ACTOR)

    assert log is None


async def test_get_log_by_request_id_restricted_actor_reads_own(
    db_session: AsyncSession,
) -> None:
    """M3-3: detail lookup returns the actor's own log."""
    service = ObservabilityService(db_session)
    _seed(db_session, "mine", user_id=SELF_ONLY_ACTOR.user_id)
    await db_session.flush()

    log = await service.get_log_by_request_id("mine", actor=SELF_ONLY_ACTOR)

    assert log is not None
    assert log.request_id == "mine"
