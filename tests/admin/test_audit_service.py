"""Direct unit tests for ``AuditQueryService`` (service layer, no HTTP).

The route-level tests (``test_audit_query.py``) exercise both endpoints but their
handler bodies are uncovered due to the ASGITransport coverage.py tracer issue on
CPython 3.11. Driving the service directly with ``await`` gets honest coverage of
the list_oper_logs and list_login_logs code paths.
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from src.admin.audit.service import AuditQueryService
from src.core.query import ListQuery
from src.db.models.audit import LoginLog, OperLog

pytestmark = pytest.mark.asyncio


def _oper(*, module: str = "user", action: str = "create", **kw: object) -> OperLog:
    defaults: dict[str, object] = {
        "method": "POST",
        "path": f"/admin/{module}s",
        "status": "success",
    }
    defaults.update(kw)
    return OperLog(module=module, action=action, **defaults)


async def test_list_oper_logs_basic(admin_session: AsyncSession) -> None:
    admin_session.add(_oper(module="user"))
    admin_session.add(_oper(module="role", action="delete", method="DELETE", status="failure"))
    await admin_session.commit()

    svc = AuditQueryService(admin_session)
    result = await svc.list_oper_logs(query=ListQuery())
    assert result.total == 2
    assert len(result.items) == 2


async def test_list_oper_logs_filter_by_module(admin_session: AsyncSession) -> None:
    admin_session.add(_oper(module="user"))
    admin_session.add(_oper(module="role", action="delete", method="DELETE"))
    await admin_session.commit()

    svc = AuditQueryService(admin_session)
    result = await svc.list_oper_logs(query=ListQuery(), module="role")
    assert result.total == 1
    assert result.items[0].module == "role"


async def test_list_oper_logs_filter_by_status(admin_session: AsyncSession) -> None:
    admin_session.add(_oper(module="user"))
    admin_session.add(
        _oper(module="role", status="failure", error_code="auth.forbidden")
    )
    await admin_session.commit()

    svc = AuditQueryService(admin_session)
    result = await svc.list_oper_logs(query=ListQuery(), status="failure")
    assert result.total == 1
    assert result.items[0].error_code == "auth.forbidden"


async def test_list_oper_logs_filter_by_actor_id(admin_session: AsyncSession) -> None:
    admin_session.add(
        _oper(module="user", actor_id=100, actor_name="alice")
    )
    admin_session.add(
        _oper(
            module="user", action="update", method="PUT",
            path="/admin/users/1", actor_id=200, actor_name="bob",
        )
    )
    await admin_session.commit()

    svc = AuditQueryService(admin_session)
    result = await svc.list_oper_logs(query=ListQuery(), actor_id=100)
    assert result.total == 1
    assert result.items[0].actor_name == "alice"


async def test_list_login_logs_basic(admin_session: AsyncSession) -> None:
    admin_session.add(LoginLog(username="alice", status="success", user_id=1))
    admin_session.add(
        LoginLog(username="ghost", status="failure", failure_reason="user_not_found")
    )
    await admin_session.commit()

    svc = AuditQueryService(admin_session)
    result = await svc.list_login_logs(query=ListQuery())
    assert result.total == 2
    assert len(result.items) == 2


async def test_list_login_logs_filter_by_status(admin_session: AsyncSession) -> None:
    admin_session.add(LoginLog(username="alice", status="success", user_id=1))
    admin_session.add(
        LoginLog(username="ghost", status="failure", failure_reason="bad_credentials")
    )
    await admin_session.commit()

    svc = AuditQueryService(admin_session)
    result = await svc.list_login_logs(query=ListQuery(), status="failure")
    assert result.total == 1
    assert result.items[0].username == "ghost"


async def test_list_login_logs_filter_by_user_id(admin_session: AsyncSession) -> None:
    admin_session.add(LoginLog(username="alice", status="success", user_id=1))
    admin_session.add(LoginLog(username="bob", status="success", user_id=2))
    await admin_session.commit()

    svc = AuditQueryService(admin_session)
    result = await svc.list_login_logs(query=ListQuery(), user_id=2)
    assert result.total == 1
    assert result.items[0].username == "bob"


async def test_list_login_logs_keyword_filter(admin_session: AsyncSession) -> None:
    admin_session.add(LoginLog(username="alice_zhang", status="success", user_id=1))
    admin_session.add(LoginLog(username="bob_li", status="success", user_id=2))
    await admin_session.commit()

    svc = AuditQueryService(admin_session)
    result = await svc.list_login_logs(query=ListQuery(keyword="alice"))
    assert result.total == 1
    assert result.items[0].username == "alice_zhang"
