"""Direct unit tests for ``AuthRepository`` edge branches.

``AuthService`` tests exercise the main aggregation queries; these pin the small
branches they don't reach: the empty-``role_ids`` early returns (which must not
emit a SQL ``IN ()``) and the ``list_role_department_ids`` body (custom-scope
dept grant lookup) with real rows.
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth.repository import AuthRepository
from src.db.models.identity import Menu, Role, RoleDept, RoleMenu
from tests.auth.conftest import seed_user

pytestmark = pytest.mark.asyncio


async def test_list_permission_codes_for_roles_empty_returns_frozenset(
    auth_session: AsyncSession,
) -> None:
    repo = AuthRepository(auth_session)
    assert await repo.list_permission_codes_for_roles([]) == frozenset()


async def test_list_permission_codes_for_roles_aggregates(
    auth_session: AsyncSession,
) -> None:
    role = Role(name="r", code="r", data_scope="self", status="active")
    menu = Menu(name="m", menu_type="button", perms="system:user:add", status="active")
    auth_session.add_all([role, menu])
    await auth_session.flush()
    auth_session.add(RoleMenu(role_id=role.id, menu_id=menu.id))
    await auth_session.flush()
    repo = AuthRepository(auth_session)
    perms = await repo.list_permission_codes_for_roles([role.id])
    assert perms == frozenset({"system:user:add"})


async def test_list_role_department_ids_empty_returns_frozenset(
    auth_session: AsyncSession,
) -> None:
    repo = AuthRepository(auth_session)
    assert await repo.list_role_department_ids([]) == frozenset()


async def test_list_role_department_ids_returns_grants(
    auth_session: AsyncSession,
) -> None:
    from src.db.models.identity import Department

    role = Role(name="c", code="c", data_scope="custom", status="active")
    auth_session.add(role)
    auth_session.add_all(
        [Department(id=d, name=f"d{d}", parent_id=None) for d in (10, 20)]
    )
    await auth_session.flush()
    auth_session.add_all(
        [RoleDept(role_id=role.id, dept_id=10), RoleDept(role_id=role.id, dept_id=20)]
    )
    await auth_session.flush()
    repo = AuthRepository(auth_session)
    dept_ids = await repo.list_role_department_ids([role.id])
    assert dept_ids == frozenset({10, 20})


async def test_list_active_roles_for_user(auth_session: AsyncSession) -> None:
    """Sanity that the active-roles query returns the user's role rows."""
    from src.db.models.identity import UserRole

    user = await seed_user(auth_session)
    role = Role(name="ar", code="ar", data_scope="self", status="active")
    auth_session.add(role)
    await auth_session.flush()
    auth_session.add(UserRole(user_id=user.id, role_id=role.id))
    await auth_session.flush()
    repo = AuthRepository(auth_session)
    roles = await repo.list_active_roles(user.id)
    assert [r.code for r in roles] == ["ar"]
