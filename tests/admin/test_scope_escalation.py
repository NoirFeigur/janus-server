"""Regression tests for the 4th-audit data-scope escalation holes (service layer).

Two confirmed P0s, driven directly against the in-memory SQLite session:

- **P0-1** Relative-scope escalation: a scoped actor must NOT assign/mint a role
  whose visibility — resolved against the eventual holder's department — exceeds
  the actor's own. The pre-fix guards waved every relative scope through on the
  false premise "bounded by the holder", but for the SAME holder
  ``dept_and_child`` resolves to a strictly broader subtree than ``dept_only``.
- **P0-2** Unscoped department mutation: a scoped admin must NOT read/mutate a
  department outside its scope. The department resource keys scope off the dept's
  OWN id (the tree IS the scope boundary), not the ``create_dept`` audit column.

The ``fake_redis`` autouse fixture (conftest) backs the dept-tree cache that
``resolve_data_scope`` consults for subtree expansion.
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from src.admin.departments.schemas import DepartmentCreate, DepartmentUpdate
from src.admin.departments.service import DepartmentService
from src.admin.roles.schemas import RoleCreate
from src.admin.roles.service import RoleService
from src.admin.users.schemas import UserCreate, UserUpdate
from src.admin.users.service import UserService
from src.auth.service import AuthenticatedUser
from src.db.models.identity import Department, Role, User, UserRole
from src.enums import DataScope
from src.exceptions import AppError

pytestmark = pytest.mark.asyncio

ACTOR_ID = 1000


def _scoped_actor(*, dept: int | None, perms: set[str]) -> AuthenticatedUser:
    """A non-super actor (no ``superadmin`` role code) → data scope from DB roles."""
    return AuthenticatedUser(
        user_id=ACTOR_ID,
        username="scoped",
        department_id=dept,
        permissions=frozenset(perms),
    )


async def _seed_actor_role(
    session: AsyncSession, *, data_scope: str, code: str = "actor-role"
) -> None:
    """Give the actor a non-super role so ``resolve_data_scope`` bounds them."""
    role = Role(name=code, code=code, data_scope=data_scope, status="active")
    session.add(role)
    await session.flush()
    session.add(UserRole(user_id=ACTOR_ID, role_id=role.id))
    await session.commit()


# --- P0-1: relative-scope escalation on role ASSIGNMENT (user surface) --------


async def test_scoped_actor_cannot_assign_dept_and_child_role(
    admin_session: AsyncSession,
) -> None:
    """A ``dept_only`` actor at dept 10 must NOT assign a ``dept_and_child`` role
    to a holder in dept 10: it would resolve to subtree {10, 20} ⊋ the actor's
    own {10} — visibility into a child dept the actor never had."""
    admin_session.add_all(
        [
            Department(id=10, name="d10", parent_id=None),
            Department(id=20, name="d20", parent_id=10),
        ]
    )
    await admin_session.flush()
    await _seed_actor_role(admin_session, data_scope="dept")
    wide = Role(
        name="wide", code="wide", data_scope="dept_and_child", status="active"
    )
    admin_session.add(wide)
    await admin_session.commit()

    actor = _scoped_actor(dept=10, perms={"system:user:add"})
    svc = UserService(admin_session)
    with pytest.raises(AppError) as exc:
        await svc.create_user(
            UserCreate(
                username="esc",
                employee_no="E-esc",
                department_id=10,
                role_ids=[wide.id],
            ),
            actor,
        )
    assert exc.value.status_code == 403


async def test_scoped_actor_can_assign_dept_only_role_in_scope(
    admin_session: AsyncSession,
) -> None:
    """Counterpart: assigning a ``dept_only`` role to a holder in the actor's own
    department resolves to {10} ⊆ {10} — allowed."""
    admin_session.add(Department(id=10, name="d10", parent_id=None))
    await admin_session.flush()
    await _seed_actor_role(admin_session, data_scope="dept")
    narrow = Role(name="narrow", code="narrow", data_scope="dept", status="active")
    admin_session.add(narrow)
    await admin_session.commit()

    actor = _scoped_actor(dept=10, perms={"system:user:add"})
    svc = UserService(admin_session)
    user, role_ids = await svc.create_user(
        UserCreate(
            username="ok",
            employee_no="E-ok",
            department_id=10,
            role_ids=[narrow.id],
        ),
        actor,
    )
    assert role_ids == [narrow.id]


async def test_scoped_actor_can_assign_self_only_role(
    admin_session: AsyncSession,
) -> None:
    """``self_only`` confers no department visibility (resolves to {}), so it is
    within any actor's scope — always assignable."""
    admin_session.add(Department(id=10, name="d10", parent_id=None))
    await admin_session.flush()
    await _seed_actor_role(admin_session, data_scope="dept")
    self_role = Role(name="selfr", code="selfr", data_scope="self", status="active")
    admin_session.add(self_role)
    await admin_session.commit()

    actor = _scoped_actor(dept=10, perms={"system:user:add"})
    svc = UserService(admin_session)
    _, role_ids = await svc.create_user(
        UserCreate(
            username="selfok",
            employee_no="E-selfok",
            department_id=10,
            role_ids=[self_role.id],
        ),
        actor,
    )
    assert role_ids == [self_role.id]


