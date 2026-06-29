"""Query-count regression guards for the admin list endpoints (P1 N+1 fixes).

These are *invariant* tests, not snapshots: they assert that the number of SQL
statements issued by a list operation does NOT grow with the number of rows
returned. A 1+N regression would make the count scale with row count, which is
exactly what these tests fail on. The absolute number is deliberately not
asserted (that would be a brittle change-detector); only its invariance to row
count is.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager

import pytest
import pytest_asyncio
from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
)

from src.admin.roles.service import RoleService
from src.admin.users.service import UserService
from src.auth.constants import SUPERADMIN_ROLE_CODE
from src.auth.service import AuthenticatedUser
from src.db.base import Base
from src.db.models.credential import ApiKey
from src.db.models.identity import (
    Department,
    Menu,
    Role,
    RoleMenu,
    User,
    UserRole,
)

pytestmark = pytest.mark.asyncio

_TABLES = [
    Base.metadata.tables[m.__tablename__]
    for m in (User, Department, Role, Menu, UserRole, RoleMenu, ApiKey)
]


@pytest_asyncio.fixture
async def db_session(
    sqlite_engine: AsyncEngine,
    sqlite_session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """Identity/RBAC tables on the in-memory engine + a bound session."""
    async with sqlite_engine.begin() as conn:
        await conn.run_sync(
            lambda sync_conn: Base.metadata.create_all(sync_conn, tables=_TABLES)
        )
    async with sqlite_session_factory() as session:
        yield session


@contextmanager
def count_selects(engine: AsyncEngine) -> Iterator[list[int]]:
    """Count SELECT statements executed on the engine within the block.

    Yields a one-element list whose value is the running count (read it after
    the block exits). Listens on the underlying sync engine, as SQLAlchemy
    cursor events fire there even for async engines.
    """
    counter = [0]

    def _on_execute(conn, cursor, statement, params, context, executemany):  # type: ignore[no-untyped-def]
        if statement.lstrip().upper().startswith("SELECT"):
            counter[0] += 1

    sync_engine = engine.sync_engine
    event.listen(sync_engine, "before_cursor_execute", _on_execute)
    try:
        yield counter
    finally:
        event.remove(sync_engine, "before_cursor_execute", _on_execute)


async def _seed_users_with_roles(
    session: AsyncSession, count: int, *, start: int = 0
) -> None:
    """Seed ``count`` users, each wired to its own role (worst case for 1+N).

    ``start`` offsets the unique keys so successive batches don't collide.
    """
    for i in range(start, start + count):
        user = User(username=f"u{i}", employee_no=f"E-{i}", status="active")
        role = Role(name=f"r{i}", code=f"r{i}", status="active")
        session.add_all([user, role])
        await session.flush()
        session.add(UserRole(user_id=user.id, role_id=role.id))
    await session.flush()


async def _seed_roles_with_grants(
    session: AsyncSession, count: int, *, start: int = 0
) -> None:
    """Seed ``count`` roles, each with a menu grant.

    ``start`` offsets the unique keys so successive batches don't collide.
    """
    for i in range(start, start + count):
        role = Role(name=f"r{i}", code=f"r{i}", status="active")
        menu = Menu(name=f"m{i}", menu_type="button", perms=f"p:{i}", status="active")
        session.add_all([role, menu])
        await session.flush()
        session.add(RoleMenu(role_id=role.id, menu_id=menu.id))
    await session.flush()


def _superuser() -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id=999,
        username="root",
        department_id=None,
        permissions=frozenset({"*:*:*"}),
        role_codes=frozenset({SUPERADMIN_ROLE_CODE}),
    )


async def test_list_users_query_count_is_constant(
    db_session: AsyncSession, sqlite_engine: AsyncEngine
) -> None:
    """list_users must issue the same number of SELECTs for 2 users as for 5
    (role ids fetched in one bulk query, not one-per-user)."""
    service = UserService(db_session)
    actor = _superuser()

    await _seed_users_with_roles(db_session, 2)
    with count_selects(sqlite_engine) as small:
        result_small = await service.list_users(actor)
    assert len(result_small.items) == 2

    # Add three more users (5 total).
    await _seed_users_with_roles(db_session, 3, start=2)
    with count_selects(sqlite_engine) as large:
        result_large = await service.list_users(actor)
    assert len(result_large.items) == 5

    # The invariant: query count did NOT grow with row count (no 1+N).
    assert small[0] == large[0], (
        f"list_users query count scaled with rows: {small[0]} (2 users) "
        f"vs {large[0]} (5 users) — 1+N regression"
    )

    # And each user still carries its role ids (correctness preserved).
    assert all(len(role_ids) == 1 for _user, role_ids in result_large.items)


async def test_list_roles_query_count_is_constant(
    db_session: AsyncSession, sqlite_engine: AsyncEngine
) -> None:
    """list_roles must issue the same number of SELECTs for 2 roles as for 5
    (menu ids fetched in one bulk query, not one-per-role)."""
    service = RoleService(db_session)
    actor = _superuser()

    await _seed_roles_with_grants(db_session, 2)
    with count_selects(sqlite_engine) as small:
        result_small = await service.list_roles(actor)
    assert len(result_small.items) == 2

    await _seed_roles_with_grants(db_session, 3, start=2)
    with count_selects(sqlite_engine) as large:
        result_large = await service.list_roles(actor)
    assert len(result_large.items) == 5

    assert small[0] == large[0], (
        f"list_roles query count scaled with rows: {small[0]} (2 roles) "
        f"vs {large[0]} (5 roles) — 1+R regression"
    )

    # Each role still carries its menu ids (correctness preserved).
    assert all(
        len(menu_ids) == 1 for _role, menu_ids in result_large.items
    )
