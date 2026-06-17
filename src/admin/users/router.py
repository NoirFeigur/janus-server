"""Admin user endpoints (router layer).

CRUD over users + role assignment, each gated by a ``system:user:*`` permission.
The data-scope filter is applied inside the service using the authenticated
actor (injected by :class:`RequiredPerms`, which returns the account). The
service returns ``(user, role_ids)``; ``_to_read`` assembles the wire model.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from src.admin.users.schemas import UserCreate, UserRead, UserUpdate
from src.admin.users.service import UserDetail, UserService
from src.auth.dependencies import RequiredPerms, TraceId
from src.auth.service import AuthenticatedAccount
from src.db.session import get_session
from src.responses import SuccessEnvelope, success

router = APIRouter(prefix="/users", tags=["admin:users"])


def get_user_service(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> UserService:
    return UserService(session)


ServiceDep = Annotated[UserService, Depends(get_user_service)]


def _to_read(detail: UserDetail) -> UserRead:
    user, role_ids = detail
    read = UserRead.model_validate(user)
    read.role_ids = [str(rid) for rid in role_ids]
    return read


@router.get("", response_model=SuccessEnvelope[list[UserRead]])
async def list_users(
    service: ServiceDep,
    trace_id: TraceId,
    account: Annotated[AuthenticatedAccount, Depends(RequiredPerms("system:user:list"))],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> SuccessEnvelope[list[UserRead]]:
    details = await service.list_users(account, limit=limit, offset=offset)
    return success([_to_read(d) for d in details], trace_id=trace_id)


@router.post("", response_model=SuccessEnvelope[UserRead])
async def create_user(
    payload: UserCreate,
    service: ServiceDep,
    trace_id: TraceId,
    account: Annotated[AuthenticatedAccount, Depends(RequiredPerms("system:user:add"))],
) -> SuccessEnvelope[UserRead]:
    detail = await service.create_user(payload, account)
    return success(_to_read(detail), trace_id=trace_id)


@router.put("/{user_id}", response_model=SuccessEnvelope[UserRead])
async def update_user(
    user_id: int,
    payload: UserUpdate,
    service: ServiceDep,
    trace_id: TraceId,
    account: Annotated[AuthenticatedAccount, Depends(RequiredPerms("system:user:edit"))],
) -> SuccessEnvelope[UserRead]:
    detail = await service.update_user(user_id, payload, account)
    return success(_to_read(detail), trace_id=trace_id)


@router.delete("/{user_id}", response_model=SuccessEnvelope[None])
async def delete_user(
    user_id: int,
    service: ServiceDep,
    trace_id: TraceId,
    account: Annotated[
        AuthenticatedAccount, Depends(RequiredPerms("system:user:remove"))
    ],
) -> SuccessEnvelope[None]:
    await service.delete_user(user_id, account)
    return success(None, trace_id=trace_id)
