"""Crash regressions for admin rate-limit list/get (M3-7, M3-8).

Two production crashes were latent here:

- **M3-7**: ``RateLimitRepository.list_rules`` called ``resolve_sort`` with the
  *old* positional signature (passing the column map as ``query`` and a column
  object as ``default``). The current ``resolve_sort(query, *, allowed, default)``
  would dereference ``query.sort_by`` on a ``dict`` → ``TypeError`` / ``AttributeError``
  on every list request.
- **M3-8**: ``RateLimitService.get_rule`` raised ``ErrorCode.resource_not_found``,
  an enum member that does not exist → ``AttributeError`` on every miss instead of
  a clean 404.

These tests exercise the real code path on the in-memory engine; they fail (raise)
against the pre-fix code and pass once the callers use the correct API.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from src.admin.rate_limits.service import RateLimitService
from src.auth.constants import SUPERADMIN_ROLE_CODE
from src.auth.service import AuthenticatedUser
from src.core.query import ListQuery
from src.db.base import Base
from src.db.models.rate_limit import RateLimitRule
from src.enums import ErrorCode
from src.exceptions import AppError

pytestmark = pytest.mark.asyncio

_TABLES = [Base.metadata.tables[RateLimitRule.__tablename__]]


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


def _actor() -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id=999,
        username="root",
        department_id=None,
        permissions=frozenset({"*:*:*"}),
        role_codes=frozenset({SUPERADMIN_ROLE_CODE}),
    )


async def test_list_rules_does_not_crash_on_default_sort(
    db_session: AsyncSession,
) -> None:
    """M3-7: listing rules must not raise from a stale resolve_sort signature."""
    service = RateLimitService(db_session)
    db_session.add(
        RateLimitRule(
            subject_type="user",
            subject_id=1,
            logical_model_id=None,
            rpm_limit=60,
            status="active",
        )
    )
    await db_session.flush()

    result = await service.list_rules(ListQuery())

    assert result.total == 1
    assert len(result.items) == 1


async def test_list_rules_honours_desc_sort(db_session: AsyncSession) -> None:
    """M3-7: desc ordering must flow through resolve_sort's (col, is_desc) tuple."""
    service = RateLimitService(db_session)
    for sid in (1, 2):
        db_session.add(
            RateLimitRule(
                subject_type="user",
                subject_id=sid,
                logical_model_id=None,
                rpm_limit=60,
                status="active",
            )
        )
    await db_session.flush()

    asc = await service.list_rules(ListQuery(sort_by="id", sort_order="asc"))
    desc = await service.list_rules(ListQuery(sort_by="id", sort_order="desc"))

    asc_ids = [r.id for r in asc.items]
    desc_ids = [r.id for r in desc.items]
    assert asc_ids == sorted(asc_ids)
    assert desc_ids == sorted(desc_ids, reverse=True)


async def test_get_rule_missing_raises_clean_404(db_session: AsyncSession) -> None:
    """M3-8: a missing rule must raise AppError(404), not AttributeError."""
    service = RateLimitService(db_session)

    with pytest.raises(AppError) as exc_info:
        await service.get_rule(123456789)

    assert exc_info.value.code == ErrorCode.request_invalid
    assert exc_info.value.status_code == 404
