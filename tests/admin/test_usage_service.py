"""Direct unit tests for UsageService (service layer, no HTTP)."""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from src.admin.usage.service import UsageService
from src.auth.constants import SUPERADMIN_ROLE_CODE
from src.auth.service import AuthenticatedUser
from src.core.query import ListQuery
from src.db.models.usage import UsageRecord

pytestmark = pytest.mark.asyncio

ACTOR = AuthenticatedUser(
    user_id=1000,
    username="admin",
    department_id=10,
    permissions=frozenset({"*:*:*"}),
    role_codes=frozenset({SUPERADMIN_ROLE_CODE}),
)


async def test_list_records_empty_returns_zero(admin_session: AsyncSession) -> None:
    svc = UsageService(admin_session)

    result = await svc.list_records(query=ListQuery(), actor=ACTOR)

    assert result.total == 0
    assert result.items == []


async def test_insert_record_directly_then_list_returns_it(
    admin_session: AsyncSession,
) -> None:
    svc = UsageService(admin_session)
    record = UsageRecord(
        user_id=1,
        logical_model_id=1,
        prompt_tokens=100,
        completion_tokens=50,
        total_tokens=150,
        status="success",
        latency_ms=200,
    )
    admin_session.add(record)
    await admin_session.flush()

    result = await svc.list_records(query=ListQuery(), actor=ACTOR)

    assert result.total == 1
    assert result.items[0].id == record.id


async def test_get_stats_with_records_totals_match(
    admin_session: AsyncSession,
) -> None:
    svc = UsageService(admin_session)
    admin_session.add_all(
        [
            UsageRecord(
                user_id=1,
                logical_model_id=1,
                prompt_tokens=100,
                completion_tokens=50,
                total_tokens=150,
                cost=Decimal("0.001"),
                status="success",
                latency_ms=200,
            ),
            UsageRecord(
                user_id=1,
                logical_model_id=1,
                prompt_tokens=10,
                completion_tokens=5,
                total_tokens=15,
                cost=Decimal("0.002"),
                status="error",
                latency_ms=300,
            ),
        ]
    )
    await admin_session.flush()

    stats = await svc.get_stats(user_id=1, actor=ACTOR)

    assert stats.total_requests == 2
    assert stats.total_tokens == 165
    assert stats.total_cost == Decimal("0.003000")
    assert stats.avg_latency_ms == 250.0
    assert stats.success_count == 1
    assert stats.error_count == 1
    assert stats.error_rate == 50.0


async def test_list_records_filter_by_user_id(admin_session: AsyncSession) -> None:
    svc = UsageService(admin_session)
    admin_session.add_all(
        [
            UsageRecord(
                user_id=1,
                logical_model_id=1,
                prompt_tokens=100,
                completion_tokens=50,
                total_tokens=150,
                status="success",
                latency_ms=200,
            ),
            UsageRecord(
                user_id=2,
                logical_model_id=1,
                prompt_tokens=20,
                completion_tokens=10,
                total_tokens=30,
                status="success",
                latency_ms=100,
            ),
        ]
    )
    await admin_session.flush()

    result = await svc.list_records(user_id=2, query=ListQuery(), actor=ACTOR)

    assert result.total == 1
    assert result.items[0].user_id == 2
