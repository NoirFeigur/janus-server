"""Admin user business logic (service layer).

Hashes passwords (argon2), enforces FK-less integrity (unique username,
referenced dept/roles exist), and — the piece the Oracle ruling pinned to the
user surface — applies the actor's resolved **data scope** on both listing and
every single-user mutation. An actor who cannot see a target user gets the same
opaque 403 as a permission failure; there is no "exists but hidden" oracle.

The transaction is owned by the request-level Unit of Work, not here: this
layer only ``flush()``es, and the single commit happens at the request edge
(:func:`src.db.session.get_session`). Side effects that must run only after the
commit lands — revoking a target's sessions when their password changes —
register as after-commit hooks so a rolled-back request never logs anyone out.

Role assignment is a set replace via ``UserRole``. ``password`` is hashed on the
way in and never read back out (§0.8).
"""

from __future__ import annotations

from collections.abc import Sequence
from functools import partial

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette import status

from src.admin.users.repository import UserRepository
from src.admin.users.schemas import UserCreate, UserUpdate
from src.auth import perm_cache
from src.auth.constants import SUPERADMIN_ROLE_CODE
from src.auth.service import AuthenticatedUser, AuthService, DataScopeFilter
from src.config import get_settings
from src.core.pagination import PageResult, page_result
from src.core.query import BatchResult, ListQuery, resolve_sort
from src.core.security import hash_password_async, password_strength_violations
from src.db.models.identity import Department, Role, User
from src.db.session import add_after_commit_hook
from src.enums import DataScope, ErrorCode, UserStatus
from src.exceptions import AppError

UserDetail = tuple[User, list[int]]
UserPage = PageResult[UserDetail]

USER_SORT_COLUMNS = {
    "id": User.id,
    "created_at": User.created_at,
    "username": User.username,
    "employee_no": User.employee_no,
    "status": User.status,
}


