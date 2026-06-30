"""Admin management-plane router aggregator.

Mounts the domain sub-routers under ``/admin``. Each sub-router self-gates its
endpoints with ``RequiredPerms``.
"""

from fastapi import APIRouter

from src.admin.audit.router import router as audit_router
from src.admin.catalog.router import router as catalog_router
from src.admin.config.router import router as config_router
from src.admin.credentials.router import router as credentials_router
from src.admin.departments.router import router as departments_router
from src.admin.grants.router import router as grants_router
from src.admin.menus.router import router as menus_router
from src.admin.meta.router import router as meta_router
from src.admin.observability.router import router as observability_router
from src.admin.online.router import router as online_router
from src.admin.quota.router import router as quota_router
from src.admin.rate_limits.router import router as rate_limits_router
from src.admin.roles.router import router as roles_router
from src.admin.usage.router import router as usage_router
from src.admin.users.router import router as users_router

router = APIRouter(prefix="/admin", tags=["admin"])

router.include_router(audit_router)
router.include_router(catalog_router)
router.include_router(config_router)
router.include_router(credentials_router)
router.include_router(departments_router)
router.include_router(grants_router)
router.include_router(menus_router)
router.include_router(meta_router)
router.include_router(observability_router)
router.include_router(online_router)
router.include_router(quota_router)
router.include_router(rate_limits_router)
router.include_router(roles_router)
router.include_router(usage_router)
router.include_router(users_router)
