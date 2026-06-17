"""Admin department endpoints (router layer).

CRUD over the org department tree, each gated by a ``system:dept:*`` permission
via :class:`RequiredPerms`. Responses use the management-plane envelope; the
actor id (for audit columns) comes from the authenticated principal.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from src.admin.departments.schemas import (
    DepartmentCreate,
    DepartmentRead,
    DepartmentUpdate,
)
from src.admin.departments.service import DepartmentService
from src.auth.dependencies import RequiredPerms, TraceId
from src.auth.service import AuthenticatedUser
from src.db.session import get_session
from src.responses import SuccessEnvelope, success

router = APIRouter(prefix="/departments", tags=["admin:departments"])


def get_department_service(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> DepartmentService:
    return DepartmentService(session)


ServiceDep = Annotated[DepartmentService, Depends(get_department_service)]


@router.get("", response_model=SuccessEnvelope[list[DepartmentRead]])
async def list_departments(
    service: ServiceDep,
    trace_id: TraceId,
    _: Annotated[AuthenticatedUser, Depends(RequiredPerms("system:dept:list"))],
) -> SuccessEnvelope[list[DepartmentRead]]:
    departments = await service.list_departments()
    return success(
        [DepartmentRead.model_validate(d) for d in departments], trace_id=trace_id
    )


@router.post("", response_model=SuccessEnvelope[DepartmentRead])
async def create_department(
    payload: DepartmentCreate,
    service: ServiceDep,
    trace_id: TraceId,
    user: Annotated[AuthenticatedUser, Depends(RequiredPerms("system:dept:add"))],
) -> SuccessEnvelope[DepartmentRead]:
    dept = await service.create_department(payload, actor=user)
    return success(DepartmentRead.model_validate(dept), trace_id=trace_id)


@router.put("/{dept_id}", response_model=SuccessEnvelope[DepartmentRead])
async def update_department(
    dept_id: int,
    payload: DepartmentUpdate,
    service: ServiceDep,
    trace_id: TraceId,
    user: Annotated[AuthenticatedUser, Depends(RequiredPerms("system:dept:edit"))],
) -> SuccessEnvelope[DepartmentRead]:
    dept = await service.update_department(dept_id, payload, actor=user)
    return success(DepartmentRead.model_validate(dept), trace_id=trace_id)


@router.delete("/{dept_id}", response_model=SuccessEnvelope[None])
async def delete_department(
    dept_id: int,
    service: ServiceDep,
    trace_id: TraceId,
    user: Annotated[
        AuthenticatedUser, Depends(RequiredPerms("system:dept:remove"))
    ],
) -> SuccessEnvelope[None]:
    await service.delete_department(dept_id, actor=user)
    return success(None, trace_id=trace_id)
