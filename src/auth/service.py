"""Auth domain business logic (README: service layer, four-layer discipline).

Three responsibilities, all on top of :class:`AuthRepository` + the
``core.security`` primitives:

- **Authentication** — password login (issues a platform RS256 access token) and
  sk-key resolution (hash → lookup → expiry).
- **Authorization (RBAC)** — per-request permission-code aggregation; holding an
  active role with the ``superadmin`` code is super-admin.
- **Data scope** — resolve a user's effective department-id set (RuoYi
  6-tier), the primitive applied by admin user list + mutation queries.

Security primitives raise the infra-level :class:`TokenError`; this layer
translates every auth failure into an :class:`AppError` carrying an
``ErrorCode`` so the web layer renders a uniform envelope. To avoid handing an
attacker an oracle, bad-credential / bad-token / disabled-user cases all map
to the same opaque ``auth_invalid_token`` (401).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette import status

from src.auth import dept_tree_cache, perm_cache
from src.auth.constants import SUPERADMIN_ROLE_CODE
from src.auth.credentials import CredentialKind
from src.auth.dept_tree_cache import DeptPair, invalidate_department_tree
from src.auth.repository import AuthRepository
from src.config import get_settings
from src.core import cache
from src.core.login_throttle import LoginThrottle, ThrottlePolicy
from src.core.oss import ObjectStorage
from src.core.redis import get_redis
from src.core.security import (
    TokenError,
    decode_access_token,
    hash_api_key,
    hash_password_async,
    hash_refresh_token,
    issue_access_token,
    issue_refresh_token,
    password_strength_violations,
    verify_password_async,
)
from src.core.session_store import RefreshOutcome, SessionStore
from src.db.models.audit import LoginLog
from src.db.models.identity import Role
from src.db.session import unit_of_work
from src.enums import AuditOutcome, DataScope, ErrorCode, LoginFailureReason
from src.exceptions import AppError

__all__ = [
    "AuthService",
    "AuthenticatedUser",
    "DataScopeFilter",
    "invalidate_department_tree",
]


@dataclass(frozen=True, slots=True)
class AuthenticatedUser:
    """Resolved user for one request (from a JWT or an sk-key).

    Frozen value object: permissions are snapshotted at resolution time. The
    snapshot is served from a race-free per-user cache (see :mod:`perm_cache`)
    that is invalidated by an after-commit hook on every role/menu/user-role
    mutation, so a permission change takes effect on the very next request once
    the mutation commits — never carried inside the token.
    """

    user_id: int
    username: str
    department_id: int | None
    permissions: frozenset[str]
    role_codes: frozenset[str] = frozenset()
    real_name: str | None = None
    email: str | None = None
    mobile: str | None = None
    preferred_locale: str = "zh-CN"
    avatar: int | None = None
    credential_kind: CredentialKind = CredentialKind.jwt
    api_key_id: int | None = None

    @property
    def is_superuser(self) -> bool:
        """Super-admin iff holding an active role with the ``superadmin`` code.

        Code-based and bootstrap-safe: the marker is the role identity itself,
        not a ``menu -> role_menu -> perm`` chain that can be half-seeded. There
        is intentionally no wildcard-permission fallback.
        """
        return SUPERADMIN_ROLE_CODE in self.role_codes

    def has_permission(self, required: str) -> bool:
        return self.is_superuser or required in self.permissions


@dataclass(frozen=True, slots=True)
class DataScopeFilter:
    """A user's effective data-visibility scope, role-union resolved.

    ``unrestricted`` short-circuits everything (a role with ``all``). Otherwise a
    row is visible if its department is in ``department_ids`` OR the row's owner
    column matches the user when ``include_self`` is set. The admin query layer
    translates this into the actual ``WHERE`` clause; this struct is the portable
    primitive.
    """

    unrestricted: bool
    department_ids: frozenset[int]
    include_self: bool


def _now_utc() -> datetime:
    return datetime.now(tz=UTC)


def _is_expired(expires_at: datetime | None) -> bool:
    """Whether an sk-key expiry has passed. Null = never expires.

    A naive timestamp (SQLite round-trips ``timestamptz`` without tzinfo) is
    treated as UTC so the comparison never raises on a mixed backend.
    """
    if expires_at is None:
        return False
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    return expires_at < _now_utc()


def _collect_subtree(root_ids: Iterable[int], dept_pairs: Sequence[DeptPair]) -> set[int]:
    """Root department ids plus all their descendants (adjacency-list walk)."""
    children: dict[int | None, list[int]] = defaultdict(list)
    for dept_id, parent_id in dept_pairs:
        children[parent_id].append(dept_id)
    collected: set[int] = set()
    stack = list(root_ids)
    while stack:
        current = stack.pop()
        if current in collected:
            continue
        collected.add(current)
        stack.extend(children.get(current, ()))
    return collected


class AuthService:
    """Authentication + authorization use cases for the auth domain."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        audit_session_factory: async_sessionmaker[AsyncSession] | None = None,
    ) -> None:
        self.repo = AuthRepository(session)
        self.sessions = SessionStore(get_redis())
        # Login-attempt audit rows are written in their OWN unit of work so a
        # *failure* row survives the AppError that follows it — the request's own
        # session is rolled back on that error, which would otherwise erase the
        # audit trail. When not explicitly injected the factory is derived lazily
        # (see :meth:`_audit_factory`) from the SAME engine as the request
        # session: production lands in PG, tests land in the ephemeral SQLite —
        # no per-call-site wiring, and never a cross-environment leak to the real
        # PG from a test. Derivation is deferred so merely *constructing* the
        # service never touches ``session.bind`` (a bare stub session is fine for
        # the non-login code paths that don't audit).
        self._injected_audit_factory = audit_session_factory
        settings = get_settings()
        self.throttle = LoginThrottle(
            get_redis(),
            ThrottlePolicy(
                max_failures=settings.login_max_failures,
                lockout_seconds=settings.login_lockout_seconds,
                failure_window_seconds=settings.login_failure_window_seconds,
                ip_max_failures=settings.login_ip_max_failures,
                ip_window_seconds=settings.login_ip_window_seconds,
            ),
        )

    # ---- authentication ----------------------------------------------------

    async def authenticate_password(
        self,
        username: str,
        password: str,
        *,
        request_ip: str | None = None,
        user_agent: str | None = None,
        trace_id: str | None = None,
    ) -> tuple[str, int, str]:
        """Verify username/password, open a session, return (access, ttl, refresh).

        Wrong user, SSO-only user (null password), and wrong password all
        collapse to one opaque 401 — no user-enumeration oracle. Every attempt
        (success or failure) appends one ``login_log`` row recording the *real*
        internal :class:`LoginFailureReason`; that reason is audit-only and is
        never surfaced to the client.

        On success the access token's ``jti`` is registered in the Redis session
        allowlist and an opaque refresh token is stored (hashed), so the session
        is revocable (logout/kick) and the refresh is rotatable.

        Brute-force defense (B6): a per-username lockout and a per-IP sliding
        window are checked *before* the DB lookup + argon2 verify, so a locked or
        flooding caller is refused cheaply (no wasted hashing — a DoS guard). The
        lockout applies identically to real and non-existent usernames, so the
        ``auth_account_locked`` (429) response is **not** a user-enumeration
        oracle. A successful login clears the username's failure counter.
        """
        if await self.throttle.is_locked(username) or (
            request_ip is not None and await self.throttle.is_ip_limited(request_ip)
        ):
            await self._record_login(
                user_id=None,
                username=username,
                outcome=AuditOutcome.failure,
                failure_reason=LoginFailureReason.account_locked,
                request_ip=request_ip,
                user_agent=user_agent,
                trace_id=trace_id,
            )
            raise AppError(
                ErrorCode.auth_account_locked, status.HTTP_429_TOO_MANY_REQUESTS
            )
        user = await self.repo.get_user_by_username(username)
        if user is None:
            await self._record_login(
                user_id=None,
                username=username,
                outcome=AuditOutcome.failure,
                failure_reason=LoginFailureReason.user_not_found,
                request_ip=request_ip,
                user_agent=user_agent,
                trace_id=trace_id,
            )
            await self.throttle.record_failure(username, ip=request_ip)
            raise AppError(ErrorCode.auth_invalid_token, status.HTTP_401_UNAUTHORIZED)
        if user.password is None or not await verify_password_async(
            user.password, password
        ):
            await self._record_login(
                user_id=user.id,
                username=username,
                outcome=AuditOutcome.failure,
                failure_reason=LoginFailureReason.bad_credentials,
                request_ip=request_ip,
                user_agent=user_agent,
                trace_id=trace_id,
            )
            await self.throttle.record_failure(username, ip=request_ip)
            raise AppError(ErrorCode.auth_invalid_token, status.HTTP_401_UNAUTHORIZED)

        await self.throttle.reset(username)
        token, ttl, access_jti = issue_access_token(user.id)
        refresh_plain, refresh_hash = issue_refresh_token()
        await self.sessions.create_session(
            user_id=user.id,
            access_jti=access_jti,
            access_ttl=ttl,
            refresh_hash=refresh_hash,
            refresh_ttl=get_settings().platform_refresh_token_ttl_seconds,
            ip=request_ip,
            user_agent=user_agent,
        )
        await self._record_login(
            user_id=user.id,
            username=username,
            outcome=AuditOutcome.success,
            failure_reason=None,
            request_ip=request_ip,
            user_agent=user_agent,
            trace_id=trace_id,
        )
        return token, ttl, refresh_plain

    async def _record_login(
        self,
        *,
        user_id: int | None,
        username: str,
        outcome: AuditOutcome,
        failure_reason: LoginFailureReason | None,
        request_ip: str | None,
        user_agent: str | None,
        trace_id: str | None,
    ) -> None:
        """Append one login-attempt audit row in its OWN unit of work.

        Written through an independent session (not the request's) so a *failure*
        row survives the ``AppError`` that follows it: the request session is
        rolled back on that error under the request-level Unit of Work, which
        would otherwise erase the audit trail. The factory is injected so tests
        bind it to the ephemeral test DB. Audit failures are NOT swallowed
        (fail-closed): a broken audit path surfaces rather than silently losing
        the security trail.
        """
        row = LoginLog(
            user_id=user_id,
            username=username,
            status=outcome.value,
            failure_reason=failure_reason.value if failure_reason is not None else None,
            request_ip=request_ip,
            user_agent=user_agent,
            trace_id=trace_id,
        )
        async with unit_of_work(self._audit_factory()) as session:
            await AuthRepository(session).append_login_log(row)

    def _audit_factory(self) -> async_sessionmaker[AsyncSession]:
        """Resolve the audit unit-of-work factory, deriving it lazily.

        If one was injected at construction, use it. Otherwise build a factory
        bound to the SAME engine as the request session (so a test's ephemeral
        SQLite never leaks an audit write to the real PG) — deferred to here so
        constructing the service never requires a live ``session.bind`` for code
        paths that don't audit.
        """
        if self._injected_audit_factory is not None:
            return self._injected_audit_factory
        return async_sessionmaker(
            bind=self.repo.session.bind,
            expire_on_commit=False,
            autoflush=False,
        )

    async def resolve_access_token(self, token: str) -> AuthenticatedUser:
        """Verify a platform JWT and build the request principal (perms from DB).

        Beyond signature/claims validation, the token's ``jti`` must still be in
        the Redis session allowlist; a revoked session (logout / kick / refresh
        reuse) fails with ``auth_token_revoked`` even while the JWT is otherwise
        cryptographically valid and unexpired.
        """
        try:
            claims = decode_access_token(token)
            user_id = int(claims.sub)
        except (TokenError, ValueError) as exc:
            raise AppError(
                ErrorCode.auth_invalid_token, status.HTTP_401_UNAUTHORIZED
            ) from exc
        if not await self.sessions.is_access_active(claims.jti):
            raise AppError(
                ErrorCode.auth_token_revoked, status.HTTP_401_UNAUTHORIZED
            )
        return await self._build_user(user_id, credential_kind=CredentialKind.jwt)

    async def logout(self, token: str) -> None:
        """Revoke the session behind a platform access token (logout).

        Decodes for the ``jti`` and drops the session (access allowlist entry,
        user-index member, and the bound refresh). Idempotent: a token whose
        session is already gone is a no-op. An undecodable token is rejected so
        a malformed logout can't masquerade as success.
        """
        try:
            claims = decode_access_token(token)
        except TokenError as exc:
            raise AppError(
                ErrorCode.auth_invalid_token, status.HTTP_401_UNAUTHORIZED
            ) from exc
        await self.sessions.revoke_access(claims.jti)

    async def refresh_session(
        self,
        refresh_token: str,
        *,
        request_ip: str | None = None,
        user_agent: str | None = None,
    ) -> tuple[str, int, str]:
        """Rotate a refresh token into a fresh (access, ttl, refresh) triple.

        Atomically consumes the presented refresh (only one concurrent caller
        wins); on success mints a new access+refresh pair, registers the new
        session, and revokes the old access token so the rotated-away access
        stops working immediately. A refresh that is unknown, expired, or already
        rotated (reuse — the store has by then revoked the whole user session as
        a theft signal) fails with ``auth_refresh_invalid``. Refresh uses a
        sliding window: each rotation grants a fresh full refresh lifetime.

        ``request_ip``/``user_agent`` are re-captured onto the rotated session so
        the online-session list (B5) reflects the latest client context.
        """
        refresh_hash = hash_refresh_token(refresh_token)
        settings = get_settings()
        refresh_ttl = settings.platform_refresh_token_ttl_seconds
        result = await self.sessions.consume_refresh(
            refresh_hash, used_marker_ttl=refresh_ttl
        )
        if (
            result.outcome is not RefreshOutcome.ok
            or result.user_id is None
            or result.old_access_jti is None
        ):
            raise AppError(
                ErrorCode.auth_refresh_invalid, status.HTTP_401_UNAUTHORIZED
            )

        token, ttl, access_jti = issue_access_token(result.user_id)
        new_refresh_plain, new_refresh_hash = issue_refresh_token()
        await self.sessions.create_session(
            user_id=result.user_id,
            access_jti=access_jti,
            access_ttl=ttl,
            refresh_hash=new_refresh_hash,
            refresh_ttl=refresh_ttl,
            ip=request_ip,
            user_agent=user_agent,
        )
        await self.sessions.revoke_access(result.old_access_jti)
        return token, ttl, new_refresh_plain

    async def resolve_api_key(self, plaintext: str) -> AuthenticatedUser:
        """Resolve an sk-key to its owner principal (hash → lookup → expiry)."""
        api_key = await self.repo.get_api_key_by_hash(hash_api_key(plaintext))
        if api_key is None or _is_expired(api_key.expires_at):
            raise AppError(ErrorCode.auth_invalid_token, status.HTTP_401_UNAUTHORIZED)
        return await self._build_user(
            api_key.user_id,
            credential_kind=CredentialKind.api_key,
            api_key_id=api_key.id,
        )

    async def _build_user(
        self,
        user_id: int,
        *,
        credential_kind: CredentialKind = CredentialKind.jwt,
        api_key_id: int | None = None,
    ) -> AuthenticatedUser:
        """Load the still-active user and snapshot its permission set."""
        user = await self.repo.get_user_by_id(user_id)
        if user is None:
            # Credential was valid but the user is gone/disabled since issue.
            raise AppError(ErrorCode.auth_invalid_token, status.HTTP_401_UNAUTHORIZED)
        # The user row (enabled-status check) stays a per-request DB read — it is
        # the security floor and must reflect a disable instantly. Only the two
        # expensive RBAC aggregations are cached, behind perm_cache's race-free
        # versioned-key scheme (invalidated by after-commit hooks on every
        # role/menu/user-role mutation).
        snapshot = await perm_cache.load_snapshot(
            user_id,
            lambda: self._load_permission_snapshot(user_id),
        )
        return AuthenticatedUser(
            user_id=user_id,
            username=user.username,
            department_id=user.department_id,
            permissions=snapshot.permissions,
            role_codes=snapshot.role_codes,
            real_name=user.real_name,
            email=user.email,
            mobile=user.mobile,
            preferred_locale=user.preferred_locale,
            avatar=user.avatar,
            credential_kind=credential_kind,
            api_key_id=api_key_id,
        )

    async def _load_permission_snapshot(
        self, user_id: int
    ) -> perm_cache.PermissionSnapshot:
        """Aggregate the user's permission + role codes from the DB (cache loader).

        The cache-miss path for :func:`perm_cache.load_snapshot` — runs the two
        RBAC aggregations that are otherwise on every request's hot path.
        """
        return perm_cache.PermissionSnapshot(
            permissions=await self.repo.list_permission_codes(user_id),
            role_codes=await self.repo.list_active_role_codes(user_id),
        )

    async def update_current_user(
        self,
        user: AuthenticatedUser,
        values: Mapping[str, str | None],
    ) -> AuthenticatedUser:
        """Update self-service profile fields and return a fresh principal.

        ``avatar`` is special: it carries an attachment id (string) the caller
        uploaded, or ``None`` to clear. A non-null value is validated to be an
        avatar attachment owned by the caller (else ``attach_not_found`` / 404),
        so a user cannot bind someone else's object as their picture.
        """
        row = await self.repo.get_user_by_id(user.user_id)
        if row is None:
            raise AppError(ErrorCode.auth_invalid_token, status.HTTP_401_UNAUTHORIZED)
        if values.get("preferred_locale") is None and "preferred_locale" in values:
            raise AppError(ErrorCode.request_invalid, status.HTTP_400_BAD_REQUEST)
        for field in ("real_name", "email", "mobile", "preferred_locale"):
            if field in values:
                setattr(row, field, values[field])
        if "avatar" in values:
            row.avatar = await self._resolve_avatar_binding(values["avatar"], user)
        row.updated_by = user.user_id
        await self.repo.session.flush()
        return await self._build_user(user.user_id, credential_kind=user.credential_kind)

    async def _resolve_avatar_binding(
        self, avatar: str | None, user: AuthenticatedUser
    ) -> int | None:
        """Resolve an avatar-binding value to a validated attachment id (or None).

        ``None`` clears the avatar. A non-null value must parse as an int and be
        an avatar attachment owned by the caller; otherwise raise
        ``attach_not_found`` (404) — a malformed id and a non-owned/absent id are
        deliberately the same opaque outcome.
        """
        if avatar is None:
            return None
        try:
            attach_id = int(avatar)
        except ValueError as exc:
            raise AppError(
                ErrorCode.attach_not_found, status.HTTP_404_NOT_FOUND
            ) from exc
        owned = await self.repo.get_owned_avatar(attach_id, user.user_id)
        if owned is None:
            raise AppError(ErrorCode.attach_not_found, status.HTTP_404_NOT_FOUND)
        return owned.id

    async def avatar_url(
        self, user: AuthenticatedUser, storage: ObjectStorage | None
    ) -> str | None:
        """Presigned GET URL for the user's bound avatar (None if unset/unavailable).

        Returns ``None`` when the user has no avatar, when object storage is not
        configured (``storage is None``), or when the referenced attachment was
        soft-deleted. Read-only enrichment for ``/me`` — never raises, so a missing
        avatar or unconfigured OSS degrades to "no picture" rather than a 500.
        """
        if user.avatar is None or storage is None:
            return None
        object_key = await self.repo.get_attach_object_key(user.avatar)
        if object_key is None:
            return None
        return await storage.presign_get(object_key)

    async def change_current_password(
        self, user: AuthenticatedUser, *, old_password: str, new_password: str
    ) -> None:
        """Change the current user's password after verifying the old one.

        Enforces the strength policy (length + must contain a letter and a digit)
        before hashing; a weak password fails with ``auth_password_too_weak``
        (400) carrying the machine-readable violation labels in ``params`` for
        frontend i18n. On success **all** of the user's sessions are revoked
        (B7 — force re-login on every device, including the current one), so a
        leaked-credential change immediately invalidates any hijacked session.

        Revocation is **synchronous before commit** (not an after-commit hook):
        the password write is flushed, then the Redis revoke runs inside the
        request's unit of work. If Redis is unreachable the revoke raises and the
        request-level UoW rolls the password write back — so we never land in the
        "password changed in the DB but old sessions still alive" state a
        best-effort after-commit hook would leave on a Redis blip. The worst case
        is the inverse (sessions killed but password unchanged on a later commit
        failure), which is safe: the user simply re-logs in with the old password.
        """
        violations = password_strength_violations(
            new_password, min_length=get_settings().password_min_length
        )
        if violations:
            raise AppError(
                ErrorCode.auth_password_too_weak,
                status.HTTP_400_BAD_REQUEST,
                params={"violations": violations},
            )
        row = await self.repo.get_user_by_id(user.user_id)
        if row is None:
            raise AppError(ErrorCode.auth_invalid_token, status.HTTP_401_UNAUTHORIZED)
        if row.password is None or not await verify_password_async(
            row.password, old_password
        ):
            raise AppError(ErrorCode.auth_invalid_token, status.HTTP_401_UNAUTHORIZED)
        row.password = await hash_password_async(new_password)
        row.updated_by = user.user_id
        await self.repo.session.flush()
        await self.sessions.revoke_all_sessions(user.user_id)

    # ---- authorization ------------------------------------------------------

    @staticmethod
    def require_permission(user: AuthenticatedUser, required: str) -> None:
        """Raise 403 unless the user holds ``required`` (or is super-admin)."""
        if not user.has_permission(required):
            raise AppError(ErrorCode.auth_forbidden, status.HTTP_403_FORBIDDEN)

    async def permissions_for_roles(self, role_ids: Sequence[int]) -> frozenset[str]:
        """Permission codes a given set of roles would confer (escalation guard)."""
        return await self.repo.list_permission_codes_for_roles(role_ids)

    async def permissions_for_menus(self, menu_ids: Sequence[int]) -> frozenset[str]:
        """Permission codes a given set of menus would confer (role-edit guard)."""
        return await self.repo.list_permission_codes_for_menus(menu_ids)

    async def roles_for_assignment(self, role_ids: Sequence[int]) -> Sequence[Role]:
        """Active role rows for an assignment escalation guard.

        Exposes each role's ``code`` (super-admin marker) and ``data_scope``
        (visibility breadth) so the user-admin layer can reject assigning a role
        broader than the actor — neither is observable through the perms-only
        subset check (a ``superadmin`` role with no menus confers zero perms, and
        an ``all``-scope role confers no perm code yet grants unrestricted view).
        """
        return await self.repo.list_active_roles_by_ids(role_ids)

    async def role_department_ids(self, role_ids: Sequence[int]) -> frozenset[int]:
        """Custom-scope department ids granted to a set of roles (scope guard)."""
        return await self.repo.list_role_department_ids(role_ids)

    async def resolve_data_scope(self, user: AuthenticatedUser) -> DataScopeFilter:
        """Resolve the user's effective data scope (broadest role wins).

        Super-admin (``superadmin`` role code) is unconditionally unrestricted
        regardless of other roles. Otherwise union semantics across all active
        roles: ``all`` beats everything; the rest accumulate department ids
        and/or the self flag. No active role → the most restrictive scope (self
        only).
        """
        if user.is_superuser:
            return DataScopeFilter(
                unrestricted=True, department_ids=frozenset(), include_self=False
            )
        roles = await self.repo.list_active_roles(user.user_id)
        if not roles:
            return DataScopeFilter(
                unrestricted=False, department_ids=frozenset(), include_self=True
            )
        if any(role.data_scope == DataScope.all_data.value for role in roles):
            return DataScopeFilter(
                unrestricted=True, department_ids=frozenset(), include_self=False
            )
        return await self._accumulate_scope(user, roles)

    async def _accumulate_scope(
        self, user: AuthenticatedUser, roles: Sequence[Role]
    ) -> DataScopeFilter:
        department_ids: set[int] = set()
        subtree_roots: set[int] = set()
        include_self = False
        own_dept = user.department_id

        custom_role_ids = [
            role.id for role in roles if role.data_scope == DataScope.custom.value
        ]
        if custom_role_ids:
            department_ids |= await self.repo.list_role_department_ids(custom_role_ids)

        for role in roles:
            scope = role.data_scope
            if scope == DataScope.dept_only.value and own_dept is not None:
                department_ids.add(own_dept)
            elif scope == DataScope.dept_and_child.value and own_dept is not None:
                subtree_roots.add(own_dept)
            elif scope == DataScope.self_only.value:
                include_self = True
            elif scope == DataScope.dept_and_child_or_self.value:
                if own_dept is not None:
                    subtree_roots.add(own_dept)
                include_self = True

        if subtree_roots:
            department_ids |= _collect_subtree(subtree_roots, await self._load_dept_tree())

        return DataScopeFilter(
            unrestricted=False,
            department_ids=frozenset(department_ids),
            include_self=include_self,
        )

    async def _load_dept_tree(self) -> list[DeptPair]:
        """Department adjacency, cache-aside (short TTL, fail-open to DB)."""

        async def _from_db() -> list[DeptPair]:
            depts = await self.repo.list_all_departments()
            return [(d.id, d.parent_id) for d in depts]

        return await cache.get_or_load(
            dept_tree_cache.CACHE_KEY,
            _from_db,
            ttl_seconds=dept_tree_cache.TTL_SECONDS,
            dumps=dept_tree_cache.encode,
            loads=dept_tree_cache.decode,
        )
