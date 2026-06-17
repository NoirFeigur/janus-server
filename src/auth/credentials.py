"""HTTP credential parsing shared by auth middleware and dependencies.

The platform has two credential classes:
- JWT: interactive/admin session credential.
- sk-key: programmatic credential for LLM/MCP protocol calls only.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from starlette import status

from src.enums import ErrorCode
from src.exceptions import AppError

_SK_PREFIX = "sk-"


class CredentialKind(StrEnum):
    """Credential class detected at the HTTP boundary."""

    jwt = "jwt"
    api_key = "api_key"


@dataclass(frozen=True, slots=True)
class Credential:
    """Raw credential plus its detected class."""

    value: str
    kind: CredentialKind


def extract_credential(
    authorization: str | None,
    x_api_key: str | None,
    *,
    allow_api_key: bool,
) -> Credential:
    """Extract the caller credential from HTTP headers.

    ``X-API-Key`` is always an sk-key. ``Authorization: Bearer sk-...`` is also
    an sk-key; any other bearer value is treated as a JWT. When ``allow_api_key``
    is false, sk-key credentials are rejected even if syntactically valid.
    """
    if x_api_key:
        if not allow_api_key:
            raise AppError(ErrorCode.auth_invalid_token, status.HTTP_401_UNAUTHORIZED)
        return Credential(x_api_key, CredentialKind.api_key)

    if authorization:
        scheme, _, value = authorization.partition(" ")
        if scheme.lower() == "bearer" and value:
            kind = CredentialKind.api_key if value.startswith(_SK_PREFIX) else CredentialKind.jwt
            if kind == CredentialKind.api_key and not allow_api_key:
                raise AppError(ErrorCode.auth_invalid_token, status.HTTP_401_UNAUTHORIZED)
            return Credential(value, kind)

    raise AppError(ErrorCode.auth_invalid_token, status.HTTP_401_UNAUTHORIZED)
