"""Tests for the declarative base and entity base classes (src/db/base.py).

Assertions are invariants/contracts (per README "不变式 > 快照"), not value
snapshots: we check column *presence and shape*, not a frozen table count.
"""

from __future__ import annotations

from src.db import models  # noqa: F401 — register every model on Base.metadata.
from src.db.base import Base, BaseEntity, LinkEntity, LogEntity


def test_all_registered_models_have_a_primary_key() -> None:
    """Every mapped table must declare exactly one PK column named ``id``."""
    assert Base.metadata.tables  # at least one model is registered
    for table in Base.metadata.tables.values():
        pk_cols = list(table.primary_key.columns)
        assert len(pk_cols) == 1, f"{table.name} must have a single-column PK"
        assert pk_cols[0].name == "id", f"{table.name} PK must be named 'id'"


def test_base_entity_has_softdelete_and_audit_columns() -> None:
    cols = {c.name for c in BaseEntity.__table__.columns} if hasattr(
        BaseEntity, "__table__"
    ) else set()
    # BaseEntity is abstract; inspect a concrete subclass instead.
    user = Base.metadata.tables["users"]
    names = set(user.columns.keys())
    for required in {
        "id",
        "is_deleted",
        "created_by",
        "create_dept",
        "created_at",
        "updated_by",
        "updated_at",
    }:
        assert required in names, f"users missing {required}"
    assert not cols  # abstract base has no table of its own


def test_log_entity_is_append_only_shape() -> None:
    """usage_record (LogEntity) has id + created_at, no soft-delete/audit cols."""
    usage = Base.metadata.tables["usage_record"]
    names = set(usage.columns.keys())
    assert {"id", "created_at"} <= names
    assert "is_deleted" not in names
    assert "updated_at" not in names
    assert "created_by" not in names
    assert "create_dept" not in names


def test_link_entity_is_physical_delete_shape() -> None:
    """user_role (LinkEntity) has id + created_at, no soft-delete/audit cols."""
    link = Base.metadata.tables["user_role"]
    names = set(link.columns.keys())
    assert {"id", "created_at", "user_id", "role_id"} <= names
    assert "is_deleted" not in names
    assert "updated_at" not in names
    assert "created_by" not in names
    assert "create_dept" not in names


def test_entity_base_classes_are_abstract() -> None:
    for base in (BaseEntity, LogEntity, LinkEntity):
        assert getattr(base, "__abstract__", False) is True


def test_timestamp_columns_are_timezone_aware() -> None:
    """created_at must be timestamptz (timezone=True), per §0.3."""
    user = Base.metadata.tables["users"]
    created_at = user.columns["created_at"]
    assert getattr(created_at.type, "timezone", False) is True
