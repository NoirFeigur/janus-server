"""Admin department endpoints (router layer).

CRUD over the org department tree, each gated by a ``system:dept:*`` permission
via :class:`RequiredPerms`. Responses use the management-plane envelope; the
actor id (for audit columns) comes from the authenticated principal.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from src.admin.departments.schemas import (
    DepartmentCreate,
    DepartmentRead,
    DepartmentUpdate,
)
from src.admin.departments.service import DepartmentService
from src.auth.dependencies import RequiredPerms, TraceId
from src.auth.service import AuthenticatedUser
from src.core.query import BatchIdsRequest, BatchResult
from src.db.session import get_session
from src.responses import SuccessEnvelope, success

router = APIRouter(prefix="/departments", tags=["admin:departments"])


def get_department_service(
    session: Annotated[AsyncSession, Depends(get_session, scope="function")],
) -> DepartmentService:
    return DepartmentService(session)


ServiceDep = Annotated[DepartmentService, Depends(get_department_service)]


@router.get("", response_model=SuccessEnvelope[list[DepartmentRead]])
async def list_departments(
    service: ServiceDep,
    trace_id: TraceId,
    user: Annotated[AuthenticatedUser, Depends(RequiredPerms("system:dept:list"))],
    keyword: Annotated[str | None, Query()] = None,
) -> SuccessEnvelope[list[DepartmentRead]]:
    departments = await service.list_departments(user, keyword=keyword)
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


@router.post("/batch-delete", response_model=SuccessEnvelope[BatchResult])
async def batch_delete_departments(
    payload: BatchIdsRequest,
    service: ServiceDep,
    trace_id: TraceId,
    user: Annotated[
        AuthenticatedUser, Depends(RequiredPerms("system:dept:remove"))
    ],
) -> SuccessEnvelope[BatchResult]:
    result = await service.batch_delete_departments(payload.ids, actor=user)
    return success(result, trace_id=trace_id)


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
