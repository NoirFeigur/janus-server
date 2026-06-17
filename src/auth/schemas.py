"""Auth domain DTOs (router layer contracts).

Request/response shapes for the auth endpoints. Wire-facing models only — the
service layer speaks domain objects (``AuthenticatedUser``), these are the
HTTP boundary.

``UserRead`` never carries ``password`` (§0.8 iron rule): the column stores a
hash, but no read model ever exposes it. ``TokenRead`` reserves an optional
``refresh_token`` (always ``None`` in M1) so the M6 refresh-token rollout is a
field population, not a contract break.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class LoginRequest(BaseModel):
    """Username/password login payload."""

    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)


class CurrentUserUpdate(BaseModel):
    """Self-service profile fields the current user may update."""

    real_name: str | None = Field(default=None, max_length=64)
    email: str | None = Field(default=None, max_length=255)
    mobile: str | None = Field(default=None, max_length=32)
    preferred_locale: str | None = Field(default=None, max_length=16)


class ChangePasswordRequest(BaseModel):
    """Current-user password change payload."""

    old_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=1, max_length=128)


class TokenRead(BaseModel):
    """Issued platform access token (+ forward-compat refresh slot)."""

    access_token: str
    token_type: str = "Bearer"
    expires_in: int  # Access-token lifetime in seconds.
    refresh_token: str | None = None  # Reserved for M6; always None in M1.


class CurrentUserRead(BaseModel):
    """The authenticated principal's own profile + effective permissions.

    Snowflake ids are serialized as **strings** (``user_id``/``department_id``):
    they are int64 and the janus-web client is JS, whose ``Number`` loses
    precision past 2^53. String on the wire is the project-wide id convention.
    """

    user_id: str
    username: str
    real_name: str | None = None
    email: str | None = None
    mobile: str | None = None
    department_id: str | None
    preferred_locale: str
    permissions: list[str]  # Sorted effective permission codes (granular grants).
    is_superuser: bool  # True iff holding an active role with the superadmin code.