async def test_update_user_resolves_role_against_new_department(
    admin_session: AsyncSession,
) -> None:
    """The role guard on update must resolve against the holder's department
    AFTER the update. An actor whose scope spans {10, 20} assigning a
    ``dept_and_child`` role while moving the user to dept 20 would let the holder
    see subtree(20) = {20, 30} ⊋ the actor's {10, 20} — must be rejected."""
    admin_session.add_all(
        [
            Department(id=10, name="d10", parent_id=None),
            Department(id=20, name="d20", parent_id=10),
            Department(id=30, name="d30", parent_id=20),
            User(id=55, username="movee", employee_no="E-55", department_id=10),
        ]
    )
    await admin_session.flush()
    # Actor scope = subtree(10) = {10, 20, 30} via dept_and_child? No — we want the
    # actor bounded to {10, 20}. Use a custom-scope actor role granting {10, 20}.
    actor_role = Role(
        name="ar", code="ar", data_scope="custom", status="active"
    )
    admin_session.add(actor_role)
    await admin_session.flush()
    from src.db.models.identity import RoleDept

    admin_session.add_all(
        [
            RoleDept(role_id=actor_role.id, dept_id=10),
            RoleDept(role_id=actor_role.id, dept_id=20),
            UserRole(user_id=ACTOR_ID, role_id=actor_role.id),
        ]
    )
    wide = Role(
        name="wide2", code="wide2", data_scope="dept_and_child", status="active"
    )
    admin_session.add(wide)
    await admin_session.commit()

    actor = _scoped_actor(dept=10, perms={"system:user:edit"})
    svc = UserService(admin_session)
    with pytest.raises(AppError) as exc:
        await svc.update_user(
            55,
            UserUpdate(department_id=20, role_ids=[wide.id]),
            actor,
        )
    assert exc.value.status_code == 403


# --- P0-1: relative-scope escalation on role MINT (role surface) --------------


async def test_scoped_actor_cannot_mint_dept_and_child_role(
    admin_session: AsyncSession,
) -> None:
    """A ``dept_only`` actor at dept 10 must NOT mint a ``dept_and_child`` role:
    resolved against the actor-as-holder it is subtree {10, 20} ⊋ {10}."""
    admin_session.add_all(
        [
            Department(id=10, name="d10", parent_id=None),
            Department(id=20, name="d20", parent_id=10),
        ]
    )
    await admin_session.flush()
    await _seed_actor_role(admin_session, data_scope="dept")

    actor = _scoped_actor(dept=10, perms={"system:role:add"})
    svc = RoleService(admin_session)
    with pytest.raises(AppError) as exc:
        await svc.create_role(
            RoleCreate(name="wide", code="wide", data_scope=DataScope.dept_and_child),
            actor=actor,
        )
    assert exc.value.status_code == 403


async def test_scoped_actor_can_mint_self_only_role(
    admin_session: AsyncSession,
) -> None:
    """``self_only`` confers no dept visibility, so a scoped actor may mint it."""
    admin_session.add(Department(id=10, name="d10", parent_id=None))
    await admin_session.flush()
    await _seed_actor_role(admin_session, data_scope="dept")

    actor = _scoped_actor(dept=10, perms={"system:role:add"})
    svc = RoleService(admin_session)
    role, _, _ = await svc.create_role(
        RoleCreate(name="s", code="s", data_scope=DataScope.self_only),
        actor=actor,
    )
    assert role.data_scope == DataScope.self_only.value


