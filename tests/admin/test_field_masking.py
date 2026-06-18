from __future__ import annotations

from pydantic import BaseModel

from src.auth.constants import SUPERADMIN_ROLE_CODE
from src.auth.service import AuthenticatedUser
from src.core.query import mask_fields


class _ContactRead(BaseModel):
    username: str
    mobile: str | None = None
    email: str | None = None


def _make_actor(*, superuser: bool) -> AuthenticatedUser:
    role_codes = frozenset({SUPERADMIN_ROLE_CODE}) if superuser else frozenset()
    return AuthenticatedUser(
        user_id=1,
        username="alice",
        department_id=None,
        permissions=frozenset(),
        role_codes=role_codes,
    )


def test_mask_fields_masks_mobile_and_email_for_non_superuser() -> None:
    read = _ContactRead(username="alice", mobile="13800001111", email="alice@x.com")
    actor = _make_actor(superuser=False)
    masked = mask_fields(read, actor=actor)
    assert masked.mobile == "138****1111"
    assert masked.email == "a***@x.com"
    assert masked.username == "alice"
    # original object is not mutated in place.
    assert read.mobile == "13800001111"
    assert read.email == "alice@x.com"


def test_mask_fields_superuser_unchanged() -> None:
    read = _ContactRead(username="alice", mobile="13800001111", email="alice@x.com")
    actor = _make_actor(superuser=True)
    masked = mask_fields(read, actor=actor)
    assert masked is read


def test_mask_fields_none_stays_none() -> None:
    read = _ContactRead(username="alice", mobile=None, email=None)
    actor = _make_actor(superuser=False)
    masked = mask_fields(read, actor=actor)
    assert masked.mobile is None
    assert masked.email is None
