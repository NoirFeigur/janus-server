"""Admin menu business logic."""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy.ext.asyncio import AsyncSession
from starlette import status

from src.admin.menus.repository import MenuRepository
from src.admin.menus.schemas import MenuCreate, MenuUpdate
from src.auth.service import AuthenticatedUser
from src.db.models.identity import Menu
from src.enums import ErrorCode, MenuType
from src.exceptions import AppError


class MenuService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.repo = MenuRepository(session)

    async def list_menus(self) -> Sequence[Menu]:
        return await self.repo.list_all()

    async def list_current_user_menus(self, user: AuthenticatedUser) -> Sequence[Menu]:
        return await self.repo.list_active_visible_for_user(
            user.user_id, unrestricted=user.is_superuser
        )

    async def _require(self, menu_id: int) -> Menu:
        menu = await self.repo.get(menu_id)
        if menu is None:
            raise AppError(ErrorCode.request_invalid, status.HTTP_404_NOT_FOUND)
        return menu

    async def _validate_parent(
        self, parent_id: int | None, *, current_id: int | None = None
    ) -> None:
        if parent_id is None:
            return
        if current_id is not None and parent_id == current_id:
            raise AppError(ErrorCode.request_invalid, status.HTTP_400_BAD_REQUEST)
        parent = await self.repo.get(parent_id)
        if parent is None:
            raise AppError(ErrorCode.request_invalid, status.HTTP_400_BAD_REQUEST)
        if current_id is not None:
            all_menus = {m.id: m for m in await self.repo.list_all()}
            cursor = parent
            while cursor.parent_id is not None:
                if cursor.parent_id == current_id:
                    raise AppError(
                        ErrorCode.request_invalid, status.HTTP_400_BAD_REQUEST
                    )
                next_parent = all_menus.get(cursor.parent_id)
                if next_parent is None:
                    break
                cursor = next_parent

    def _validate_kind(self, menu_type: MenuType, perms: str | None) -> None:
        if menu_type == MenuType.button and not perms:
            raise AppError(ErrorCode.request_invalid, status.HTTP_400_BAD_REQUEST)
        if menu_type != MenuType.button and perms:
            raise AppError(ErrorCode.request_invalid, status.HTTP_400_BAD_REQUEST)

    async def create_menu(
        self, payload: MenuCreate, *, actor: AuthenticatedUser
    ) -> Menu:
        await self._validate_parent(payload.parent_id)
        self._validate_kind(payload.menu_type, payload.perms)
        menu = Menu(
            name=payload.name,
            parent_id=payload.parent_id,
            menu_type=payload.menu_type.value,
            perms=payload.perms,
            path=payload.path,
            component=payload.component,
            query_param=payload.query_param,
            is_frame=payload.is_frame,
            is_cache=payload.is_cache,
            icon=payload.icon,
            sort_order=payload.sort_order,
            visible=payload.visible,
            status=payload.status.value,
            remark=payload.remark,
            created_by=actor.user_id,
            create_dept=actor.department_id,
            updated_by=actor.user_id,
        )
        await self.repo.create(menu)
        await self.session.commit()
        return menu

    async def update_menu(
        self, menu_id: int, payload: MenuUpdate, *, actor: AuthenticatedUser
    ) -> Menu:
        menu = await self._require(menu_id)
        values = payload.model_dump(exclude_unset=True, exclude={"menu_type", "status"})
        if "parent_id" in values:
            await self._validate_parent(values["parent_id"], current_id=menu_id)

        effective_type = (
            payload.menu_type.value if payload.menu_type is not None else menu.menu_type
        )
        effective_perms = values.get("perms", menu.perms)
        self._validate_kind(MenuType(effective_type), effective_perms)

        if payload.menu_type is not None:
            values["menu_type"] = payload.menu_type.value
        if payload.status is not None:
            values["status"] = payload.status.value
        values["updated_by"] = actor.user_id
        await self.repo.update(menu, **values)
        await self.session.commit()
        return menu

    async def delete_menu(self, menu_id: int, *, actor: AuthenticatedUser) -> None:
        menu = await self._require(menu_id)
        if await self.repo.has_children(menu_id) or await self.repo.is_role_granted(
            menu_id
        ):
            raise AppError(ErrorCode.request_invalid, status.HTTP_400_BAD_REQUEST)
        menu.updated_by = actor.user_id
        await self.repo.soft_delete(menu)
        await self.session.commit()
