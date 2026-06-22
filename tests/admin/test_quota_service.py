"""Direct unit tests for QuotaService (service layer, no HTTP)."""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from src.admin.quota.schemas import QuotaCreate, QuotaUpdate
from src.admin.quota.service import QuotaService
from src.auth.constants import SUPERADMIN_ROLE_CODE
from src.auth.service import AuthenticatedUser
from src.exceptions import AppError

pytestmark = pytest.mark.asyncio

ACTOR = AuthenticatedUser(
    user_id=1000,
    username="admin",
    department_id=10,
    permissions=frozenset({"*:*:*"}),
    role_codes=frozenset({SUPERADMIN_ROLE_CODE}),
)


def _quota_payload(**overrides: object) -> QuotaCreate:
    defaults: dict[str, object] = {
        "scope": "user",
        "scope_id": 1,
        "logical_model_id": 101,
        "period": "daily",
        "metric": "tokens",
        "limit_value": Decimal("1000"),
    }
    defaults.update(overrides)
    return QuotaCreate(**defaults)


async def test_create_quota_and_get_quota(admin_session: AsyncSession) -> None:
    svc = QuotaService(admin_session)
    quota = await svc.create_quota(_quota_payload(), actor=ACTOR)

    fetched = await svc.get_quota(quota.id)

    assert fetched.id == quota.id
    assert fetched.limit_value == Decimal("1000.000000")


async def test_create_quota_duplicate_conflicts(admin_session: AsyncSession) -> None:
    svc = QuotaService(admin_session)
    await svc.create_quota(_quota_payload(), actor=ACTOR)

    with pytest.raises(AppError) as exc:
        await svc.create_quota(_quota_payload(), actor=ACTOR)

    assert exc.value.status_code == 409


async def test_create_quota_global_with_scope_id_rejected(
    admin_session: AsyncSession,
) -> None:
    svc = QuotaService(admin_session)

    with pytest.raises(AppError) as exc:
        await svc.create_quota(
            _quota_payload(scope="global", scope_id=1, logical_model_id=None),
            actor=ACTOR,
        )

    assert exc.value.status_code == 400


async def test_create_quota_user_without_scope_id_rejected(
    admin_session: AsyncSession,
) -> None:
    svc = QuotaService(admin_session)

    with pytest.raises(AppError) as exc:
        await svc.create_quota(_quota_payload(scope="user", scope_id=None), actor=ACTOR)

    assert exc.value.status_code == 400


async def test_update_quota_limit_value(admin_session: AsyncSession) -> None:
    svc = QuotaService(admin_session)
    quota = await svc.create_quota(_quota_payload(), actor=ACTOR)

    updated = await svc.update_quota(
        quota.id,
        QuotaUpdate(limit_value=Decimal("2500")),
        actor=ACTOR,
    )

    assert updated.limit_value == Decimal("2500.000000")


async def test_delete_quota_soft_deletes(admin_session: AsyncSession) -> None:
    svc = QuotaService(admin_session)
    quota = await svc.create_quota(_quota_payload(), actor=ACTOR)

    await svc.delete_quota(quota.id, actor=ACTOR)

    with pytest.raises(AppError) as exc:
        await svc.get_quota(quota.id)

    assert exc.value.status_code == 404
