"""Admin role endpoints (router layer).

CRUD over roles + their menu/dept grants, each gated by a ``system:role:*``
permission. The service returns a ``(role, menu_ids, dept_ids)`` tuple which
``_to_read`` assembles into the wire model (ids stringified).
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from src.admin.roles.schemas import RoleCreate, RoleRead, RoleUpdate
from src.admin.roles.service import RoleDetail, RoleService
from src.auth.dependencies import RequiredPerms, TraceId
from src.auth.service import AuthenticatedUser
from src.core.pagination import Page, page
from src.core.query import BatchIdsRequest, BatchResult, ListQuery
from src.db.session import get_session
from src.responses import SuccessEnvelope, success

router = APIRouter(prefix="/roles", tags=["admin:roles"])


def get_role_service(
    session: Annotated[AsyncSession, Depends(get_session, scope="function")],
) -> RoleService:
    return RoleService(session)


ServiceDep = Annotated[RoleService, Depends(get_role_service)]


def _to_read(detail: RoleDetail) -> RoleRead:
    role, menu_ids, dept_ids = detail
    read = RoleRead.model_validate(role)
    read.menu_ids = [str(mid) for mid in menu_ids]
    read.dept_ids = [str(did) for did in dept_ids]
    return read


@router.get("", response_model=SuccessEnvelope[Page[RoleRead]])
async def list_roles(
    service: ServiceDep,
    trace_id: TraceId,
    user: Annotated[AuthenticatedUser, Depends(RequiredPerms("system:role:list"))],
    keyword: str | None = None,
    sort_by: str | None = None,
    sort_order: Literal["asc", "desc"] = "asc",
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> SuccessEnvelope[Page[RoleRead]]:
    query = ListQuery(
        keyword=keyword,
        sort_by=sort_by,
        sort_order=sort_order,
        limit=limit,
        offset=offset,
    )
    result = await service.list_roles(user, query=query)
    return success(
        page(
            [_to_read(d) for d in result.items],
            total=result.total,
            limit=result.limit,
            offset=result.offset,
        ),
        trace_id=trace_id,
    )


@router.post("", response_model=SuccessEnvelope[RoleRead])
async def create_role(
    payload: RoleCreate,
    service: ServiceDep,
    trace_id: TraceId,
    user: Annotated[AuthenticatedUser, Depends(RequiredPerms("system:role:add"))],
) -> SuccessEnvelope[RoleRead]:
    detail = await service.create_role(payload, actor=user)
    return success(_to_read(detail), trace_id=trace_id)


@router.post("/batch-delete", response_model=SuccessEnvelope[BatchResult])
async def batch_delete_roles(
    payload: BatchIdsRequest,
    service: ServiceDep,
    trace_id: TraceId,
    user: Annotated[
        AuthenticatedUser, Depends(RequiredPerms("system:role:remove"))
    ],
) -> SuccessEnvelope[BatchResult]:
    result = await service.batch_delete_roles(payload.ids, actor=user)
    return success(result, trace_id=trace_id)


@router.put("/{role_id}", response_model=SuccessEnvelope[RoleRead])
async def update_role(
    role_id: int,
    payload: RoleUpdate,
    service: ServiceDep,
    trace_id: TraceId,
    user: Annotated[AuthenticatedUser, Depends(RequiredPerms("system:role:edit"))],
) -> SuccessEnvelope[RoleRead]:
    detail = await service.update_role(role_id, payload, actor=user)
    return success(_to_read(detail), trace_id=trace_id)


@router.delete("/{role_id}", response_model=SuccessEnvelope[None])
async def delete_role(
    role_id: int,
    service: ServiceDep,
    trace_id: TraceId,
    user: Annotated[
        AuthenticatedUser, Depends(RequiredPerms("system:role:remove"))
    ],
) -> SuccessEnvelope[None]:
    await service.delete_role(role_id, actor=user)
    return success(None, trace_id=trace_id)
