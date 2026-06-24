"""Route-level test for the catalog change-log listing (M3-9).

Drives the real app through ``admin_ctx``. The endpoint paginates audit entries;
the invariant under test is that ``total`` reflects EVERY matching row, not just
the page returned under ``limit`` — the count is a DB-side aggregate, decoupled
from the page slice. Also covers that the ``resource_type`` filter narrows both
the page and the total consistently.
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models.catalog_ops import CatalogChangeLog
from tests.admin.conftest import AdminCtx

pytestmark = pytest.mark.asyncio


async def _seed_changelog(
    session: AsyncSession, *, resource_type: str, count: int
) -> None:
    session.add_all(
        CatalogChangeLog(
            actor_id=1,
            resource_type=resource_type,
            resource_id=str(i),
            action="update",
        )
        for i in range(count)
    )
    await session.commit()


async def test_changelog_total_counts_all_matching_rows_not_just_page(
    admin_ctx: AdminCtx,
) -> None:
    """M3-9: total must reflect every matching row even when limit slices the page."""
    await _seed_changelog(admin_ctx.session, resource_type="channel", count=5)

    resp = await admin_ctx.client.get("/admin/catalog/changelog?limit=2&offset=0")

    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert len(data["items"]) == 2  # page is sliced by limit
    assert data["total"] == 5  # but total reflects ALL matching rows


async def test_changelog_filter_narrows_page_and_total(
    admin_ctx: AdminCtx,
) -> None:
    """M3-9: a resource_type filter narrows both the page and the total count."""
    await _seed_changelog(admin_ctx.session, resource_type="channel", count=3)
    await _seed_changelog(admin_ctx.session, resource_type="model", count=2)

    resp = await admin_ctx.client.get(
        "/admin/catalog/changelog?resource_type=model&limit=50"
    )

    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["total"] == 2
    assert {item["resource_type"] for item in data["items"]} == {"model"}
