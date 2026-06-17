"""Direct unit tests for admin repository edge branches.

Route + service tests exercise the main repository paths; these pin the small
branches they don't reach: the empty-input early returns (which must not emit a
SQL ``IN ()``) and the data-scope predicate variants on the user repository
(self-only clause, the match-nothing fallback, single-row visibility).
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from src.admin.roles.repository import RoleRepository
from src.admin.users.repository import UserRepository
from src.auth.service import DataScopeFilter
from src.db.models.identity import User

pytestmark = pytest.mark.asyncio


# ---- empty-input early returns (no SQL emitted) -----------------------------


async def test_role_bulk_lookups_empty_inputs(admin_session: AsyncSession) -> None:
    repo = RoleRepository(admin_session)
    assert await repo.list_menu_ids_for_roles([]) == {}
    assert await repo.list_dept_ids_for_roles([]) == {}
    assert await repo.existing_menu_ids([]) == set()
    assert await repo.existing_dept_ids([]) == set()


async def test_user_bulk_role_lookup_empty_input(admin_session: AsyncSession) -> None:
    repo = UserRepository(admin_session)
    assert await repo.list_role_ids_for_users([]) == {}


# ---- data-scope predicate variants ------------------------------------------


async def test_list_in_scope_self_only(admin_session: AsyncSession) -> None:
    """include_self with no dept grants → only the actor's own row is visible."""
    admin_session.add_all(
        [
            User(id=11, username="me", employee_no="E-11", department_id=None),
            User(id=12, username="other", employee_no="E-12", department_id=None),
        ]
    )
    await admin_session.commit()
    repo = UserRepository(admin_session)
    scope = DataScopeFilter(
        unrestricted=False, department_ids=frozenset(), include_self=True
    )
    rows = await repo.list_in_scope(scope, actor_id=11)
    assert {u.id for u in rows} == {11}


async def test_list_in_scope_no_clauses_matches_nothing(
    admin_session: AsyncSession,
) -> None:
    """Restricted scope with no depts and no self → match-nothing predicate."""
    admin_session.add(User(id=21, username="x", employee_no="E-21", department_id=5))
    await admin_session.commit()
    repo = UserRepository(admin_session)
    scope = DataScopeFilter(
        unrestricted=False, department_ids=frozenset(), include_self=False
    )
    rows = await repo.list_in_scope(scope, actor_id=21)
    assert rows == []


async def test_is_visible_department_in_scope(admin_session: AsyncSession) -> None:
    """Single-row visibility: a row whose dept is in scope is visible."""
    repo = UserRepository(admin_session)
    user = User(id=31, username="d", employee_no="E-31", department_id=500)
    scope = DataScopeFilter(
        unrestricted=False, department_ids=frozenset({500}), include_self=False
    )
    assert repo.is_visible(user, scope, actor_id=999) is True
    # A row outside the dept set, not the actor → not visible.
    other = User(id=32, username="o", employee_no="E-32", department_id=600)
    assert repo.is_visible(other, scope, actor_id=999) is False
