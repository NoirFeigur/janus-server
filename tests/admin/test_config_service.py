"""Direct unit tests for ``ConfigService`` (service layer, no HTTP).

Drives the service with a plain ``await`` against an in-memory SQLite session.
Covers the branches the route-level tests (``test_config.py``) missed due to the
ASGITransport coverage.py tracer corruption on CPython 3.11: list/get/create/
update/delete including validation, duplicate-key rejection, type-change
revalidation, builtin-undeletable guard, and sort resolution.
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from src.admin.config.schemas import ConfigCreate, ConfigUpdate
from src.admin.config.service import ConfigService
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


def _create_payload(**overrides: object) -> ConfigCreate:
    defaults: dict[str, object] = {
        "config_key": "auth.login_max_failures",
        "config_value": "5",
        "value_type": "int",
        "config_name": "登录最大失败次数",
        "is_builtin": False,
    }
    defaults.update(overrides)
    return ConfigCreate(**defaults)  # type: ignore[arg-type]


async def test_create_and_get(admin_session: AsyncSession) -> None:
    svc = ConfigService(admin_session)
    config = await svc.create_config(_create_payload(), actor=ACTOR)
    assert config.config_key == "auth.login_max_failures"
    assert config.config_value == "5"

    fetched = await svc.get_config(config.id)
    assert fetched.id == config.id


async def test_get_missing_raises_404(admin_session: AsyncSession) -> None:
    svc = ConfigService(admin_session)
    with pytest.raises(AppError) as exc:
        await svc.get_config(99999)
    assert exc.value.status_code == 404


async def test_create_duplicate_key_rejected(admin_session: AsyncSession) -> None:
    svc = ConfigService(admin_session)
    await svc.create_config(_create_payload(), actor=ACTOR)
    with pytest.raises(AppError) as exc:
        await svc.create_config(_create_payload(), actor=ACTOR)
    assert exc.value.status_code == 400


async def test_create_value_not_parsing_rejected(admin_session: AsyncSession) -> None:
    svc = ConfigService(admin_session)
    with pytest.raises(AppError) as exc:
        await svc.create_config(
            _create_payload(config_value="not-a-number", value_type="int"),
            actor=ACTOR,
        )
    assert exc.value.status_code == 400


async def test_list_configs_basic(admin_session: AsyncSession) -> None:
    svc = ConfigService(admin_session)
    await svc.create_config(_create_payload(), actor=ACTOR)
    await svc.create_config(
        _create_payload(config_key="app.name", config_value="janus", value_type="string"),
        actor=ACTOR,
    )
    result = await svc.list_configs(query=ListQuery())
    assert result.total == 2
    assert len(result.items) == 2


async def test_list_configs_keyword_filter(admin_session: AsyncSession) -> None:
    svc = ConfigService(admin_session)
    await svc.create_config(_create_payload(), actor=ACTOR)
    await svc.create_config(
        _create_payload(config_key="app.name", config_value="janus", value_type="string"),
        actor=ACTOR,
    )
    result = await svc.list_configs(query=ListQuery(keyword="app"))
    assert result.total == 1
    assert result.items[0].config_key == "app.name"


async def test_list_configs_sort_descending(admin_session: AsyncSession) -> None:
    svc = ConfigService(admin_session)
    await svc.create_config(_create_payload(config_key="a.key"), actor=ACTOR)
    await svc.create_config(
        _create_payload(config_key="z.key", config_value="10"), actor=ACTOR
    )
    result = await svc.list_configs(
        query=ListQuery(sort_by="config_key", sort_order="desc")
    )
    assert result.items[0].config_key == "z.key"


async def test_list_configs_invalid_sort_rejected(admin_session: AsyncSession) -> None:
    svc = ConfigService(admin_session)
    with pytest.raises(AppError) as exc:
        await svc.list_configs(query=ListQuery(sort_by="evil"))
    assert exc.value.status_code == 400


async def test_update_value(admin_session: AsyncSession) -> None:
    svc = ConfigService(admin_session)
    config = await svc.create_config(_create_payload(), actor=ACTOR)
    updated = await svc.update_config(
        config.id, ConfigUpdate(config_value="10"), actor=ACTOR
    )
    assert updated.config_value == "10"


async def test_update_value_not_parsing_rejected(admin_session: AsyncSession) -> None:
    svc = ConfigService(admin_session)
    config = await svc.create_config(_create_payload(), actor=ACTOR)
    with pytest.raises(AppError) as exc:
        await svc.update_config(
            config.id, ConfigUpdate(config_value="abc"), actor=ACTOR
        )
    assert exc.value.status_code == 400


async def test_update_type_change_revalidates(admin_session: AsyncSession) -> None:
    """Changing value_type alone re-checks the stored value against the new type."""
    svc = ConfigService(admin_session)
    config = await svc.create_config(
        _create_payload(config_key="s.key", config_value="hello", value_type="string"),
        actor=ACTOR,
    )
    with pytest.raises(AppError) as exc:
        await svc.update_config(
            config.id, ConfigUpdate(value_type="int"), actor=ACTOR
        )
    assert exc.value.status_code == 400


async def test_update_missing_raises_404(admin_session: AsyncSession) -> None:
    svc = ConfigService(admin_session)
    with pytest.raises(AppError) as exc:
        await svc.update_config(99999, ConfigUpdate(config_value="x"), actor=ACTOR)
    assert exc.value.status_code == 404


async def test_delete_non_builtin(admin_session: AsyncSession) -> None:
    svc = ConfigService(admin_session)
    config = await svc.create_config(_create_payload(), actor=ACTOR)
    await svc.delete_config(config.id, actor=ACTOR)
    with pytest.raises(AppError) as exc:
        await svc.get_config(config.id)
    assert exc.value.status_code == 404


async def test_delete_builtin_rejected(admin_session: AsyncSession) -> None:
    svc = ConfigService(admin_session)
    config = await svc.create_config(
        _create_payload(is_builtin=True), actor=ACTOR
    )
    with pytest.raises(AppError) as exc:
        await svc.delete_config(config.id, actor=ACTOR)
    assert exc.value.status_code == 400


async def test_delete_missing_raises_404(admin_session: AsyncSession) -> None:
    svc = ConfigService(admin_session)
    with pytest.raises(AppError) as exc:
        await svc.delete_config(99999, actor=ACTOR)
    assert exc.value.status_code == 404
