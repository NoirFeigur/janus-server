"""Auth domain business logic (README: service layer, four-layer discipline).

Three responsibilities, all on top of :class:`AuthRepository` + the
``core.security`` primitives:

- **Authentication** — password login (issues a platform RS256 access token) and
  sk-key resolution (hash → lookup → expiry).
- **Authorization (RBAC)** — per-request permission-code aggregation; a wildcard
  ``*:*:*`` grant is super-admin.
- **Data scope** — resolve an account's effective department-id set (RuoYi
  6-tier), the primitive applied by admin user/account list + mutation queries.

Security primitives raise the infra-level :class:`TokenError`; this layer
translates every auth failure into an :class:`AppError` carrying an
``ErrorCode`` so the web layer renders a uniform envelope. To avoid handing an
attacker an oracle, bad-credential / bad-token / disabled-account cases all map
to the same opaque ``auth_invalid_token`` (401).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession
from starlette import status

from src.auth import dept_tree_cache
from src.auth.dept_tree_cache import DeptPair, invalidate_department_tree
from src.auth.repository import AuthRepository
from src.core import cache
from src.core.security import (
    TokenError,
    decode_access_token,
    hash_api_key,
    issue_access_token,
    verify_password_async,
)
from src.db.models.identity import Role
from src.enums import DataScope, ErrorCode
from src.exceptions import AppError

__all__ = [
    "AuthService",
    "AuthenticatedAccount",
    "DataScopeFilter",
    "invalidate_department_tree",
]

_SUPERUSER_PERM = "*:*:*"  # Wildcard grant: bypasses every permission check.


@dataclass(frozen=True, slots=True)
class AuthenticatedAccount:
    """Resolved principal for one request (from a JWT or an sk-key).

    Frozen value object: permissions are snapshotted at resolution time (per
    request, straight from DB — never cached in the token), so a role change
    takes effect on the very next request.
    """

    account_id: int
    username: str
    department_id: int | None
    permissions: frozenset[str]

    @property
    def is_superuser(self) -> bool:
        return _SUPERUSER_PERM in self.permissions

    def has_permission(self, required: str) -> bool:
        return self.is_superuser or required in self.permissions


@dataclass(frozen=True, slots=True)
class DataScopeFilter:
    """An account's effective data-visibility scope, role-union resolved.

    ``unrestricted`` short-circuits everything (a role with ``all``). Otherwise a
    row is visible if its department is in ``department_ids`` OR (``include_self``
    and the row was created by the account). The admin query layer translates this
    into the actual ``WHERE`` clause; this struct is the portable primitive.
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

    def __init__(self, session: AsyncSession) -> None:
        self.repo = AuthRepository(session)

    # ---- authentication ----------------------------------------------------

    async def authenticate_password(self, username: str, password: str) -> tuple[str, int]:
        """Verify username/password, issue a platform access token (token, ttl).

        Wrong user, SSO-only account (null password), and wrong password all
        collapse to one opaque 401 — no user-enumeration oracle.
        """
        user = await self.repo.get_user_by_username(username)
        if user is None or user.password is None:
            raise AppError(ErrorCode.auth_invalid_token, status.HTTP_401_UNAUTHORIZED)
        if not await verify_password_async(user.password, password):
            raise AppError(ErrorCode.auth_invalid_token, status.HTTP_401_UNAUTHORIZED)
        return issue_access_token(user.id)

    async def resolve_access_token(self, token: str) -> AuthenticatedAccount:
        """Verify a platform JWT and build the request principal (perms from DB)."""
        try:
            claims = decode_access_token(token)
            account_id = int(claims.sub)
        except (TokenError, ValueError) as exc:
            raise AppError(
                ErrorCode.auth_invalid_token, status.HTTP_401_UNAUTHORIZED
            ) from exc
        return await self._build_principal(account_id)

    async def resolve_api_key(self, plaintext: str) -> AuthenticatedAccount:
        """Resolve an sk-key to its owner principal (hash → lookup → expiry)."""
        api_key = await self.repo.get_api_key_by_hash(hash_api_key(plaintext))
        if api_key is None or _is_expired(api_key.expires_at):
            raise AppError(ErrorCode.auth_invalid_token, status.HTTP_401_UNAUTHORIZED)
        return await self._build_principal(api_key.user_id)

    async def _build_principal(self, account_id: int) -> AuthenticatedAccount:
        """Load the (still-active) account and snapshot its permission set."""
        user = await self.repo.get_user_by_id(account_id)
        if user is None:
            # Credential was valid but the account is gone/disabled since issue.
            raise AppError(ErrorCode.auth_invalid_token, status.HTTP_401_UNAUTHORIZED)
        permissions = await self.repo.list_permission_codes(account_id)
        return AuthenticatedAccount(
            account_id=account_id,
            username=user.username,
            department_id=user.department_id,
            permissions=permissions,
        )

    # ---- authorization ------------------------------------------------------

    @staticmethod
    def require_permission(account: AuthenticatedAccount, required: str) -> None:
        """Raise 403 unless the account holds ``required`` (or is super-admin)."""
        if not account.has_permission(required):
            raise AppError(ErrorCode.auth_forbidden, status.HTTP_403_FORBIDDEN)

    async def permissions_for_roles(self, role_ids: Sequence[int]) -> frozenset[str]:
        """Permission codes a given set of roles would confer (escalation guard)."""
        return await self.repo.list_permission_codes_for_roles(role_ids)

    async def resolve_data_scope(self, account: AuthenticatedAccount) -> DataScopeFilter:
        """Resolve the account's effective data scope (broadest role wins).

        Super-admin (``*:*:*``) is unconditionally unrestricted regardless of
        roles. Otherwise union semantics across all active roles: ``all`` beats
        everything; the rest accumulate department ids and/or the self flag. No
        active role → the most restrictive scope (self only).
        """
        if account.is_superuser:
            return DataScopeFilter(
                unrestricted=True, department_ids=frozenset(), include_self=False
            )
        roles = await self.repo.list_active_roles(account.account_id)
        if not roles:
            return DataScopeFilter(
                unrestricted=False, department_ids=frozenset(), include_self=True
            )
        if any(role.data_scope == DataScope.all_data.value for role in roles):
            return DataScopeFilter(
                unrestricted=True, department_ids=frozenset(), include_self=False
            )
        return await self._accumulate_scope(account, roles)

    async def _accumulate_scope(
        self, account: AuthenticatedAccount, roles: Sequence[Role]
    ) -> DataScopeFilter:
        department_ids: set[int] = set()
        subtree_roots: set[int] = set()
        include_self = False
        own_dept = account.department_id

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
