"""Admin user business logic (service layer).

Hashes passwords (argon2), enforces FK-less integrity (unique username,
referenced dept/roles exist), and — the piece the Oracle ruling pinned to the
user surface — applies the actor's resolved **data scope** on both listing and
every single-user mutation. An actor who cannot see a target user gets the same
opaque 403 as a permission failure; there is no "exists but hidden" oracle.

The transaction is owned by the request-level Unit of Work, not here: this
layer only ``flush()``es, and the single commit happens at the request edge
(:func:`src.db.session.get_session`). Two kinds of post-write side effect:

- **Security-critical session revocation** (password change / disable / delete)
  runs SYNCHRONOUSLY before commit — the write is flushed, then the Redis revoke
  runs inside the request's unit of work. A Redis failure raises and the request
  edge rolls the DB write back, so a Redis blip can never leave "credential
  changed in the DB but old sessions still alive". The inverse (sessions killed
  but the write rolled back on a later commit failure) is safe.
- **Permission-cache invalidation** stays a fail-open after-commit hook: it has
  a TTL backstop bounding staleness, so a Redis blip degrades gracefully rather
  than failing the request.

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
from src.enums import ActiveStatus, DataScope, ErrorCode, UserStatus
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
        # Roles must exist, be non-deleted, AND be active. Assigning a disabled
        # role would silently confer no effect now yet re-activate its grants the
        # moment an admin re-enables it — a non-disabled role is the only valid
        # assignment target. A disabled role drops out of the result set and trips
        # the set-equality check below → 400 request_invalid.
        stmt = (
            select(Role.id)
            .where(Role.id.in_(role_ids))
            .where(Role.is_deleted.is_(False))
            .where(Role.status == ActiveStatus.active.value)
        )
        result = await self.session.scalars(stmt)
        if set(result.all()) != set(role_ids):
            raise AppError(ErrorCode.request_invalid, status.HTTP_400_BAD_REQUEST)

    async def _require_assignable_roles(
        self,
        role_ids: Sequence[int],
        actor: AuthenticatedUser,
        *,
        holder_department_id: int | None,
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
        3. **Data-scope breadth** — the visibility the role would confer on the
           eventual holder (resolved against ``holder_department_id``) must not
           exceed the actor's own. ``all`` is refused outright; every other scope
           is resolved to a concrete department set and required to fall within
           the actor's visible set. A relative scope like ``dept_and_child`` is
           NOT waved through: for the holder's department it can resolve to a
           strictly broader subtree than the actor can see, which is the
           escalation this guard closes.
        """
        if actor.is_superuser or not role_ids:
            return
        conferred = await self.auth.permissions_for_roles(role_ids)
        if not conferred.issubset(actor.permissions):
            raise AppError(ErrorCode.auth_forbidden, status.HTTP_403_FORBIDDEN)

        roles = await self.auth.roles_for_assignment(role_ids)
        if any(role.code == SUPERADMIN_ROLE_CODE for role in roles):
            raise AppError(ErrorCode.auth_forbidden, status.HTTP_403_FORBIDDEN)
        await self._require_roles_within_scope(
            roles, actor, holder_department_id=holder_department_id
        )

    async def _require_roles_within_scope(
        self,
        roles: Sequence[Role],
        actor: AuthenticatedUser,
        *,
        holder_department_id: int | None,
    ) -> None:
        """Reject assigning any role whose resolved scope exceeds the actor's own.

        An unrestricted actor (super-admin or an ``all``-scope role) may assign
        any scope. For a scoped actor, each role's effective visibility is
        resolved against the eventual holder's department: ``all`` is broader than
        the actor and refused; every other scope resolves to a concrete department
        set (``self_only`` resolves to none) that must be a subset of the actor's
        own. This is what makes ``dept_and_child`` safe ONLY when the holder's
        subtree already lies inside the actor's scope — a ``dept_only`` actor
        granting ``dept_and_child`` to a holder in its department is rejected
        because the holder's subtree reaches child departments the actor cannot
        see.
        """
        actor_scope = await self._scope(actor)
        if actor_scope.unrestricted:
            return
        custom_dept_ids = await self.auth.role_department_ids(
            [role.id for role in roles if role.data_scope == DataScope.custom.value]
        )
        for role in roles:
            granted = await self.auth.resolve_role_scope(
                DataScope(role.data_scope),
                list(custom_dept_ids)
                if role.data_scope == DataScope.custom.value
                else [],
                holder_department_id=holder_department_id,
            )
            if granted.unrestricted or not granted.department_ids.issubset(
                actor_scope.department_ids
            ):
                raise AppError(ErrorCode.auth_forbidden, status.HTTP_403_FORBIDDEN)

    async def _require_dominance(
        self, target: User, actor: AuthenticatedUser
    ) -> None:
        """Reject acting on a target who outranks the actor (dominance guard).

        Visibility (``_require_visible``) answers "may the actor SEE this user";
        it does NOT answer "may the actor MANAGE this user". Without this guard a
        scoped admin holding ``system:user:resetPwd/edit/remove`` could reset the
        password of / disable / delete any *higher-privileged* user who merely
        falls inside their data scope — e.g. a department admin resetting a
        super-admin's password and taking the account over.

        The dominance test is exactly the assignment escalation test applied to
        the target's CURRENT roles: an actor may manage a target only if it could
        (re)assign that target's role set itself. This reuses the same three-axis
        guard (perm subset + ``superadmin`` marker + data-scope breadth), so a
        target holding ``superadmin``, an ``all``-scope role, or any permission
        the actor lacks is unmanageable. Super-admin actors and role-less targets
        (zero conferred privilege) pass trivially.
        """
        if actor.is_superuser:
            return
        target_role_ids = await self.repo.list_role_ids(target.id)
        await self._require_assignable_roles(
            target_role_ids, actor, holder_department_id=target.department_id
        )

    async def _dominates_roles(
        self,
        role_ids: Sequence[int],
        actor: AuthenticatedUser,
        *,
        holder_department_id: int | None,
    ) -> bool:
        """Boolean form of :meth:`_require_dominance` for bulk pre-filtering.

        Same three-axis test (perm subset + ``superadmin`` marker + scope breadth)
        as the assignment guard, returning a verdict instead of raising — so a
        batch can quietly *skip* targets the actor cannot manage rather than fail
        the whole request. Super-admin and role-less targets dominate trivially.
        """
        if actor.is_superuser or not role_ids:
            return True
        try:
            await self._require_assignable_roles(
                role_ids, actor, holder_department_id=holder_department_id
            )
        except AppError:
            return False
        return True

    async def _require_unique_employee_no(self, employee_no: str) -> None:
        stmt = (
            select(User.id)
            .where(User.employee_no == employee_no)
            .where(User.is_deleted.is_(False))
        )
        if await self.session.scalar(stmt) is not None:
            raise AppError(ErrorCode.request_invalid, status.HTTP_400_BAD_REQUEST)

    def _require_password_strength(self, plain: str) -> None:
        """Reject a too-weak password with a machine-readable violation list.

        Single source of strength enforcement shared by every admin set-password
        path (create / update / reset). Without this, ``create_user`` and
        ``update_user`` would hash whatever the admin typed — letting a holder of
        ``system:user:add/edit`` plant a weak credential that bypasses the policy
        ``reset_password`` and self-service change both enforce.
        """
        violations = password_strength_violations(
            plain, min_length=get_settings().password_min_length
        )
        if violations:
            raise AppError(
                ErrorCode.auth_password_too_weak,
                status.HTTP_400_BAD_REQUEST,
                params={"violations": violations},
            )

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
        await self._require_assignable_roles(
            payload.role_ids, actor, holder_department_id=payload.department_id
        )
        if payload.password is not None:
            self._require_password_strength(payload.password)
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
        await self._require_dominance(user, actor)
        values = payload.model_dump(
            exclude_unset=True, exclude={"role_ids", "password", "status"}
        )
        password_changed = payload.password is not None
        if payload.password is not None:
            self._require_password_strength(payload.password)
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
            # Resolve the role guard against the holder's department AFTER this
            # update — moving a user into a child dept while granting a relative
            # scope would otherwise be evaluated against the stale old dept.
            new_department_id = values.get("department_id", user.department_id)
            await self._require_assignable_roles(
                payload.role_ids, actor, holder_department_id=new_department_id
            )
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
        # would silently leave the target's old sessions alive (B7).
        #
        # Disabling a user must ALSO kill every live session: the access-allowlist
        # entry and rotatable refresh both outlive the status flip on their own,
        # so a disabled user keeps a usable refresh token until it expires, and a
        # later re-enable would silently resurrect those stale sessions. Revoking
        # on disable forces a fresh login and severs the resurrection path.
        #
        # Revocation is SYNCHRONOUS before commit (not an after-commit hook): the
        # write is flushed, then the Redis revoke runs inside the request's unit
        # of work. A Redis failure raises and the request edge rolls the write
        # back — closing the "password/status changed in the DB but old sessions
        # still live" window a best-effort after-commit hook would leave open on a
        # Redis blip. (revoke_all_sessions is idempotent, so a password change that
        # also disables runs it once with no double-revoke hazard.)
        disabled_now = payload.status is not None and (
            payload.status == UserStatus.disabled
        )
        if password_changed or disabled_now:
            await self.session.flush()
            await self.auth.sessions.revoke_all_sessions(user_id)
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
        session standing on the old credential dies immediately.

        Revocation is SYNCHRONOUS before commit (not an after-commit hook): the
        password write is flushed, then the Redis revoke runs inside the request's
        unit of work. A Redis failure raises and the request edge rolls the
        password write back — so a Redis blip can never leave "password reset in
        the DB but the target's old sessions still alive".
        """
        user = await self._require_visible(user_id, actor)
        await self._require_dominance(user, actor)
        self._require_password_strength(new_password)
        user.password = await hash_password_async(new_password)
        user.updated_by = actor.user_id
        await self.repo.session.flush()
        await self.auth.sessions.revoke_all_sessions(user_id)

    async def delete_user(self, user_id: int, actor: AuthenticatedUser) -> None:
        user = await self._require_visible(user_id, actor)
        await self._require_dominance(user, actor)
        await self.repo.replace_roles(user_id, [])
        user.updated_by = actor.user_id
        await self.repo.soft_delete(user)
        # Soft-deleting must kill every live session: like disable, the
        # access-allowlist entry and refresh token outlive the row flip on their
        # own, so a deleted user keeps usable credentials until TTL. Revoke
        # SYNCHRONOUSLY before commit (Redis failure rolls the delete back), so a
        # Redis blip can never leave a deleted user with live sessions. The perm
        # snapshot drop stays a fail-open after-commit hook (TTL backstop bounds
        # staleness; it is not a security-critical revocation).
        await self.repo.session.flush()
        await self.auth.sessions.revoke_all_sessions(user_id)
        add_after_commit_hook(
            self.session, lambda: perm_cache.invalidate_user(user_id)
        )

    async def batch_delete_users(
        self, ids: Sequence[int], actor: AuthenticatedUser
    ) -> BatchResult:
        requested_ids = list(dict.fromkeys(ids))
        scope = await self._scope(actor)

        # Dominance pre-filter: a scoped actor may only delete targets it could
        # (re)assign the roles of — same guard the single-user delete enforces,
        # applied in bulk so an outranking target is skipped, not silently swept
        # out with the rest. Super-admin dominates everything (skip the lookup).
        dominance_skipped: list[int] = []
        deletable_ids = requested_ids
        if not actor.is_superuser:
            role_map = await self.repo.list_role_ids_for_users(requested_ids)
            dept_map = await self.repo.department_ids_for_users(requested_ids)
            deletable_ids = []
            for user_id in requested_ids:
                if await self._dominates_roles(
                    role_map.get(user_id, []),
                    actor,
                    holder_department_id=dept_map.get(user_id),
                ):
                    deletable_ids.append(user_id)
                else:
                    dominance_skipped.append(user_id)

        affected, scope_skipped_ids = await self.repo.soft_delete_many(
            deletable_ids,
            scope_predicate=self.repo._scope_predicate(scope, actor_id=actor.user_id),
        )
        skipped_ids = scope_skipped_ids + dominance_skipped
        skipped = set(skipped_ids)
        for user_id in requested_ids:
            if user_id not in skipped:
                await self.repo.replace_roles(user_id, [])
                # partial binds each id by value (a bare lambda would late-bind
                # the loop variable to its final value in every hook).
                add_after_commit_hook(
                    self.session, partial(perm_cache.invalidate_user, user_id)
                )
        # Each deleted user must lose every live session, exactly like the
        # single-user delete — otherwise a batch-deleted user keeps usable
        # credentials until TTL (the resurrection hole). Revoke SYNCHRONOUSLY
        # before commit: a Redis failure rolls the whole batch back rather than
        # leaving deleted users with live sessions. revoke_all_sessions is a
        # no-op for users with no active sessions, so this is cheap.
        deleted_ids = [uid for uid in requested_ids if uid not in skipped]
        if deleted_ids:
            await self.session.flush()
            for user_id in deleted_ids:
                await self.auth.sessions.revoke_all_sessions(user_id)
        return BatchResult.of(
            requested=len(requested_ids), affected=affected, skipped=skipped_ids
        )
