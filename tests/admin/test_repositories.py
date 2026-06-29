"""Direct unit tests for admin repository edge branches.

Route + service tests exercise the main repository paths; these pin the small
branches they don't reach: the empty-input early returns (which must not emit a
SQL ``IN ()``).
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from src.admin.departments.repository import DepartmentRepository
from src.admin.roles.repository import RoleRepository
from src.admin.users.repository import UserRepository
from src.db.models.identity import Department

pytestmark = pytest.mark.asyncio


# ---- empty-input early returns (no SQL emitted) -----------------------------


async def test_role_bulk_lookups_empty_inputs(admin_session: AsyncSession) -> None:
    repo = RoleRepository(admin_session)
    assert await repo.list_menu_ids_for_roles([]) == {}
    assert await repo.existing_menu_ids([]) == set()


async def test_user_bulk_role_lookup_empty_input(admin_session: AsyncSession) -> None:
    repo = UserRepository(admin_session)
    assert await repo.list_role_ids_for_users([]) == {}


async def test_department_get_for_update_excludes_deleted(
    admin_session: AsyncSession,
) -> None:
    """The lock-acquiring fetch returns a live dept and excludes soft-deleted
    (FOR UPDATE is a no-op on SQLite, so this just pins the filter + return)."""
    repo = DepartmentRepository(admin_session)
    admin_session.add_all(
        [
            Department(id=61, name="live", parent_id=None),
            Department(id=62, name="gone", parent_id=None, is_deleted=True),
        ]
    )
    await admin_session.commit()
    assert (await repo.get_for_update(61)) is not None
    assert (await repo.get_for_update(62)) is None
    assert (await repo.get_for_update(999)) is None

