"""Direct unit tests for QuotaService (service layer, no HTTP)."""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from src.admin.quota.schemas import QuotaCreate, QuotaUpdate
from src.admin.quota.service import QuotaService
from src.auth.constants import SUPERADMIN_ROLE_CODE
from src.auth.service import AuthenticatedUser
from src.db.models.identity import Role, User, UserRole
from src.db.models.model_catalog import LogicalModel
from src.exceptions import AppError

pytestmark = pytest.mark.asyncio

ACTOR = AuthenticatedUser(
    user_id=1000,
    username="admin",
    department_id=10,
    permissions=frozenset({"*:*:*"}),
    role_codes=frozenset({SUPERADMIN_ROLE_CODE}),
)

# A non-superadmin actor with the quota list perm. Sees every user/department
# quota, yet platform-level `global` quotas must stay hidden (superuser-only) — it
# isolates the M3-5 global-leak gate on the list path.
UNRESTRICTED_NON_SUPER = AuthenticatedUser(
    user_id=2000,
    username="scoped-admin",
    department_id=20,
    permissions=frozenset({"ai:quota:list"}),
    role_codes=frozenset(),
)


async def _seed_unrestricted_role(session: AsyncSession, *, user_id: int) -> None:
    """Give ``user_id`` an active role (non-superadmin)."""
    session.add(
        Role(
            id=900,
            name="platform-admin",
            code="platform_admin",
            status="active",
        )
    )
    session.add(
        User(id=user_id, username="scoped-admin", employee_no="E900", status="active")
    )
    await session.flush()
    session.add(UserRole(user_id=user_id, role_id=900))
    await session.commit()


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


async def _seed_user(session: AsyncSession) -> None:
    session.add_all(
        [
            User(id=1, username="alice", employee_no="E001", status="active"),
            LogicalModel(
                id=101,
                name="claude-sonnet",
                display_name="Claude Sonnet",
                status="active",
            ),
        ]
    )
    await session.commit()


async def test_create_quota_and_get_quota(admin_session: AsyncSession) -> None:
    await _seed_user(admin_session)
    svc = QuotaService(admin_session)
    quota = await svc.create_quota(_quota_payload(), actor=ACTOR)

    fetched = await svc.get_quota(quota.id, actor=ACTOR)

    assert fetched.id == quota.id
    assert fetched.limit_value == Decimal("1000.000000")


async def test_create_quota_duplicate_conflicts(admin_session: AsyncSession) -> None:
    await _seed_user(admin_session)
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
    await _seed_user(admin_session)
    svc = QuotaService(admin_session)
    quota = await svc.create_quota(_quota_payload(), actor=ACTOR)

    updated = await svc.update_quota(
        quota.id,
        QuotaUpdate(limit_value=Decimal("2500")),
        actor=ACTOR,
    )

    assert updated.limit_value == Decimal("2500.000000")


async def test_delete_quota_soft_deletes(admin_session: AsyncSession) -> None:
    await _seed_user(admin_session)
    svc = QuotaService(admin_session)
    quota = await svc.create_quota(_quota_payload(), actor=ACTOR)

    await svc.delete_quota(quota.id, actor=ACTOR)

    with pytest.raises(AppError) as exc:
        await svc.get_quota(quota.id, actor=ACTOR)

    assert exc.value.status_code == 404


async def test_list_quotas_unrestricted_non_super_excludes_global(
    admin_session: AsyncSession,
) -> None:
    """M3-5: a non-superuser lists every user/department quota but must NOT see
    platform-level `global` quotas on the list path."""
    await _seed_user(admin_session)
    await _seed_unrestricted_role(
        admin_session, user_id=UNRESTRICTED_NON_SUPER.user_id
    )
    svc = QuotaService(admin_session)
    user_quota = await svc.create_quota(_quota_payload(), actor=ACTOR)
    global_quota = await svc.create_quota(
        _quota_payload(scope="global", scope_id=None, logical_model_id=None),
        actor=ACTOR,
    )

    page = await svc.list_quotas(actor=UNRESTRICTED_NON_SUPER)

    visible_ids = {q.id for q in page.items}
    assert user_quota.id in visible_ids
    assert global_quota.id not in visible_ids
    assert page.total == 1


async def test_list_quotas_superuser_sees_global(
    admin_session: AsyncSession,
) -> None:
    """M3-5 control: the superuser still sees global quotas (include_global)."""
    await _seed_user(admin_session)
    svc = QuotaService(admin_session)
    user_quota = await svc.create_quota(_quota_payload(), actor=ACTOR)
    global_quota = await svc.create_quota(
        _quota_payload(scope="global", scope_id=None, logical_model_id=None),
        actor=ACTOR,
    )

    page = await svc.list_quotas(actor=ACTOR)

    visible_ids = {q.id for q in page.items}
    assert user_quota.id in visible_ids
    assert global_quota.id in visible_ids
    assert page.total == 2
