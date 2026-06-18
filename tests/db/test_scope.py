"""Tests for the reusable data-scope predicate (src/db/scope.py)."""

from __future__ import annotations

from dataclasses import dataclass

from src.db.models.identity import Role
from src.db.scope import data_scope_predicate


@dataclass(frozen=True)
class _Scope:
    unrestricted: bool
    department_ids: frozenset[int]
    include_self: bool


def test_unrestricted_scope_returns_none() -> None:
    scope = _Scope(unrestricted=True, department_ids=frozenset(), include_self=False)
    assert data_scope_predicate(Role, scope, actor_id=1) is None


def test_department_scope_builds_create_dept_in_clause() -> None:
    scope = _Scope(
        unrestricted=False, department_ids=frozenset({10, 20}), include_self=False
    )
    predicate = data_scope_predicate(Role, scope, actor_id=1)
    assert predicate is not None
    # Compiles to a create_dept IN (...) predicate against the Role table.
    compiled = str(predicate.compile(compile_kwargs={"literal_binds": True}))
    assert "create_dept" in compiled
    assert "IN" in compiled.upper()


def test_include_self_builds_created_by_clause() -> None:
    scope = _Scope(unrestricted=False, department_ids=frozenset(), include_self=True)
    predicate = data_scope_predicate(Role, scope, actor_id=42)
    assert predicate is not None
    compiled = str(predicate.compile(compile_kwargs={"literal_binds": True}))
    assert "created_by" in compiled
    assert "42" in compiled


def test_dept_and_self_are_or_combined() -> None:
    scope = _Scope(
        unrestricted=False, department_ids=frozenset({5}), include_self=True
    )
    predicate = data_scope_predicate(Role, scope, actor_id=7)
    assert predicate is not None
    compiled = str(predicate.compile(compile_kwargs={"literal_binds": True}))
    assert "create_dept" in compiled
    assert "created_by" in compiled
    assert " OR " in compiled.upper()


def test_restricted_scope_with_no_allowances_matches_nothing() -> None:
    """Fail-closed: a restricted scope granting neither dept nor self leaks no rows."""
    scope = _Scope(unrestricted=False, department_ids=frozenset(), include_self=False)
    predicate = data_scope_predicate(Role, scope, actor_id=1)
    assert predicate is not None
    compiled = str(predicate.compile(compile_kwargs={"literal_binds": True}))
    # id == -1 → impossible, matches no row.
    assert "-1" in compiled
