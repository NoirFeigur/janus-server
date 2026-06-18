"""Direct tests for the standalone admin audit repository."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from src.admin.audit.repository import AuditRepository
from src.db.models.audit import LoginLog, OperLog
from src.enums import AuditOutcome, LoginFailureReason


async def test_oper_log_repository_filters_counts_paginates_and_orders(
    admin_session: AsyncSession,
) -> None:
    repo = AuditRepository(admin_session)
    base_time = datetime(2026, 1, 1, tzinfo=UTC)

    first = await repo.append_oper_log(
        OperLog(
            actor_id=1,
            actor_name="Alice",
            module="user",
            action="create",
            method="POST",
            path="/admin/users",
            status=AuditOutcome.success.value,
            created_at=base_time,
        )
    )
    second = await repo.append_oper_log(
        OperLog(
            actor_id=2,
            actor_name="Bob",
            module="role",
            action="delete",
            method="DELETE",
            path="/admin/roles/5",
            status=AuditOutcome.failure.value,
            error_code="auth.forbidden",
            created_at=base_time + timedelta(seconds=1),
        )
    )
    third = await repo.append_oper_log(
        OperLog(
            actor_id=1,
            actor_name="Carol",
            module="user",
            action="update",
            method="PUT",
            path="/admin/users/9",
            status=AuditOutcome.failure.value,
            created_at=base_time + timedelta(seconds=2),
        )
    )
    await admin_session.commit()

    rows = await repo.list_oper_logs(limit=10, offset=0)
    assert [row.id for row in rows] == [third.id, second.id, first.id]
    assert await repo.count_oper_logs() == 3

    user_rows = await repo.list_oper_logs(module="user", limit=10, offset=0)
    assert [row.id for row in user_rows] == [third.id, first.id]
    assert await repo.count_oper_logs(module="user") == 2

    actor_failures = await repo.list_oper_logs(
        status=AuditOutcome.failure.value,
        actor_id=1,
        limit=10,
        offset=0,
    )
    assert [row.id for row in actor_failures] == [third.id]
    assert await repo.count_oper_logs(status=AuditOutcome.failure.value, actor_id=1) == 1

    actor_keyword = await repo.list_oper_logs(keyword="ali", limit=10, offset=0)
    assert [row.id for row in actor_keyword] == [first.id]
    path_keyword = await repo.list_oper_logs(keyword="ROLES", limit=10, offset=0)
    assert [row.id for row in path_keyword] == [second.id]

    page = await repo.list_oper_logs(limit=1, offset=1)
    assert [row.id for row in page] == [second.id]


async def test_login_log_repository_filters_counts_paginates_and_orders(
    admin_session: AsyncSession,
) -> None:
    repo = AuditRepository(admin_session)
    base_time = datetime(2026, 1, 2, tzinfo=UTC)

    first = await repo.append_login_log(
        LoginLog(
            user_id=10,
            username="alice",
            status=AuditOutcome.success.value,
            request_ip="127.0.0.1",
            user_agent="pytest",
            trace_id="login-1",
            created_at=base_time,
        )
    )
    second = await repo.append_login_log(
        LoginLog(
            user_id=None,
            username="bob",
            status=AuditOutcome.failure.value,
            failure_reason=LoginFailureReason.user_not_found.value,
            created_at=base_time + timedelta(seconds=1),
        )
    )
    third = await repo.append_login_log(
        LoginLog(
            user_id=10,
            username="alice.mobile",
            status=AuditOutcome.failure.value,
            failure_reason=LoginFailureReason.bad_credentials.value,
            created_at=base_time + timedelta(seconds=2),
        )
    )
    await admin_session.commit()

    rows = await repo.list_login_logs(limit=10, offset=0)
    assert [row.id for row in rows] == [third.id, second.id, first.id]
    assert await repo.count_login_logs() == 3

    alice_rows = await repo.list_login_logs(keyword="ALICE", limit=10, offset=0)
    assert [row.id for row in alice_rows] == [third.id, first.id]
    assert await repo.count_login_logs(keyword="alice") == 2

    user_failures = await repo.list_login_logs(
        status=AuditOutcome.failure.value,
        user_id=10,
        limit=10,
        offset=0,
    )
    assert [row.id for row in user_failures] == [third.id]
    assert await repo.count_login_logs(status=AuditOutcome.failure.value, user_id=10) == 1

    page = await repo.list_login_logs(limit=1, offset=1)
    assert [row.id for row in page] == [second.id]
