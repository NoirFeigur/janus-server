from __future__ import annotations

import pytest
from pydantic import ValidationError
from sqlalchemy.orm.attributes import InstrumentedAttribute

from src.core.query import BatchIdsRequest, BatchResult, ListQuery, resolve_sort
from src.db.models import User
from src.enums import ErrorCode
from src.exceptions import AppError


def test_list_query_defaults_offset_zero_limit_50() -> None:
    query = ListQuery()
    assert query.offset == 0
    assert query.limit == 50
    assert query.sort_order == "asc"
    assert query.keyword is None
    assert query.sort_by is None


def test_list_query_rejects_unknown_sort_field_returns_error() -> None:
    allowed: dict[str, InstrumentedAttribute[str]] = {"username": User.username}
    query = ListQuery(sort_by="; DROP TABLE")
    with pytest.raises(AppError) as exc_info:
        resolve_sort(query, allowed=allowed, default="username")
    assert exc_info.value.code == ErrorCode.request_invalid
    assert exc_info.value.status_code == 400


def test_list_query_resolves_allowed_sort_desc() -> None:
    allowed: dict[str, InstrumentedAttribute[str]] = {"username": User.username}
    query = ListQuery(sort_by="username", sort_order="desc")
    column, descending = resolve_sort(query, allowed=allowed, default="username")
    assert column is User.username
    assert descending is True


def test_list_query_limit_over_200_rejected() -> None:
    with pytest.raises(ValidationError):
        ListQuery(limit=201)


def test_batch_ids_request_coerces_string_ids() -> None:
    request = BatchIdsRequest(ids=["1", "2"])
    assert request.ids == [1, 2]


def test_batch_result_of_stringifies_skipped() -> None:
    result = BatchResult.of(requested=2, affected=1, skipped=[12345])
    assert result.skipped_ids == ["12345"]
