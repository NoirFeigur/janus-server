"""Direct unit tests for CredentialService (service layer, no HTTP)."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from src.admin.credentials.schemas import ApiKeyCreate, ApiKeyUpdate
from src.admin.credentials.service import CredentialService
from src.auth.constants import SUPERADMIN_ROLE_CODE
from src.auth.service import AuthenticatedUser
from src.core.query import ListQuery
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

    result = await svc.list_keys(user_id=2, query=ListQuery())

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
        await svc.get_key(key.id)

    assert exc.value.status_code == 404
