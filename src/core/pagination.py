"""Pagination DTOs shared by management-plane list endpoints."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Generic, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


class Page(BaseModel, Generic[T]):
    """Offset/limit page payload for admin list responses."""

    items: list[T]
    total: int = Field(ge=0)
    limit: int = Field(ge=1)
    offset: int = Field(ge=0)


@dataclass(frozen=True, slots=True)
class PageResult(Generic[T]):
    """Internal page result before ORM/domain objects are mapped to DTOs."""

    items: list[T]
    total: int
    limit: int
    offset: int


def page(items: list[T], *, total: int, limit: int, offset: int) -> Page[T]:
    """Build a typed page payload."""
    return Page[T](items=items, total=total, limit=limit, offset=offset)


def page_result(
    items: list[T], *, total: int, limit: int, offset: int
) -> PageResult[T]:
    """Build an internal page result."""
    return PageResult[T](items=items, total=total, limit=limit, offset=offset)
