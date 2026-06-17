"""Direct-call coverage for the admin router layer (no HTTP transport).

Route tests (``test_departments.py`` / ``test_roles.py`` / ``test_users.py``)
prove behaviour end-to-end through ``httpx.ASGITransport``, but that path runs
the handlers under anyio task-switching which corrupts coverage.py's C tracer on
CPython 3.11 — the ``return success(...)`` line of each create/update/delete
endpoint executes but reads back as uncovered. Calling the endpoint coroutines
directly with a real service keeps the tracer armed, so the thin router bodies
get honest coverage. Behaviour is already asserted by the route tests; here we
just assert the envelope plumbing (data passthrough + trace id) is wired.
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from src.admin.departments import router as dept_router
from src.admin.departments.schemas import DepartmentCreate, DepartmentUpdate
from src.admin.departments.service import DepartmentService
from src.admin.roles import router as role_router
from src.admin.roles.schemas import RoleCreate, RoleUpdate
from src.admin.roles.service import RoleService
from src.admin.users import router as user_router
from src.admin.users.schemas import UserCreate, UserUpdate
from src.admin.users.service import UserService
from src.auth.service import AuthenticatedUser

pytestmark = pytest.mark.asyncio

TRACE = "trace-xyz"


def _actor() -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id=1000,
        username="admin",
        department_id=None,
        permissions=frozenset({"*:*:*"}),
    )


# ---- departments router (lines 58, 70, 83) ----------------------------------


async def test_department_create_update_delete_endpoints(
    admin_session: AsyncSession,
) -> None:
    svc = DepartmentService(admin_session)
    actor = _actor()

    created = await dept_router.create_department(
        DepartmentCreate(name="Eng"), svc, TRACE, actor
    )
    assert created.success is True
    assert created.trace_id == TRACE
    assert created.data is not None
    dept_id = int(created.data.id)

    updated = await dept_router.update_department(
        dept_id, DepartmentUpdate(name="Eng2"), svc, TRACE, actor
    )
    assert updated.data is not None
    assert updated.data.name == "Eng2"

    deleted = await dept_router.delete_department(dept_id, svc, TRACE, actor)
    assert deleted.success is True
    assert deleted.data is None
    assert deleted.trace_id == TRACE


# ---- roles router (lines 60, 72, 85) ----------------------------------------


async def test_role_create_update_delete_endpoints(
    admin_session: AsyncSession,
) -> None:
    svc = RoleService(admin_session)
    actor = _actor()

    created = await role_router.create_role(
        RoleCreate(name="Viewer", code="viewer"), svc, TRACE, actor
    )
    assert created.success is True
    assert created.trace_id == TRACE
    assert created.data is not None
    role_id = int(created.data.id)

    updated = await role_router.update_role(
        role_id, RoleUpdate(name="Viewer2"), svc, TRACE, actor
    )
    assert updated.data is not None
    assert updated.data.name == "Viewer2"

    deleted = await role_router.delete_role(role_id, svc, TRACE, actor)
    assert deleted.success is True
    assert deleted.data is None


# ---- users router (lines 62, 74, 87) ----------------------------------------


async def test_user_create_update_delete_endpoints(
    admin_session: AsyncSession,
) -> None:
    svc = UserService(admin_session)
    actor = _actor()

    created = await user_router.create_user(
        UserCreate(username="carol", employee_no="E-1"), svc, TRACE, actor
    )
    assert created.success is True
    assert created.trace_id == TRACE
    assert created.data is not None
    user_id = int(created.data.id)

    updated = await user_router.update_user(
        user_id, UserUpdate(real_name="Carol R."), svc, TRACE, actor
    )
    assert updated.data is not None
    assert updated.data.real_name == "Carol R."

    deleted = await user_router.delete_user(user_id, svc, TRACE, actor)
    assert deleted.success is True
    assert deleted.data is None
