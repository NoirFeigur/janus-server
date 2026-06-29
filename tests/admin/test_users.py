"""Route-level tests for admin user CRUD + role assignment + PII masking."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from src.admin.users.schemas import UserRead
from src.auth.service import AuthenticatedUser
from src.core.query import mask_fields
from src.db.models.identity import Department, Menu, Role, RoleMenu, User, UserRole
from tests.admin.conftest import ADMIN_ID, AdminCtx

pytestmark = pytest.mark.asyncio


def _set_non_super_actor(admin_ctx: AdminCtx, *, perms: set[str]) -> None:
    """Drop the default super-admin actor to a permissioned non-superuser.

    Super-admin is code-based: the conftest override marks the actor superuser
    iff its perms contain ``*:*:*``. Setting a concrete perm set (without the
    wildcard) makes ``is_superuser`` False, which is all the masking + dominance
    paths key off — there is no data-scope axis any more.
    """
    admin_ctx.state.perms = perms


def _non_super_actor(*, perms: set[str]) -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id=ADMIN_ID,
        username="admin",
        department_id=None,
        permissions=frozenset(perms),
    )


async def test_create_user_hashes_password_and_hides_it(admin_ctx: AdminCtx) -> None:
    resp = await admin_ctx.client.post(
        "/admin/users",
        json={"username": "carol", "employee_no": "E-100", "password": "pw123456"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["username"] == "carol"
    assert "password" not in data  # §0.8 — never exposed

    # Password is stored hashed, not plaintext.
    row = await admin_ctx.session.get(User, int(data["id"]))
    assert row is not None
    assert row.password is not None
    assert row.password != "pw123456"


async def test_create_user_rejects_weak_password(admin_ctx: AdminCtx) -> None:
    # Strength policy applies to admin-set passwords too (not only self-service /
    # reset): a 7-char password fails the min-length-8 floor with a machine code.
    resp = await admin_ctx.client.post(
        "/admin/users",
        json={"username": "weakling", "employee_no": "E-weak", "password": "pw12345"},
    )
    assert resp.status_code == 400, resp.text
    body = resp.json()
    assert body["success"] is False
    assert body["code"] == "auth.password_too_weak"
    assert "too_short" in body["params"]["violations"]
    # Nothing persisted on a rejected create.
    assert await admin_ctx.session.get(User, 0) is None


async def test_create_user_duplicate_username_rejected(admin_ctx: AdminCtx) -> None:
    await admin_ctx.client.post(
        "/admin/users", json={"username": "dave", "employee_no": "E-1"}
    )
    resp = await admin_ctx.client.post(
        "/admin/users", json={"username": "dave", "employee_no": "E-2"}
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == "request.invalid"


async def test_create_user_duplicate_employee_no_rejected(admin_ctx: AdminCtx) -> None:
    await admin_ctx.client.post(
        "/admin/users", json={"username": "u1", "employee_no": "E-DUP"}
    )
    resp = await admin_ctx.client.post(
        "/admin/users", json={"username": "u2", "employee_no": "E-DUP"}
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == "request.invalid"


async def test_create_user_with_roles(admin_ctx: AdminCtx) -> None:
    role = Role(name="member", code="member", status="active")
    admin_ctx.session.add(role)
    await admin_ctx.session.commit()
    resp = await admin_ctx.client.post(
        "/admin/users",
        json={"username": "erin", "employee_no": "E-3", "role_ids": [str(role.id)]},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["role_ids"] == [str(role.id)]


async def test_create_user_unknown_role_rejected(admin_ctx: AdminCtx) -> None:
    resp = await admin_ctx.client.post(
        "/admin/users",
        json={"username": "frank", "employee_no": "E-4", "role_ids": ["77777"]},
    )
    assert resp.status_code == 400


async def test_update_user_status_and_roles(admin_ctx: AdminCtx) -> None:
    created = await admin_ctx.client.post(
        "/admin/users", json={"username": "gina", "employee_no": "E-5"}
    )
    user_id = created.json()["data"]["id"]
    resp = await admin_ctx.client.put(
        f"/admin/users/{user_id}", json={"status": "disabled"}
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["status"] == "disabled"


async def test_delete_user(admin_ctx: AdminCtx) -> None:
    created = await admin_ctx.client.post(
        "/admin/users", json={"username": "harry", "employee_no": "E-6"}
    )
    user_id = created.json()["data"]["id"]
    resp = await admin_ctx.client.delete(f"/admin/users/{user_id}")
    assert resp.status_code == 200


async def test_user_endpoints_require_permission(admin_ctx: AdminCtx) -> None:
    admin_ctx.state.perms = {"system:dept:list"}  # lacks user perms
    resp = await admin_ctx.client.get("/admin/users")
    assert resp.status_code == 403


async def test_list_users_keyword_filters_by_username(admin_ctx: AdminCtx) -> None:
    admin_ctx.session.add_all(
        [
            User(id=2101, username="alpha-match", employee_no="E-2101"),
            User(id=2102, username="beta-only", employee_no="E-2102"),
        ]
    )
    await admin_ctx.session.commit()

    resp = await admin_ctx.client.get("/admin/users?keyword=ALPHA")
    assert resp.status_code == 200, resp.text
    usernames = {item["username"] for item in resp.json()["data"]["items"]}
    assert usernames == {"alpha-match"}


async def test_list_users_sort_by_username_desc(admin_ctx: AdminCtx) -> None:
    admin_ctx.session.add_all(
        [
            User(id=2111, username="anna", employee_no="E-2111"),
            User(id=2112, username="zoe", employee_no="E-2112"),
            User(id=2113, username="mike", employee_no="E-2113"),
        ]
    )
    await admin_ctx.session.commit()

    resp = await admin_ctx.client.get(
        "/admin/users?sort_by=username&sort_order=desc&limit=3"
    )
    assert resp.status_code == 200, resp.text
    usernames = [item["username"] for item in resp.json()["data"]["items"]]
    assert usernames == ["zoe", "mike", "anna"]


async def test_list_users_invalid_sort_by_returns_400(admin_ctx: AdminCtx) -> None:
    resp = await admin_ctx.client.get("/admin/users?sort_by=evil")
    assert resp.status_code == 400
    assert resp.json()["code"] == "request.invalid"


async def test_list_users_masks_pii_for_non_superuser(admin_ctx: AdminCtx) -> None:
    admin_ctx.session.add(Department(id=2120, name="d2120", parent_id=None))
    await admin_ctx.session.commit()
    created = await admin_ctx.client.post(
        "/admin/users",
        json={
            "username": "pii-scoped",
            "employee_no": "E-2120",
            "department_id": "2120",
            "email": "alice@example.com",
            "mobile": "13800001111",
        },
    )
    assert created.status_code == 200, created.text
    created_read = UserRead.model_validate(created.json()["data"])
    _set_non_super_actor(admin_ctx, perms={"system:user:list"})

    resp = await admin_ctx.client.get("/admin/users?keyword=pii-scoped")
    assert resp.status_code == 200, resp.text
    listed = resp.json()["data"]["items"][0]
    expected = mask_fields(
        created_read,
        actor=_non_super_actor(perms={"system:user:list"}),
        sensitive=("mobile", "email"),
    )
    assert listed["mobile"] == expected.mobile
    assert listed["email"] == expected.email


async def test_create_user_readback_masks_pii_for_non_superuser(
    admin_ctx: AdminCtx,
) -> None:
    """A non-super creator must see masked contacts in the readback too,
    exactly like the list endpoint — not unmasked raw email/mobile."""
    admin_ctx.session.add(Department(id=2160, name="d2160", parent_id=None))
    await admin_ctx.session.commit()
    _set_non_super_actor(admin_ctx, perms={"system:user:add"})

    resp = await admin_ctx.client.post(
        "/admin/users",
        json={
            "username": "create-mask",
            "employee_no": "E-2160",
            "department_id": "2160",
            "email": "alice@example.com",
            "mobile": "13800001111",
        },
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["email"] == "a***@example.com"
    assert data["mobile"] == "138****1111"


async def test_update_user_readback_masks_pii_for_non_superuser(
    admin_ctx: AdminCtx,
) -> None:
    """The update readback must mask contacts for a non-super editor — a no-op
    edit must not leak unmasked email/mobile of a user."""
    admin_ctx.session.add_all(
        [
            Department(id=2170, name="d2170", parent_id=None),
            User(
                id=2171,
                username="upd-mask",
                employee_no="E-2171",
                department_id=2170,
                email="bob@example.com",
                mobile="13900002222",
            ),
        ]
    )
    await admin_ctx.session.commit()
    _set_non_super_actor(admin_ctx, perms={"system:user:edit"})

    resp = await admin_ctx.client.put(
        "/admin/users/2171", json={"real_name": "Bob R."}
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["email"] == "b***@example.com"
    assert data["mobile"] == "139****2222"


async def test_list_users_superuser_sees_unmasked_pii(admin_ctx: AdminCtx) -> None:
    created = await admin_ctx.client.post(
        "/admin/users",
        json={
            "username": "pii-super",
            "employee_no": "E-2130",
            "email": "bob@example.com",
            "mobile": "13900002222",
        },
    )
    assert created.status_code == 200, created.text

    resp = await admin_ctx.client.get("/admin/users?keyword=pii-super")
    assert resp.status_code == 200, resp.text
    listed = resp.json()["data"]["items"][0]
    assert listed["mobile"] == "13900002222"
    assert listed["email"] == "bob@example.com"


async def test_batch_delete_users_skips_undominated(admin_ctx: AdminCtx) -> None:
    """Batch delete skips targets the actor cannot dominate (one holding a role
    that confers a permission the actor lacks), deleting only the dominated one
    and clearing links only for it."""
    menu = Menu(name="m.esc", menu_type="button", perms="*:*:*", status="active")
    power_role = Role(name="power-bd", code="power-bd", status="active")
    dominated = User(
        id=2141, username="batch-in", employee_no="E-2141", department_id=2140
    )
    undominated = User(
        id=2142, username="batch-out", employee_no="E-2142", department_id=2142
    )
    admin_ctx.session.add_all([menu, power_role, dominated, undominated])
    await admin_ctx.session.flush()
    admin_ctx.session.add_all(
        [
            RoleMenu(role_id=power_role.id, menu_id=menu.id),
            UserRole(user_id=undominated.id, role_id=power_role.id),
        ]
    )
    await admin_ctx.session.commit()
    _set_non_super_actor(admin_ctx, perms={"system:user:remove"})

    resp = await admin_ctx.client.post(
        "/admin/users/batch-delete",
        json={"ids": [str(dominated.id), str(undominated.id)]},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data == {
        "requested": 2,
        "affected": 1,
        "skipped_ids": [str(undominated.id)],
    }

    await admin_ctx.session.refresh(dominated)
    await admin_ctx.session.refresh(undominated)
    assert dominated.is_deleted is True
    assert undominated.is_deleted is False
    links = await admin_ctx.session.scalars(
        select(UserRole.user_id)
        .where(UserRole.user_id.in_([dominated.id, undominated.id]))
        .order_by(UserRole.user_id)
    )
    assert list(links.all()) == [undominated.id]


async def test_batch_delete_users_all_skipped(admin_ctx: AdminCtx) -> None:
    menu = Menu(name="m.esc2", menu_type="button", perms="*:*:*", status="active")
    power_role = Role(name="power-as", code="power-as", status="active")
    undominated = User(
        id=2152, username="batch-all-skip", employee_no="E-2152", department_id=2152
    )
    admin_ctx.session.add_all([menu, power_role, undominated])
    await admin_ctx.session.flush()
    admin_ctx.session.add_all(
        [
            RoleMenu(role_id=power_role.id, menu_id=menu.id),
            UserRole(user_id=undominated.id, role_id=power_role.id),
        ]
    )
    await admin_ctx.session.commit()
    _set_non_super_actor(admin_ctx, perms={"system:user:remove"})

    resp = await admin_ctx.client.post(
        "/admin/users/batch-delete", json={"ids": [str(undominated.id)]}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"] == {
        "requested": 1,
        "affected": 0,
        "skipped_ids": [str(undominated.id)],
    }
    await admin_ctx.session.refresh(undominated)
    assert undominated.is_deleted is False


async def test_non_superuser_cannot_assign_role_granting_unheld_perms(
    admin_ctx: AdminCtx,
) -> None:
    """CRITICAL-2: privilege-escalation guard on role assignment."""
    # A powerful role granting a permission the actor does NOT hold.
    powerful = Role(name="power", code="power", status="active")
    menu = Menu(
        name="m.super", menu_type="button", perms="*:*:*", status="active"
    )
    admin_ctx.session.add_all([powerful, menu])
    await admin_ctx.session.flush()
    admin_ctx.session.add(RoleMenu(role_id=powerful.id, menu_id=menu.id))
    await admin_ctx.session.commit()

    # Actor can add users but is NOT a superuser.
    admin_ctx.state.perms = {"system:user:add"}

    resp = await admin_ctx.client.post(
        "/admin/users",
        json={
            "username": "escalate",
            "employee_no": "E-esc",
            "role_ids": [str(powerful.id)],
        },
    )
    assert resp.status_code == 403
    assert resp.json()["code"] == "auth.forbidden"


async def test_superuser_can_assign_any_role(admin_ctx: AdminCtx) -> None:
    """Counterpart: a super-admin (``*:*:*``) may assign a powerful role."""
    powerful = Role(name="power2", code="power2", status="active")
    menu = Menu(name="m.s2", menu_type="button", perms="*:*:*", status="active")
    admin_ctx.session.add_all([powerful, menu])
    await admin_ctx.session.flush()
    admin_ctx.session.add(RoleMenu(role_id=powerful.id, menu_id=menu.id))
    await admin_ctx.session.commit()

    # Default actor has *:*:* (superuser).
    resp = await admin_ctx.client.post(
        "/admin/users",
        json={
            "username": "blessed",
            "employee_no": "E-bless",
            "role_ids": [str(powerful.id)],
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["role_ids"] == [str(powerful.id)]


async def test_reset_password_succeeds_and_changes_hash(admin_ctx: AdminCtx) -> None:
    created = await admin_ctx.client.post(
        "/admin/users",
        json={"username": "rstroute", "employee_no": "E-rstr", "password": "old12345"},
    )
    user_id = created.json()["data"]["id"]
    before = await admin_ctx.session.get(User, int(user_id))
    assert before is not None
    old_hash = before.password

    resp = await admin_ctx.client.post(
        f"/admin/users/{user_id}/reset-password", json={"password": "new12345"}
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["success"] is True
    assert body["data"] is None  # empty success envelope, no password echoed
    await admin_ctx.session.refresh(before)
    assert before.password != old_hash  # rehashed


async def test_reset_password_requires_permission(admin_ctx: AdminCtx) -> None:
    created = await admin_ctx.client.post(
        "/admin/users",
        json={"username": "gated", "employee_no": "E-gate", "password": "old12345"},
    )
    user_id = created.json()["data"]["id"]
    # Drop to a non-superuser actor lacking the reset perm.
    admin_ctx.state.perms = {"system:user:list"}

    resp = await admin_ctx.client.post(
        f"/admin/users/{user_id}/reset-password", json={"password": "new12345"}
    )

    assert resp.status_code == 403


async def test_reset_password_weak_rejected(admin_ctx: AdminCtx) -> None:
    created = await admin_ctx.client.post(
        "/admin/users",
        json={"username": "weakrt", "employee_no": "E-wkr", "password": "old12345"},
    )
    user_id = created.json()["data"]["id"]

    resp = await admin_ctx.client.post(
        f"/admin/users/{user_id}/reset-password", json={"password": "short"}
    )

    assert resp.status_code == 400
    body = resp.json()
    assert body["success"] is False
    assert body["code"] == "auth.password_too_weak"