class UserService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.repo = UserRepository(session)
        self.auth = AuthService(session)

    async def _scope(self, actor: AuthenticatedUser) -> DataScopeFilter:
        return await self.auth.resolve_data_scope(actor)

    async def _require_visible(
        self, user_id: int, actor: AuthenticatedUser
    ) -> User:
        """Fetch a user the actor is allowed to see, else opaque 403."""
        user = await self.repo.get(user_id)
        scope = await self._scope(actor)
        if user is None or not self.repo.is_visible(
            user, scope, actor_id=actor.user_id
        ):
            raise AppError(ErrorCode.auth_forbidden, status.HTTP_403_FORBIDDEN)
        return user

    async def _validate_department(self, department_id: int | None) -> None:
        if department_id is None:
            return
        stmt = (
            select(Department.id)
            .where(Department.id == department_id)
            .where(Department.is_deleted.is_(False))
        )
        if await self.session.scalar(stmt) is None:
            raise AppError(ErrorCode.request_invalid, status.HTTP_400_BAD_REQUEST)

    async def _require_department_in_scope(
        self, department_id: int | None, actor: AuthenticatedUser
    ) -> None:
        """A non-unrestricted actor may only place a user in a department they
        can see. ``department_id IS NULL`` (no department) is allowed only for
        unrestricted/super-admin actors — a scoped actor cannot create users they
        would then be unable to see or manage (no write-outside-scope hole)."""
        scope = await self._scope(actor)
        if scope.unrestricted:
            return
        if department_id is None or department_id not in scope.department_ids:
            raise AppError(ErrorCode.auth_forbidden, status.HTTP_403_FORBIDDEN)

    async def _validate_roles(self, role_ids: Sequence[int]) -> None:
        if not role_ids:
            return
        stmt = (
            select(Role.id)
            .where(Role.id.in_(role_ids))
            .where(Role.is_deleted.is_(False))
        )
        result = await self.session.scalars(stmt)
        if set(result.all()) != set(role_ids):
            raise AppError(ErrorCode.request_invalid, status.HTTP_400_BAD_REQUEST)

    async def _require_assignable_roles(
        self, role_ids: Sequence[int], actor: AuthenticatedUser
    ) -> None:
        """Privilege-escalation guard for role assignment (three layers).

        Super-admin may assign anything; for everyone else a role is assignable
        only if it grants the actor no power the actor lacks along EVERY axis:

        1. **Permission codes** — the role's conferred perms must be a subset of
           the actor's own (no minting a ``*:*:*`` role onto anyone).
        2. **Super-admin marker** — a role carrying the ``superadmin`` code is
           never assignable by a non-super-admin. This is the axis the perms-only
           check is blind to: ``is_superuser`` is *code*-based, so a ``superadmin``
           role with no menus confers an EMPTY perm set that trivially passes the
           subset test — yet assigning it would hand out full super-admin.
        3. **Data-scope breadth** — the role's visibility may not exceed the
           actor's own (``all`` is refused outright; ``custom`` depts must fall
           within the actor's visible department set). A perms-light, scope-heavy
           role (e.g. ``all``-scope with zero menus) otherwise slips through.
        """
        if actor.is_superuser or not role_ids:
            return
        conferred = await self.auth.permissions_for_roles(role_ids)
        if not conferred.issubset(actor.permissions):
            raise AppError(ErrorCode.auth_forbidden, status.HTTP_403_FORBIDDEN)

        roles = await self.auth.roles_for_assignment(role_ids)
        if any(role.code == SUPERADMIN_ROLE_CODE for role in roles):
            raise AppError(ErrorCode.auth_forbidden, status.HTTP_403_FORBIDDEN)
        await self._require_roles_within_scope(roles, actor)

    async def _require_roles_within_scope(
        self, roles: Sequence[Role], actor: AuthenticatedUser
    ) -> None:
        """Reject assigning any role whose data scope exceeds the actor's own.

        An unrestricted actor (super-admin or an ``all``-scope role) may assign
        any scope. A scoped actor may not assign a role that is ``all`` (strictly
        broader than the actor) nor a ``custom`` role whose granted departments
        leak outside the actor's visible set. Relative scopes (``dept`` /
        ``dept_and_child`` / ``self`` / ``dept_and_child_or_self``) are bounded by
        the eventual holder's own position, so they confer no breadth the actor
        lacks and are allowed.
        """
        actor_scope = await self._scope(actor)
        if actor_scope.unrestricted:
            return
        if any(role.data_scope == DataScope.all_data.value for role in roles):
            raise AppError(ErrorCode.auth_forbidden, status.HTTP_403_FORBIDDEN)
        custom_role_ids = [
            role.id for role in roles if role.data_scope == DataScope.custom.value
        ]
        if custom_role_ids:
            granted = await self.auth.role_department_ids(custom_role_ids)
            if not granted.issubset(actor_scope.department_ids):
                raise AppError(ErrorCode.auth_forbidden, status.HTTP_403_FORBIDDEN)

    async def _require_unique_employee_no(self, employee_no: str) -> None:
        stmt = (
            select(User.id)
            .where(User.employee_no == employee_no)
            .where(User.is_deleted.is_(False))
        )
        if await self.session.scalar(stmt) is not None:
            raise AppError(ErrorCode.request_invalid, status.HTTP_400_BAD_REQUEST)

    async def _detail(self, user: User) -> UserDetail:
        return user, await self.repo.list_role_ids(user.id)

    async def list_users(
        self,
        actor: AuthenticatedUser,
        *,
        query: ListQuery | None = None,
    ) -> UserPage:
        query = query or ListQuery()
        scope = await self._scope(actor)
        sort = resolve_sort(query, allowed=USER_SORT_COLUMNS, default="id")
        total = await self.repo.count_in_scope(
            scope, actor_id=actor.user_id, keyword=query.keyword
        )
        users = await self.repo.list_in_scope(
            scope,
            actor_id=actor.user_id,
            keyword=query.keyword,
            sort=sort,
            limit=query.limit,
            offset=query.offset,
        )
        # One bulk role lookup for the whole page (was 1+N: one query per user).
        role_map = await self.repo.list_role_ids_for_users([u.id for u in users])
        items = [(u, role_map.get(u.id, [])) for u in users]
        return page_result(items, total=total, limit=query.limit, offset=query.offset)

    async def get_user(self, user_id: int, actor: AuthenticatedUser) -> UserDetail:
        return await self._detail(await self._require_visible(user_id, actor))

    async def create_user(
        self, payload: UserCreate, actor: AuthenticatedUser
    ) -> UserDetail:
        if await self.repo.get_by_username(payload.username) is not None:
            raise AppError(ErrorCode.request_invalid, status.HTTP_400_BAD_REQUEST)
        await self._require_unique_employee_no(payload.employee_no)
        await self._validate_department(payload.department_id)
        await self._require_department_in_scope(payload.department_id, actor)
        await self._validate_roles(payload.role_ids)
        await self._require_assignable_roles(payload.role_ids, actor)
        password_hash = (
            await hash_password_async(payload.password) if payload.password else None
        )
        user = User(
            username=payload.username,
            employee_no=payload.employee_no,
            password=password_hash,
            real_name=payload.real_name,
            email=payload.email,
            mobile=payload.mobile,
            department_id=payload.department_id,
            status=payload.status.value,
            preferred_locale=payload.preferred_locale,
            remark=payload.remark,
            created_by=actor.user_id,
            create_dept=actor.department_id,
            updated_by=actor.user_id,
        )
        await self.repo.create(user)
        await self.repo.replace_roles(user.id, payload.role_ids)
        return await self._detail(user)

    async def update_user(
        self, user_id: int, payload: UserUpdate, actor: AuthenticatedUser
    ) -> UserDetail:
        user = await self._require_visible(user_id, actor)
        values = payload.model_dump(
            exclude_unset=True, exclude={"role_ids", "password", "status"}
        )
        password_changed = payload.password is not None
        if payload.password is not None:
            values["password"] = await hash_password_async(payload.password)
        if payload.status is not None:
            values["status"] = payload.status.value
        if "department_id" in values:
            await self._validate_department(values["department_id"])
            await self._require_department_in_scope(values["department_id"], actor)
        values["updated_by"] = actor.user_id
        await self.repo.update(user, **values)

        if payload.role_ids is not None:
            await self._validate_roles(payload.role_ids)
            await self._require_assignable_roles(payload.role_ids, actor)
            await self.repo.replace_roles(user_id, payload.role_ids)

        # A role-binding or status change alters this user's cached permission
        # snapshot, so drop it after the write commits (a rolled-back update must
        # not invalidate). Status matters because a re-enabled user could
        # otherwise be served a snapshot cached before a concurrent role change;
        # invalidating keeps the cache strictly consistent with the DB.
        if payload.role_ids is not None or payload.status is not None:
            add_after_commit_hook(
                self.session, lambda: perm_cache.invalidate_user(user_id)
            )

        # A password change here must force re-login everywhere, exactly like
        # reset_password — otherwise an admin-set password through this endpoint
        # would silently leave the target's old sessions alive (B7). Fires only
        # after the write commits (after-commit hook), so a rolled-back update
        # never logs the target out.
        #
        # Disabling a user must ALSO kill every live session: the access-allowlist
        # entry and rotatable refresh both outlive the status flip on their own,
        # so a disabled user keeps a usable refresh token until it expires, and a
        # later re-enable would silently resurrect those stale sessions. Revoking
        # on disable forces a fresh login and severs the resurrection path. (A
        # single ``revoke_all_sessions`` is idempotent, so overlapping with the
        # password-change hook is harmless.)
        disabled_now = payload.status is not None and (
            payload.status == UserStatus.disabled
        )
        if password_changed or disabled_now:
            add_after_commit_hook(
                self.session, lambda: self.auth.sessions.revoke_all_sessions(user_id)
            )
        return await self._detail(user)

    async def reset_password(
        self, user_id: int, new_password: str, actor: AuthenticatedUser
    ) -> None:
        """Admin-set a target user's password and force re-login everywhere.

        Visibility-gated like every single-user mutation (an actor who cannot see
        the target gets the same opaque 403). No old-password check — this is an
        admin acting on the target's behalf. Strength is enforced server-side
        (``auth_password_too_weak`` / 400 with machine-readable violation labels).
        On success **all** of the target's sessions are revoked (B7), so any
        session standing on the old credential dies immediately. The revoke
        fires as an after-commit hook so it only runs once the password write
        has landed (a rolled-back request never logs the target out).
        """
        user = await self._require_visible(user_id, actor)
        violations = password_strength_violations(
            new_password, min_length=get_settings().password_min_length
        )
        if violations:
            raise AppError(
                ErrorCode.auth_password_too_weak,
                status.HTTP_400_BAD_REQUEST,
                params={"violations": violations},
            )
        user.password = await hash_password_async(new_password)
        user.updated_by = actor.user_id
        await self.repo.session.flush()
        add_after_commit_hook(
            self.session, lambda: self.auth.sessions.revoke_all_sessions(user_id)
        )

    async def delete_user(self, user_id: int, actor: AuthenticatedUser) -> None:
        user = await self._require_visible(user_id, actor)
        await self.repo.replace_roles(user_id, [])
        user.updated_by = actor.user_id
        await self.repo.soft_delete(user)
        # Soft-deleting drops the user's perm snapshot AND must kill every live
        # session: like disable, the access-allowlist entry and refresh token
        # outlive the row flip on their own, so a deleted user keeps usable
        # credentials until TTL. Both hooks fire only after the write commits.
        add_after_commit_hook(
            self.session, lambda: perm_cache.invalidate_user(user_id)
        )
        add_after_commit_hook(
            self.session, lambda: self.auth.sessions.revoke_all_sessions(user_id)
        )

    async def batch_delete_users(
        self, ids: Sequence[int], actor: AuthenticatedUser
    ) -> BatchResult:
        requested_ids = list(dict.fromkeys(ids))
        scope = await self._scope(actor)
        affected, skipped_ids = await self.repo.soft_delete_many(
            requested_ids,
            scope_predicate=self.repo._scope_predicate(scope, actor_id=actor.user_id),
        )
        skipped = set(skipped_ids)
        for user_id in requested_ids:
            if user_id not in skipped:
                await self.repo.replace_roles(user_id, [])
                # partial binds each id by value (a bare lambda would late-bind
                # the loop variable to its final value in every hook).
                add_after_commit_hook(
                    self.session, partial(perm_cache.invalidate_user, user_id)
                )
        return BatchResult.of(
            requested=len(requested_ids), affected=affected, skipped=skipped_ids
        )
