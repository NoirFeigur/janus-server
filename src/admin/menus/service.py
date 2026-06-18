"""Admin menu business logic."""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy.ext.asyncio import AsyncSession
from starlette import status

from src.admin.menus.repository import MenuRepository
from src.admin.menus.schemas import MenuCreate, MenuUpdate
from src.auth import perm_cache
from src.auth.service import AuthenticatedUser
from src.core.query import BatchResult
from src.db.models.identity import Menu
from src.db.session import add_after_commit_hook
from src.enums import ErrorCode, MenuType
from src.exceptions import AppError


class MenuService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.repo = MenuRepository(session)

    async def list_menus(self, *, keyword: str | None = None) -> Sequence[Menu]:
        all_menus = await self.repo.list_all()
        normalized_keyword = keyword.strip().lower() if keyword is not None else ""
        if not normalized_keyword:
            return all_menus
        return self._filter_with_ancestors(all_menus, normalized_keyword)

    def _filter_with_ancestors(
        self, all_menus: Sequence[Menu], keyword: str
    ) -> list[Menu]:
        menus_by_id = {menu.id: menu for menu in all_menus}
        included_ids: set[int] = set()

        for menu in all_menus:
            if not self._matches_keyword(menu, keyword):
                continue
            included_ids.add(menu.id)
            cursor = menu
            seen_ids = {menu.id}
            while cursor.parent_id is not None:
                if cursor.parent_id in seen_ids:
                    break
                seen_ids.add(cursor.parent_id)
                parent = menus_by_id.get(cursor.parent_id)
                if parent is None:
                    break
                included_ids.add(parent.id)
                cursor = parent

        return [menu for menu in all_menus if menu.id in included_ids]

    def _matches_keyword(self, menu: Menu, keyword: str) -> bool:
        return any(
            keyword in value.lower()
            for value in (menu.name, menu.perms, menu.path)
            if value is not None
        )

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

    def _require_assignable_perms(
        self, perms: str | None, actor: AuthenticatedUser
    ) -> None:
        """Privilege-escalation guard for a menu's permission code.

        A menu's ``perms`` string IS a permission grant: every role linking the
        menu confers that code to its holders. Without this guard an actor holding
        ``system:menu:edit`` could set a menu's ``perms`` to any code — e.g. point
        a menu their own role already links at ``*:*:*`` — and instantly mint
        themselves a permission they never held (the role→menu link is unchanged,
        so the role-edit guard never re-runs). Mirror the role-assignment rule: a
        non-super-admin may only set a perms code that is already within their own
        permission set. Super-admin may set anything; a null/empty perms (catalog
        or menu node, no operation code) is trivially allowed.
        """
        if actor.is_superuser or not perms:
            return
        if perms not in actor.permissions:
            raise AppError(ErrorCode.auth_forbidden, status.HTTP_403_FORBIDDEN)

    async def create_menu(
        self, payload: MenuCreate, *, actor: AuthenticatedUser
    ) -> Menu:
        await self._validate_parent(payload.parent_id)
        self._validate_kind(payload.menu_type, payload.perms)
        self._require_assignable_perms(payload.perms, actor)
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
        await self.session.flush()
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
        # Only re-gate when this edit actually changes ``perms`` — an unrelated
        # field edit (path/icon/visible) must not require the actor to already
        # hold the menu's pre-existing code (that was vetted when it was set).
        if "perms" in values:
            self._require_assignable_perms(effective_perms, actor)

        if payload.menu_type is not None:
            values["menu_type"] = payload.menu_type.value
        if payload.status is not None:
            values["status"] = payload.status.value
        values["updated_by"] = actor.user_id
        await self.repo.update(menu, **values)
        await self.session.flush()
        # A menu's status or perms string changing alters the permission codes
        # conferred to EVERY user whose roles link this menu. Rather than walk
        # RoleMenu⋈UserRole to enumerate them, bump the global generation once —
        # one INCR invalidates all perm snapshots after commit. Other field edits
        # (path/visible/icon/...) do not affect list_permission_codes, so skip.
        if payload.status is not None or "perms" in values:
            add_after_commit_hook(self.session, perm_cache.invalidate_all)
        return menu

    async def delete_menu(self, menu_id: int, *, actor: AuthenticatedUser) -> None:
        menu = await self._require(menu_id)
        if await self.repo.has_children(menu_id) or await self.repo.is_role_granted(
            menu_id
        ):
            raise AppError(ErrorCode.request_invalid, status.HTTP_400_BAD_REQUEST)
        menu.updated_by = actor.user_id
        await self.repo.soft_delete(menu)
        await self.session.flush()

    async def batch_delete_menus(
        self, ids: Sequence[int], *, actor: AuthenticatedUser
    ) -> BatchResult:
        requested_ids = list(dict.fromkeys(ids))
        affected = 0
        skipped_ids: list[int] = []
        for menu_id in requested_ids:
            menu = await self.repo.get(menu_id)
            if menu is None:
                skipped_ids.append(menu_id)
                continue
            if await self.repo.has_children(menu_id) or await self.repo.is_role_granted(
                menu_id
            ):
                skipped_ids.append(menu_id)
                continue
            menu.updated_by = actor.user_id
            await self.repo.soft_delete(menu)
            affected += 1
        await self.session.flush()
        return BatchResult.of(
            requested=len(requested_ids), affected=affected, skipped=skipped_ids
        )
