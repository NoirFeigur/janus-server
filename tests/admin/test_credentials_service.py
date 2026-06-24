"""Direct unit tests for CredentialService (service layer, no HTTP)."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from src.admin.credentials.schemas import ApiKeyCreate, ApiKeyUpdate
from src.admin.credentials.service import CredentialService
from src.auth.constants import SUPERADMIN_ROLE_CODE
from src.auth.service import AuthenticatedUser
from src.core.query import ListQuery
from src.db.models.identity import Role, User, UserRole
from src.enums import DataScope
from src.exceptions import AppError

pytestmark = pytest.mark.asyncio

ACTOR = AuthenticatedUser(
    user_id=1000,
    username="admin",
    department_id=10,
    permissions=frozenset({"*:*:*"}),
    role_codes=frozenset({SUPERADMIN_ROLE_CODE}),
)

# Unrestricted data scope (via an all_data role) but NOT superadmin and WITHOUT
# the dedicated ai:credential:issue grant. Passes _require_owner_in_scope for any
# target user, so it isolates the M3-2 cross-user issuance gate.
UNRESTRICTED_NON_SUPER = AuthenticatedUser(
    user_id=2000,
    username="scoped-admin",
    department_id=20,
    permissions=frozenset({"ai:credential:add"}),
    role_codes=frozenset(),
)

# Same as above but carrying the dedicated issuance grant: allowed to mint for
# others.
ISSUER = AuthenticatedUser(
    user_id=2000,
    username="issuer-admin",
    department_id=20,
    permissions=frozenset({"ai:credential:add", "ai:credential:issue"}),
    role_codes=frozenset(),
)


def _key_payload(**overrides: object) -> ApiKeyCreate:
    defaults: dict[str, object] = {
        "user_id": 1,
        "name": "local-dev",
    }
    defaults.update(overrides)
    return ApiKeyCreate(**defaults)


async def _seed_users(session: AsyncSession) -> None:
    session.add_all(
        [
            User(id=1, username="alice", employee_no="E001", status="active"),
            User(id=2, username="bob", employee_no="E002", status="active"),
        ]
    )
    await session.commit()


async def _seed_unrestricted_role(session: AsyncSession, *, user_id: int) -> None:
    """Give ``user_id`` an active all_data role (unrestricted, non-superadmin)."""
    session.add(
        Role(
            id=900,
            name="平台管理员",
            code="platform_admin",
            status="active",
            data_scope=DataScope.all_data.value,
        )
    )
    session.add(
        User(id=user_id, username="scoped-admin", employee_no="E900", status="active")
    )
    await session.flush()
    session.add(UserRole(user_id=user_id, role_id=900))
    await session.commit()


async def test_create_key_returns_plain_key(admin_session: AsyncSession) -> None:
    await _seed_users(admin_session)
    svc = CredentialService(admin_session)

    _, plain_key = await svc.create_key(_key_payload(), actor=ACTOR)

    assert plain_key.startswith("sk-")


async def test_create_key_stores_hash_not_plaintext(
    admin_session: AsyncSession,
) -> None:
    await _seed_users(admin_session)
    svc = CredentialService(admin_session)

    key, plain_key = await svc.create_key(_key_payload(), actor=ACTOR)

    assert key.key_hash != plain_key
    assert len(key.key_hash) == 64


async def test_create_key_sets_prefix(admin_session: AsyncSession) -> None:
    await _seed_users(admin_session)
    svc = CredentialService(admin_session)

    key, plain_key = await svc.create_key(_key_payload(), actor=ACTOR)

    assert key.key_prefix == plain_key[:8]


async def test_list_keys_by_user_id(admin_session: AsyncSession) -> None:
    await _seed_users(admin_session)
    svc = CredentialService(admin_session)
    await svc.create_key(_key_payload(user_id=1, name="first"), actor=ACTOR)
    await svc.create_key(_key_payload(user_id=2, name="second"), actor=ACTOR)

    result = await svc.list_keys(user_id=2, query=ListQuery(), actor=ACTOR)

    assert result.total == 1
    assert result.items[0].user_id == 2


async def test_update_key_status_to_disabled(admin_session: AsyncSession) -> None:
    await _seed_users(admin_session)
    svc = CredentialService(admin_session)
    key, _ = await svc.create_key(_key_payload(), actor=ACTOR)

    updated = await svc.update_key(
        key.id, ApiKeyUpdate(status="disabled"), actor=ACTOR
    )

    assert updated.status == "disabled"


async def test_delete_key_soft_deletes(admin_session: AsyncSession) -> None:
    await _seed_users(admin_session)
    svc = CredentialService(admin_session)
    key, _ = await svc.create_key(_key_payload(), actor=ACTOR)

    await svc.delete_key(key.id, actor=ACTOR)

    with pytest.raises(AppError) as exc:
        await svc.get_key(key.id, actor=ACTOR)

    assert exc.value.status_code == 404


async def test_create_key_for_other_user_without_issue_perm_forbidden(
    admin_session: AsyncSession,
) -> None:
    # Actor has unrestricted scope (all_data role) so it passes the owner-in-scope
    # check, but is neither superadmin nor holds ai:credential:issue. Minting a key
    # for another user (alice, id=1) must be rejected (M3-2).
    await _seed_users(admin_session)
    await _seed_unrestricted_role(admin_session, user_id=UNRESTRICTED_NON_SUPER.user_id)
    svc = CredentialService(admin_session)

    with pytest.raises(AppError) as exc:
        await svc.create_key(
            _key_payload(user_id=1, name="for-alice"), actor=UNRESTRICTED_NON_SUPER
        )

    assert exc.value.status_code == 403


async def test_create_key_for_self_allowed_without_issue_perm(
    admin_session: AsyncSession,
) -> None:
    # Issuing one's OWN key needs no dedicated grant — only the base add perm and
    # in-scope ownership (self is always in scope here via all_data).
    await _seed_unrestricted_role(admin_session, user_id=UNRESTRICTED_NON_SUPER.user_id)
    svc = CredentialService(admin_session)

    key, plain_key = await svc.create_key(
        _key_payload(user_id=UNRESTRICTED_NON_SUPER.user_id, name="own"),
        actor=UNRESTRICTED_NON_SUPER,
    )

    assert plain_key.startswith("sk-")
    assert key.user_id == UNRESTRICTED_NON_SUPER.user_id


async def test_create_key_for_other_user_with_issue_perm_allowed(
    admin_session: AsyncSession,
) -> None:
    # The dedicated ai:credential:issue grant unlocks cross-user issuance.
    await _seed_users(admin_session)
    await _seed_unrestricted_role(admin_session, user_id=ISSUER.user_id)
    svc = CredentialService(admin_session)

    key, plain_key = await svc.create_key(
        _key_payload(user_id=1, name="for-alice"), actor=ISSUER
    )

    assert plain_key.startswith("sk-")
    assert key.user_id == 1
