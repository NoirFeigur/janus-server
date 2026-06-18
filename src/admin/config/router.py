"""Admin platform-config endpoints (router layer).

CRUD over the ``sys_config`` key-value table, each gated by a ``system:config:*``
permission via :class:`RequiredPerms`. Responses use the management-plane
envelope; the actor id (for audit columns) comes from the authenticated principal.
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from src.admin.config.schemas import SysConfigCreate, SysConfigRead, SysConfigUpdate
from src.admin.config.service import SysConfigService
from src.auth.dependencies import RequiredPerms, TraceId
from src.auth.service import AuthenticatedUser
from src.core.pagination import Page, page
from src.core.query import ListQuery
from src.db.session import get_session
from src.responses import SuccessEnvelope, success

router = APIRouter(prefix="/config", tags=["admin:config"])


def get_config_service(
    session: Annotated[AsyncSession, Depends(get_session, scope="function")],
) -> SysConfigService:
    return SysConfigService(session)


ServiceDep = Annotated[SysConfigService, Depends(get_config_service)]


@router.get("", response_model=SuccessEnvelope[Page[SysConfigRead]])
async def list_configs(
    service: ServiceDep,
    trace_id: TraceId,
    _: Annotated[AuthenticatedUser, Depends(RequiredPerms("system:config:list"))],
    keyword: str | None = None,
    sort_by: str | None = None,
    sort_order: Literal["asc", "desc"] = "asc",
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> SuccessEnvelope[Page[SysConfigRead]]:
    query = ListQuery(
        keyword=keyword,
        sort_by=sort_by,
        sort_order=sort_order,
        limit=limit,
        offset=offset,
    )
    result = await service.list_configs(query=query)
    return success(
        page(
            [SysConfigRead.model_validate(row) for row in result.items],
            total=result.total,
            limit=result.limit,
            offset=result.offset,
        ),
        trace_id=trace_id,
    )


@router.get("/{config_id}", response_model=SuccessEnvelope[SysConfigRead])
async def get_config(
    config_id: int,
    service: ServiceDep,
    trace_id: TraceId,
    _: Annotated[AuthenticatedUser, Depends(RequiredPerms("system:config:query"))],
) -> SuccessEnvelope[SysConfigRead]:
    config = await service.get_config(config_id)
    return success(SysConfigRead.model_validate(config), trace_id=trace_id)


@router.post("", response_model=SuccessEnvelope[SysConfigRead])
async def create_config(
    payload: SysConfigCreate,
    service: ServiceDep,
    trace_id: TraceId,
    user: Annotated[AuthenticatedUser, Depends(RequiredPerms("system:config:add"))],
) -> SuccessEnvelope[SysConfigRead]:
    config = await service.create_config(payload, actor=user)
    return success(SysConfigRead.model_validate(config), trace_id=trace_id)


@router.put("/{config_id}", response_model=SuccessEnvelope[SysConfigRead])
async def update_config(
    config_id: int,
    payload: SysConfigUpdate,
    service: ServiceDep,
    trace_id: TraceId,
    user: Annotated[AuthenticatedUser, Depends(RequiredPerms("system:config:edit"))],
) -> SuccessEnvelope[SysConfigRead]:
    config = await service.update_config(config_id, payload, actor=user)
    return success(SysConfigRead.model_validate(config), trace_id=trace_id)


@router.delete("/{config_id}", response_model=SuccessEnvelope[None])
async def delete_config(
    config_id: int,
    service: ServiceDep,
    trace_id: TraceId,
    user: Annotated[AuthenticatedUser, Depends(RequiredPerms("system:config:remove"))],
) -> SuccessEnvelope[None]:
    await service.delete_config(config_id, actor=user)
    return success(None, trace_id=trace_id)
