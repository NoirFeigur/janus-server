"""Admin menu and dynamic-menu endpoints."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from src.admin.menus.schemas import MenuCreate, MenuRead, MenuUpdate
from src.admin.menus.service import MenuService
from src.auth.dependencies import CurrentJwtUser, RequiredPerms, TraceId
from src.auth.service import AuthenticatedUser
from src.core.query import BatchIdsRequest, BatchResult
from src.db.models.identity import Menu
from src.db.session import get_session
from src.responses import SuccessEnvelope, success

router = APIRouter(prefix="/menus", tags=["admin:menus"])


def get_menu_service(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> MenuService:
    return MenuService(session)


ServiceDep = Annotated[MenuService, Depends(get_menu_service)]


def _to_read(menu: Menu) -> MenuRead:
    return MenuRead.model_validate(menu)


@router.get("", response_model=SuccessEnvelope[list[MenuRead]])
async def list_menus(
    service: ServiceDep,
    trace_id: TraceId,
    _: Annotated[AuthenticatedUser, Depends(RequiredPerms("system:menu:list"))],
    keyword: str | None = None,
) -> SuccessEnvelope[list[MenuRead]]:
    menus = await service.list_menus(keyword=keyword)
    return success([_to_read(m) for m in menus], trace_id=trace_id)


@router.get("/current", response_model=SuccessEnvelope[list[MenuRead]])
async def current_menus(
    service: ServiceDep,
    trace_id: TraceId,
    user: CurrentJwtUser,
) -> SuccessEnvelope[list[MenuRead]]:
    menus = await service.list_current_user_menus(user)
    return success([_to_read(m) for m in menus], trace_id=trace_id)


@router.post("", response_model=SuccessEnvelope[MenuRead])
async def create_menu(
    payload: MenuCreate,
    service: ServiceDep,
    trace_id: TraceId,
    user: Annotated[AuthenticatedUser, Depends(RequiredPerms("system:menu:add"))],
) -> SuccessEnvelope[MenuRead]:
    menu = await service.create_menu(payload, actor=user)
    return success(_to_read(menu), trace_id=trace_id)


@router.post("/batch-delete", response_model=SuccessEnvelope[BatchResult])
async def batch_delete_menus(
    payload: BatchIdsRequest,
    service: ServiceDep,
    trace_id: TraceId,
    user: Annotated[
        AuthenticatedUser, Depends(RequiredPerms("system:menu:remove"))
    ],
) -> SuccessEnvelope[BatchResult]:
    result = await service.batch_delete_menus(payload.ids, actor=user)
    return success(result, trace_id=trace_id)


@router.put("/{menu_id}", response_model=SuccessEnvelope[MenuRead])
async def update_menu(
    menu_id: int,
    payload: MenuUpdate,
    service: ServiceDep,
    trace_id: TraceId,
    user: Annotated[AuthenticatedUser, Depends(RequiredPerms("system:menu:edit"))],
) -> SuccessEnvelope[MenuRead]:
    menu = await service.update_menu(menu_id, payload, actor=user)
    return success(_to_read(menu), trace_id=trace_id)


@router.delete("/{menu_id}", response_model=SuccessEnvelope[None])
async def delete_menu(
    menu_id: int,
    service: ServiceDep,
    trace_id: TraceId,
    user: Annotated[
        AuthenticatedUser, Depends(RequiredPerms("system:menu:remove"))
    ],
) -> SuccessEnvelope[None]:
    await service.delete_menu(menu_id, actor=user)
    return success(None, trace_id=trace_id)
