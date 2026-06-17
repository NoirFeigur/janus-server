"""Admin management-plane router aggregator.

Mounts the domain sub-routers (departments / roles / users) under ``/admin``.
Each sub-router self-gates its endpoints with ``RequiredPerms``.
"""

from fastapi import APIRouter

from src.admin.departments.router import router as departments_router
from src.admin.roles.router import router as roles_router
from src.admin.users.router import router as users_router

router = APIRouter(prefix="/admin", tags=["admin"])

router.include_router(departments_router)
router.include_router(roles_router)
router.include_router(users_router)
