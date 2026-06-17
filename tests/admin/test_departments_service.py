"""Direct unit tests for ``DepartmentService`` (service layer, no HTTP).

Drives the service with a plain ``await`` against an in-memory SQLite session.
Route tests (``test_departments.py``) prove behaviour end-to-end; these give
honest, measurable coverage of the service body (the ``ASGITransport`` route
path corrupts coverage.py's C tracer on CPython 3.11) and pin branch invariants:
parent-exists validation, the delete guards (active children / members), and the
reparent cycle guard's multi-hop ancestor walk.

The ``fake_redis`` autouse fixture (conftest) backs ``invalidate_department_tree``
so the post-commit cache invalidation each mutation performs hits the in-process
double, not the shared Redis instance.
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from src.admin.departments.schemas import DepartmentCreate, DepartmentUpdate
from src.admin.departments.service import DepartmentService
from src.auth.constants import SUPERADMIN_ROLE_CODE
from src.auth.service import AuthenticatedUser
from src.db.models.identity import User
from src.exceptions import AppError

pytestmark = pytest.mark.asyncio

ACTOR = AuthenticatedUser(
    user_id=1000,
    username="admin",
    department_id=10,
    permissions=frozenset({"*:*:*"}),
    role_codes=frozenset({SUPERADMIN_ROLE_CODE}),
)


async def test_create_top_level_department(admin_session: AsyncSession) -> None:
    svc = DepartmentService(admin_session)
    dept = await svc.create_department(
        DepartmentCreate(name="Engineering"), actor=ACTOR
    )
    assert dept.id is not None
    assert dept.parent_id is None
    assert dept.created_by == ACTOR.user_id
    assert dept.create_dept == ACTOR.department_id
    listed = await svc.list_departments()
    assert any(d.name == "Engineering" for d in listed)


async def test_create_child_department(admin_session: AsyncSession) -> None:
    svc = DepartmentService(admin_session)
    parent = await svc.create_department(
        DepartmentCreate(name="Parent"), actor=ACTOR
    )
    child = await svc.create_department(
        DepartmentCreate(name="Child", parent_id=parent.id), actor=ACTOR
    )
    assert child.parent_id == parent.id


async def test_create_child_with_bad_parent_rejected(
    admin_session: AsyncSession,
) -> None:
    svc = DepartmentService(admin_session)
    with pytest.raises(AppError) as exc:
        await svc.create_department(
            DepartmentCreate(name="Sub", parent_id=99999), actor=ACTOR
        )
    assert exc.value.status_code == 400


async def test_get_department_not_found_raises(admin_session: AsyncSession) -> None:
    svc = DepartmentService(admin_session)
    with pytest.raises(AppError) as exc:
        await svc.get_department(123456)
    assert exc.value.status_code == 404


async def test_update_department_name(admin_session: AsyncSession) -> None:
    svc = DepartmentService(admin_session)
    dept = await svc.create_department(DepartmentCreate(name="Old"), actor=ACTOR)
    updated = await svc.update_department(
        dept.id, DepartmentUpdate(name="New"), actor=ACTOR
    )
    assert updated.name == "New"


async def test_update_department_not_found_raises(
    admin_session: AsyncSession,
) -> None:
    svc = DepartmentService(admin_session)
    with pytest.raises(AppError) as exc:
        await svc.update_department(
            555555, DepartmentUpdate(name="X"), actor=ACTOR
        )
    assert exc.value.status_code == 404


async def test_reparent_to_valid_parent(admin_session: AsyncSession) -> None:
    svc = DepartmentService(admin_session)
    a = await svc.create_department(DepartmentCreate(name="A"), actor=ACTOR)
    b = await svc.create_department(DepartmentCreate(name="B"), actor=ACTOR)
    # Move B under A — valid, no cycle.
    updated = await svc.update_department(
        b.id, DepartmentUpdate(parent_id=a.id), actor=ACTOR
    )
    assert updated.parent_id == a.id


async def test_reparent_to_self_rejected(admin_session: AsyncSession) -> None:
    svc = DepartmentService(admin_session)
    dept = await svc.create_department(DepartmentCreate(name="Solo"), actor=ACTOR)
    with pytest.raises(AppError) as exc:
        await svc.update_department(
            dept.id, DepartmentUpdate(parent_id=dept.id), actor=ACTOR
        )
    assert exc.value.status_code == 400


async def test_reparent_to_nonexistent_parent_rejected(
    admin_session: AsyncSession,
) -> None:
    svc = DepartmentService(admin_session)
    dept = await svc.create_department(DepartmentCreate(name="D"), actor=ACTOR)
    with pytest.raises(AppError) as exc:
        await svc.update_department(
            dept.id, DepartmentUpdate(parent_id=88888), actor=ACTOR
        )
    assert exc.value.status_code == 400


async def test_reparent_direct_cycle_rejected(admin_session: AsyncSession) -> None:
    svc = DepartmentService(admin_session)
    a = await svc.create_department(DepartmentCreate(name="A"), actor=ACTOR)
    b = await svc.create_department(
        DepartmentCreate(name="B", parent_id=a.id), actor=ACTOR
    )
    # Make A a child of B → A->B->A cycle.
    with pytest.raises(AppError) as exc:
        await svc.update_department(
            a.id, DepartmentUpdate(parent_id=b.id), actor=ACTOR
        )
    assert exc.value.status_code == 400


async def test_reparent_deep_cycle_rejected(admin_session: AsyncSession) -> None:
    # A -> B -> C ; trying to set A.parent = C must walk C->B->A and detect the cycle.
    svc = DepartmentService(admin_session)
    a = await svc.create_department(DepartmentCreate(name="A"), actor=ACTOR)
    b = await svc.create_department(
        DepartmentCreate(name="B", parent_id=a.id), actor=ACTOR
    )
    c = await svc.create_department(
        DepartmentCreate(name="C", parent_id=b.id), actor=ACTOR
    )
    with pytest.raises(AppError) as exc:
        await svc.update_department(
            a.id, DepartmentUpdate(parent_id=c.id), actor=ACTOR
        )
    assert exc.value.status_code == 400


async def test_reparent_to_none_allowed(admin_session: AsyncSession) -> None:
    # Clearing parent (promote to top-level) short-circuits the cycle guard.
    svc = DepartmentService(admin_session)
    a = await svc.create_department(DepartmentCreate(name="A"), actor=ACTOR)
    b = await svc.create_department(
        DepartmentCreate(name="B", parent_id=a.id), actor=ACTOR
    )
    updated = await svc.update_department(
        b.id, DepartmentUpdate(parent_id=None), actor=ACTOR
    )
    # parent_id None is in the unset-exclusion semantics; explicit None is a no-op
    # reparent path that must not raise.
    assert updated.id == b.id


async def test_delete_empty_department_succeeds(admin_session: AsyncSession) -> None:
    svc = DepartmentService(admin_session)
    dept = await svc.create_department(DepartmentCreate(name="Temp"), actor=ACTOR)
    await svc.delete_department(dept.id, actor=ACTOR)
    with pytest.raises(AppError):
        await svc.get_department(dept.id)  # soft-deleted → gone


async def test_delete_department_not_found_raises(
    admin_session: AsyncSession,
) -> None:
    svc = DepartmentService(admin_session)
    with pytest.raises(AppError) as exc:
        await svc.delete_department(777777, actor=ACTOR)
    assert exc.value.status_code == 404


async def test_delete_department_with_child_blocked(
    admin_session: AsyncSession,
) -> None:
    svc = DepartmentService(admin_session)
    parent = await svc.create_department(
        DepartmentCreate(name="Parent"), actor=ACTOR
    )
    await svc.create_department(
        DepartmentCreate(name="Child", parent_id=parent.id), actor=ACTOR
    )
    with pytest.raises(AppError) as exc:
        await svc.delete_department(parent.id, actor=ACTOR)
    assert exc.value.status_code == 400


async def test_delete_department_with_member_blocked(
    admin_session: AsyncSession,
) -> None:
    svc = DepartmentService(admin_session)
    dept = await svc.create_department(
        DepartmentCreate(name="Staffed"), actor=ACTOR
    )
    admin_session.add(
        User(
            id=4242,
            username="member",
            employee_no="E-4242",
            department_id=dept.id,
            status="active",
        )
    )
    await admin_session.commit()
    with pytest.raises(AppError) as exc:
        await svc.delete_department(dept.id, actor=ACTOR)
    assert exc.value.status_code == 400
