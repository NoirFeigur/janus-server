"""Platform config subpackage (admin resource).

CRUD over the ``config`` key-value table, exposed under ``/admin/config`` and
gated by ``system:config:*`` permissions. Writes invalidate the short-TTL Redis
cache (see :mod:`src.core.config_accessor`) so runtime readers pick up changes.
"""

from src.admin.config.router import router

__all__ = ["router"]
