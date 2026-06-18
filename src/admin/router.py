"""Admin management-plane router aggregator.

Mounts the domain sub-routers (audit / config / departments / roles / users /
menus / online) under ``/admin``. Each sub-router self-gates its endpoints with
``RequiredPerms``.
"""

from fastapi import APIRouter

from src.admin.audit.router import router as audit_router
from src.admin.config.router import router as config_router
from src.admin.departments.router import router as departments_router
from src.admin.menus.router import router as menus_router
from src.admin.online.router import router as online_router
from src.admin.roles.router import router as roles_router
from src.admin.users.router import router as users_router

router = APIRouter(prefix="/admin", tags=["admin"])

router.include_router(audit_router)
router.include_router(config_router)
router.include_router(departments_router)
router.include_router(menus_router)
router.include_router(online_router)
router.include_router(roles_router)
router.include_router(users_router)
