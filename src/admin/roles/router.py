"""Admin role endpoints (router layer).

CRUD over roles + their menu/dept grants, each gated by a ``system:role:*``
permission. The service returns a ``(role, menu_ids, dept_ids)`` tuple which
``_to_read`` assembles into the wire model (ids stringified).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from src.admin.roles.schemas import RoleCreate, RoleRead, RoleUpdate
from src.admin.roles.service import RoleDetail, RoleService
from src.auth.dependencies import RequiredPerms, TraceId
from src.auth.service import AuthenticatedUser
from src.db.session import get_session
from src.responses import SuccessEnvelope, success

router = APIRouter(prefix="/roles", tags=["admin:roles"])


def get_role_service(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> RoleService:
    return RoleService(session)


ServiceDep = Annotated[RoleService, Depends(get_role_service)]


def _to_read(detail: RoleDetail) -> RoleRead:
    role, menu_ids, dept_ids = detail
    read = RoleRead.model_validate(role)
    read.menu_ids = [str(mid) for mid in menu_ids]
    read.dept_ids = [str(did) for did in dept_ids]
    return read


@router.get("", response_model=SuccessEnvelope[list[RoleRead]])
async def list_roles(
    service: ServiceDep,
    trace_id: TraceId,
    user: Annotated[AuthenticatedUser, Depends(RequiredPerms("system:role:list"))],
) -> SuccessEnvelope[list[RoleRead]]:
    details = await service.list_roles(user)
    return success([_to_read(d) for d in details], trace_id=trace_id)


@router.post("", response_model=SuccessEnvelope[RoleRead])
async def create_role(
    payload: RoleCreate,
    service: ServiceDep,
    trace_id: TraceId,
    user: Annotated[AuthenticatedUser, Depends(RequiredPerms("system:role:add"))],
) -> SuccessEnvelope[RoleRead]:
    detail = await service.create_role(payload, actor=user)
    return success(_to_read(detail), trace_id=trace_id)


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
