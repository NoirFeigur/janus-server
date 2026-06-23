"""Cache invalidation helpers for admin write paths.

After catalog/grant/quota writes commit, these helpers bump the appropriate
generation counters so all prior cache entries become stale.  Combined with
the existing ``_publish_router_invalidation()`` pub/sub for cross-replica
Router rebuilds.
"""

from __future__ import annotations

from src.core.logging import get_logger
from src.gateway.cache import (
    bump_catalog_generation,
    bump_grant_generation,
    bump_quota_generation,
)

_log = get_logger(__name__)


async def invalidate_catalog_cache() -> None:
    """Bump catalog generation after channel/model/deployment/key writes.

    Should be registered as an after-commit hook alongside the existing
    ``_publish_router_invalidation``.
    """
    await bump_catalog_generation()


async def invalidate_grant_cache(
    *, user_id: int | None = None, dept_id: int | None = None
) -> None:
    """Bump grant generation after user_model_grant create/update/delete."""
    await bump_grant_generation(user_id=user_id, dept_id=dept_id)


async def invalidate_quota_cache(
    *,
    user_id: int | None = None,
    dept_id: int | None = None,
    is_global: bool = False,
) -> None:
    """Bump quota generation after quota rule create/update/delete."""
    await bump_quota_generation(user_id=user_id, dept_id=dept_id, is_global=is_global)
