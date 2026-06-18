"""Reusable data-scope WHERE predicate for ``BaseEntity`` business tables.

RBAC data-scope (§1.5) restricts which rows a non-superuser actor sees. The
*generic* rule keys off the audit columns every :class:`BaseEntity` carries —
``create_dept`` (owning department) and ``created_by`` (owning user) — so any
business domain (role / api_key / channel / grant / quota …) gets the same
visibility predicate for free instead of hand-rolling the SQL per repository.

Layering: this lives in the ``db`` layer and must not import from ``auth`` (that
would be an upward dependency). The resolved scope is consumed structurally via a
:class:`Protocol`, so :class:`src.auth.service.DataScopeFilter` satisfies it
without an import.

Note: ``User`` deliberately does NOT use this — its visibility keys off
``department_id`` (membership) and ``id`` (identity), not the audit columns, so it
keeps its own predicate. This helper is for the audit-column-owned majority.
"""

from __future__ import annotations

from typing import Protocol

from sqlalchemy import or_
from sqlalchemy.sql.elements import ColumnElement

from src.db.base import BaseEntity


class DataScope(Protocol):
    """Structural view of a resolved data scope (DataScopeFilter satisfies it).

    Members are read-only (``@property``) so a frozen dataclass like
    :class:`src.auth.service.DataScopeFilter` structurally conforms — a plain
    attribute declaration would demand a settable field and reject frozen ones.
    """

    @property
    def unrestricted(self) -> bool: ...

    @property
    def department_ids(self) -> frozenset[int]: ...

    @property
    def include_self(self) -> bool: ...


def data_scope_predicate(
    model: type[BaseEntity], scope: DataScope, *, actor_id: int
) -> ColumnElement[bool] | None:
    """Build the audit-column data-scope predicate for ``model``.

    Returns ``None`` for an unrestricted scope (no WHERE restriction). A
    restricted scope matches rows whose ``create_dept`` is in the allowed set OR
    (when ``include_self``) whose ``created_by`` is the actor. A restricted scope
    with neither allowance matches nothing (``id == -1``) — fail-closed, never
    leak rows on a misconfigured scope.
    """
    if scope.unrestricted:
        return None
    clauses: list[ColumnElement[bool]] = []
    if scope.department_ids:
        clauses.append(model.create_dept.in_(scope.department_ids))
    if scope.include_self:
        clauses.append(model.created_by == actor_id)
    if not clauses:
        return model.id == -1
    return or_(*clauses)
