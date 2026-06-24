"""Validation regressions for admin rate-limit rule creation (M3-4).

``RateLimitRuleCreate`` accepted an arbitrary ``subject_type`` string, allowed a
missing ``subject_id`` for scoped rules (and a stray one for global), permitted
negative limits, and the service let any admin mint a platform-wide (``global``)
rule against a non-existent subject. These tests pin the hardened contract:

- schema: ``subject_type`` must be a valid ``RateLimitScope``; ``subject_id`` is
  required for scoped rules and forbidden for global; limits must be >= 1.
- service: ``global`` rules are super-admin only; scoped subjects must exist.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from src.admin.rate_limits.schemas import RateLimitRuleCreate, RateLimitRuleUpdate
from src.admin.rate_limits.service import RateLimitService
from src.auth.constants import SUPERADMIN_ROLE_CODE
from src.auth.service import AuthenticatedUser, DataScopeFilter
from src.core.query import ListQuery
from src.db.base import Base
from src.db.models.credential import ApiKey
from src.db.models.identity import Department, User
from src.db.models.rate_limit import RateLimitRule
from src.exceptions import AppError

_TABLES = [
    Base.metadata.tables[RateLimitRule.__tablename__],
    Base.metadata.tables[User.__tablename__],
    Base.metadata.tables[Department.__tablename__],
    Base.metadata.tables[ApiKey.__tablename__],
]

SUPER_ACTOR = AuthenticatedUser(
    user_id=999,
    username="root",
    department_id=None,
    permissions=frozenset({"*:*:*"}),
    role_codes=frozenset({SUPERADMIN_ROLE_CODE}),
)

# Holds the add perm but is NOT super-admin: must be blocked from global rules.
SCOPED_ACTOR = AuthenticatedUser(
    user_id=2000,
    username="scoped-admin",
    department_id=20,
    permissions=frozenset({"ai:rate_limit:add"}),
    role_codes=frozenset(),
)


def _scoped_service(session: AsyncSession) -> RateLimitService:
    service = RateLimitService(session)
    service.auth.resolve_data_scope = AsyncMock(
        return_value=DataScopeFilter(
            unrestricted=False,
            department_ids=frozenset({20}),
            include_self=True,
        )
    )
    return service


@pytest_asyncio.fixture
async def db_session(
    sqlite_engine: AsyncEngine,
    sqlite_session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    async with sqlite_engine.begin() as conn:
        await conn.run_sync(
            lambda sync_conn: Base.metadata.create_all(sync_conn, tables=_TABLES)
        )
    async with sqlite_session_factory() as session:
        yield session


async def _seed_subjects(session: AsyncSession) -> None:
    session.add(User(id=1, username="alice", employee_no="E001", status="active"))
    session.add(Department(id=10, name="平台组"))
    session.add(
        ApiKey(
            id=100,
            user_id=1,
            name="k",
            key_hash="h" * 64,
            key_prefix="sk-aaaa",
            status="active",
        )
    )
    await session.flush()


# --- schema-level validation ------------------------------------------------


def test_invalid_subject_type_rejected() -> None:
    with pytest.raises(ValidationError):
        RateLimitRuleCreate(subject_type="organization", subject_id=1, rpm_limit=60)


def test_scoped_rule_without_subject_id_rejected() -> None:
    with pytest.raises(ValidationError):
        RateLimitRuleCreate(subject_type="user", subject_id=None, rpm_limit=60)


def test_global_rule_with_subject_id_rejected() -> None:
    with pytest.raises(ValidationError):
        RateLimitRuleCreate(subject_type="global", subject_id=5, rpm_limit=60)


def test_negative_rpm_limit_rejected() -> None:
    with pytest.raises(ValidationError):
        RateLimitRuleCreate(subject_type="user", subject_id=1, rpm_limit=-1)


def test_zero_max_concurrent_rejected() -> None:
    with pytest.raises(ValidationError):
        RateLimitRuleCreate(subject_type="user", subject_id=1, max_concurrent=0)


# --- service-level validation -----------------------------------------------


@pytest.mark.asyncio
async def test_global_rule_forbidden_for_non_superuser(
    db_session: AsyncSession,
) -> None:
    service = RateLimitService(db_session)
    payload = RateLimitRuleCreate(subject_type="global", subject_id=None, rpm_limit=60)

    with pytest.raises(AppError) as exc:
        await service.create_rule(payload, actor=SCOPED_ACTOR)

    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_global_rule_allowed_for_superuser(db_session: AsyncSession) -> None:
    service = RateLimitService(db_session)
    payload = RateLimitRuleCreate(subject_type="global", subject_id=None, rpm_limit=60)

    rule = await service.create_rule(payload, actor=SUPER_ACTOR)

    assert rule.subject_type == "global"
    assert rule.subject_id is None


@pytest.mark.asyncio
async def test_user_rule_nonexistent_subject_rejected(
    db_session: AsyncSession,
) -> None:
    service = RateLimitService(db_session)
    payload = RateLimitRuleCreate(subject_type="user", subject_id=4242, rpm_limit=60)

    with pytest.raises(AppError) as exc:
        await service.create_rule(payload, actor=SUPER_ACTOR)

    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_user_rule_existing_subject_created(db_session: AsyncSession) -> None:
    await _seed_subjects(db_session)
    service = RateLimitService(db_session)
    payload = RateLimitRuleCreate(subject_type="user", subject_id=1, rpm_limit=60)

    rule = await service.create_rule(payload, actor=SUPER_ACTOR)

    assert rule.subject_type == "user"
    assert rule.subject_id == 1


@pytest.mark.asyncio
async def test_department_rule_existing_subject_created(
    db_session: AsyncSession,
) -> None:
    await _seed_subjects(db_session)
    service = RateLimitService(db_session)
    payload = RateLimitRuleCreate(
        subject_type="department", subject_id=10, rpm_limit=60
    )

    rule = await service.create_rule(payload, actor=SUPER_ACTOR)

    assert rule.subject_type == "department"
    assert rule.subject_id == 10


@pytest.mark.asyncio
async def test_api_key_rule_existing_subject_created(
    db_session: AsyncSession,
) -> None:
    await _seed_subjects(db_session)
    service = RateLimitService(db_session)
    payload = RateLimitRuleCreate(
        subject_type="api_key", subject_id=100, rpm_limit=60
    )

    rule = await service.create_rule(payload, actor=SUPER_ACTOR)

    assert rule.subject_type == "api_key"
    assert rule.subject_id == 100


@pytest.mark.asyncio
async def test_scoped_actor_cannot_create_out_of_scope_user_rule(
    db_session: AsyncSession,
) -> None:
    await _seed_subjects(db_session)
    db_session.add(User(id=2, username="bob", employee_no="E002", department_id=99))
    await db_session.flush()
    service = _scoped_service(db_session)
    payload = RateLimitRuleCreate(subject_type="user", subject_id=2, rpm_limit=60)

    with pytest.raises(AppError) as exc:
        await service.create_rule(payload, actor=SCOPED_ACTOR)

    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_scoped_actor_lists_only_visible_rate_limit_rules(
    db_session: AsyncSession,
) -> None:
    await _seed_subjects(db_session)
    db_session.add(User(id=2, username="bob", employee_no="E002", department_id=99))
    db_session.add_all(
        [
            RateLimitRule(subject_type="user", subject_id=2000, rpm_limit=10),
            RateLimitRule(subject_type="user", subject_id=2, rpm_limit=20),
            RateLimitRule(subject_type="global", subject_id=None, rpm_limit=30),
        ]
    )
    await db_session.flush()
    service = _scoped_service(db_session)

    page = await service.list_rules(ListQuery(limit=50), actor=SCOPED_ACTOR)

    assert [rule.subject_id for rule in page.items] == [2000]


@pytest.mark.asyncio
async def test_scoped_actor_cannot_update_out_of_scope_api_key_rule(
    db_session: AsyncSession,
) -> None:
    await _seed_subjects(db_session)
    db_session.add(User(id=2, username="bob", employee_no="E002", department_id=99))
    db_session.add(
        ApiKey(
            id=101,
            user_id=2,
            name="k2",
            key_hash="i" * 64,
            key_prefix="sk-bbbb",
            status="active",
        )
    )
    rule = RateLimitRule(subject_type="api_key", subject_id=101, rpm_limit=10)
    db_session.add(rule)
    await db_session.flush()
    service = _scoped_service(db_session)

    with pytest.raises(AppError) as exc:
        await service.update_rule(rule.id, RateLimitRuleUpdate(rpm_limit=20), actor=SCOPED_ACTOR)

    assert exc.value.status_code == 403
