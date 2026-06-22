"""Direct unit tests for GrantService (service layer, no HTTP)."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from src.admin.grants.schemas import GrantCreate, GrantUpdate
from src.admin.grants.service import GrantService
from src.auth.constants import SUPERADMIN_ROLE_CODE
from src.auth.service import AuthenticatedUser
from src.core.query import ListQuery
from src.exceptions import AppError

pytestmark = pytest.mark.asyncio

ACTOR = AuthenticatedUser(
    user_id=1000,
    username="admin",
    department_id=10,
    permissions=frozenset({"*:*:*"}),
    role_codes=frozenset({SUPERADMIN_ROLE_CODE}),
)


def _grant_payload(**overrides: object) -> GrantCreate:
    defaults: dict[str, object] = {
        "scope": "user",
        "scope_id": 1,
        "logical_model_id": 101,
    }
    defaults.update(overrides)
    return GrantCreate(**defaults)


async def test_create_grant_and_get_grant(admin_session: AsyncSession) -> None:
    svc = GrantService(admin_session)
    grant = await svc.create_grant(_grant_payload(), actor=ACTOR)

    fetched = await svc.get_grant(grant.id)

    assert fetched.id == grant.id
    assert fetched.scope == "user"


async def test_create_grant_duplicate_scope_model_rejected(
    admin_session: AsyncSession,
) -> None:
    svc = GrantService(admin_session)
    await svc.create_grant(_grant_payload(), actor=ACTOR)

    with pytest.raises(AppError) as exc:
        await svc.create_grant(_grant_payload(), actor=ACTOR)

    assert exc.value.status_code == 400


async def test_create_grant_default_clears_old_default(
    admin_session: AsyncSession,
) -> None:
    svc = GrantService(admin_session)
    old = await svc.create_grant(
        _grant_payload(logical_model_id=101, is_default=True), actor=ACTOR
    )
    await svc.update_grant(old.id, GrantUpdate(is_default=False), actor=ACTOR)

    new = await svc.create_grant(
        _grant_payload(logical_model_id=102, is_default=True), actor=ACTOR
    )
    await admin_session.refresh(old)

    assert old.is_default is False
    assert new.is_default is True


async def test_update_grant_default_toggle_clears_old_default(
    admin_session: AsyncSession,
) -> None:
    svc = GrantService(admin_session)
    old = await svc.create_grant(
        _grant_payload(logical_model_id=101, is_default=True), actor=ACTOR
    )
    await svc.update_grant(old.id, GrantUpdate(is_default=False), actor=ACTOR)
    new = await svc.create_grant(
        _grant_payload(logical_model_id=102, is_default=False), actor=ACTOR
    )
    await svc.update_grant(old.id, GrantUpdate(is_default=True), actor=ACTOR)

    updated = await svc.update_grant(new.id, GrantUpdate(is_default=True), actor=ACTOR)
    await admin_session.refresh(old)

    assert updated.is_default is True
    assert old.is_default is False


async def test_delete_grant_soft_deletes(admin_session: AsyncSession) -> None:
    svc = GrantService(admin_session)
    grant = await svc.create_grant(_grant_payload(), actor=ACTOR)

    await svc.delete_grant(grant.id, actor=ACTOR)

    with pytest.raises(AppError) as exc:
        await svc.get_grant(grant.id)

    assert exc.value.status_code == 404


async def test_list_grants_filtered_by_scope(admin_session: AsyncSession) -> None:
    svc = GrantService(admin_session)
    await svc.create_grant(_grant_payload(scope="user", scope_id=1), actor=ACTOR)
    await svc.create_grant(
        _grant_payload(scope="department", scope_id=10, logical_model_id=102),
        actor=ACTOR,
    )

    result = await svc.list_grants(scope="department", query=ListQuery())

    assert result.total == 1
    assert result.items[0].scope == "department"