async def test_scoped_actor_can_mint_dept_only_role(
    admin_session: AsyncSession,
) -> None:
    """A ``dept_only`` mint resolves to the actor's own {10} ⊆ {10} — allowed."""
    admin_session.add(Department(id=10, name="d10", parent_id=None))
    await admin_session.flush()
    await _seed_actor_role(admin_session, data_scope="dept")

    actor = _scoped_actor(dept=10, perms={"system:role:add"})
    svc = RoleService(admin_session)
    role, _, _ = await svc.create_role(
        RoleCreate(name="d", code="d", data_scope=DataScope.dept_only),
        actor=actor,
    )
    assert role.data_scope == DataScope.dept_only.value


# --- P0-2: department-tree mutation must respect actor scope -------------------


async def _scoped_dept_admin(
    session: AsyncSession, *, perms: set[str]
) -> AuthenticatedUser:
    """Actor scoped to dept 10 only (dept-scope role), with dept 30 a sibling."""
    session.add_all(
        [
            Department(id=10, name="d10", parent_id=None),
            Department(id=30, name="d30", parent_id=None),
        ]
    )
    await session.flush()
    await _seed_actor_role(session, data_scope="dept")
    return _scoped_actor(dept=10, perms=perms)


async def test_scoped_admin_cannot_get_out_of_scope_department(
    admin_session: AsyncSession,
) -> None:
    actor = await _scoped_dept_admin(admin_session, perms={"system:dept:list"})
    svc = DepartmentService(admin_session)
    with pytest.raises(AppError) as exc:
        await svc.get_department(30, actor=actor)
    assert exc.value.status_code == 403


async def test_scoped_admin_cannot_update_out_of_scope_department(
    admin_session: AsyncSession,
) -> None:
    actor = await _scoped_dept_admin(admin_session, perms={"system:dept:edit"})
    svc = DepartmentService(admin_session)
    with pytest.raises(AppError) as exc:
        await svc.update_department(30, DepartmentUpdate(name="hijack"), actor=actor)
    assert exc.value.status_code == 403


async def test_scoped_admin_cannot_delete_out_of_scope_department(
    admin_session: AsyncSession,
) -> None:
    actor = await _scoped_dept_admin(admin_session, perms={"system:dept:remove"})
    svc = DepartmentService(admin_session)
    with pytest.raises(AppError) as exc:
        await svc.delete_department(30, actor=actor)
    assert exc.value.status_code == 403


async def test_scoped_admin_cannot_reparent_into_out_of_scope_parent(
    admin_session: AsyncSession,
) -> None:
    """An actor scoped to subtree {10, 11} must not reparent its own dept 11 under
    the out-of-scope dept 30 (would graft the actor's subtree under foreign tree)."""
    admin_session.add_all(
        [
            Department(id=10, name="d10", parent_id=None),
            Department(id=11, name="d11", parent_id=10),
            Department(id=30, name="d30", parent_id=None),
        ]
    )
    await admin_session.flush()
    await _seed_actor_role(admin_session, data_scope="dept_and_child")

    actor = _scoped_actor(dept=10, perms={"system:dept:edit"})
    svc = DepartmentService(admin_session)
    with pytest.raises(AppError) as exc:
        await svc.update_department(11, DepartmentUpdate(parent_id=30), actor=actor)
    assert exc.value.status_code == 403


async def test_scoped_admin_list_filters_to_scope(
    admin_session: AsyncSession,
) -> None:
    actor = await _scoped_dept_admin(admin_session, perms={"system:dept:list"})
    svc = DepartmentService(admin_session)
    listed = await svc.list_departments(actor)
    ids = {d.id for d in listed}
    assert ids == {10}
    assert 30 not in ids


async def test_scoped_admin_batch_delete_skips_out_of_scope(
    admin_session: AsyncSession,
) -> None:
    actor = await _scoped_dept_admin(admin_session, perms={"system:dept:remove"})
    svc = DepartmentService(admin_session)
    result = await svc.batch_delete_departments([10, 30], actor=actor)
    assert result.requested == 2
    assert result.affected == 1
    assert result.skipped_ids == ["30"]


async def test_unrestricted_admin_can_create_top_level(
    admin_session: AsyncSession,
) -> None:
    """An unrestricted (all-scope) actor may still create a root department —
    the parent-in-scope rule only binds non-unrestricted actors."""
    role = Role(name="allr", code="allr", data_scope="all", status="active")
    admin_session.add(role)
    await admin_session.flush()
    admin_session.add(UserRole(user_id=ACTOR_ID, role_id=role.id))
    await admin_session.commit()

    actor = _scoped_actor(dept=None, perms={"system:dept:add"})
    svc = DepartmentService(admin_session)
    dept = await svc.create_department(DepartmentCreate(name="Root"), actor=actor)
    assert dept.parent_id is None
