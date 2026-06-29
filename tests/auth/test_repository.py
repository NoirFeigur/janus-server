"""Direct unit tests for ``AuthRepository`` edge branches.

``AuthService`` tests exercise the main aggregation queries; these pin the small
branches they don't reach: the empty-``role_ids`` early return (which must not
emit a SQL ``IN ()``) and the permission-code aggregation with real rows.
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth.repository import AuthRepository
from src.db.models.identity import Menu, Role, RoleMenu

pytestmark = pytest.mark.asyncio


async def test_list_permission_codes_for_roles_empty_returns_frozenset(
    auth_session: AsyncSession,
) -> None:
    repo = AuthRepository(auth_session)
    assert await repo.list_permission_codes_for_roles([]) == frozenset()


async def test_list_permission_codes_for_roles_aggregates(
    auth_session: AsyncSession,
) -> None:
    role = Role(name="r", code="r", status="active")
    menu = Menu(name="m", menu_type="button", perms="system:user:add", status="active")
    auth_session.add_all([role, menu])
    await auth_session.flush()
    auth_session.add(RoleMenu(role_id=role.id, menu_id=menu.id))
    await auth_session.flush()
    repo = AuthRepository(auth_session)
    perms = await repo.list_permission_codes_for_roles([role.id])
    assert perms == frozenset({"system:user:add"})
