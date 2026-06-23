"""SQLAlchemy ORM models, centralized (README: ORM models live in db/models/).

This module re-exports every mapped class so Alembic autogenerate sees the
full metadata by importing one place. Import side effects register each model
on ``Base.metadata``.
"""

from src.db.models.attach import SysAttach
from src.db.models.audit import LoginLog, OperLog
from src.db.models.catalog_ops import CatalogChangeLog, CatalogConfigSnapshot
from src.db.models.credential import ApiKey
from src.db.models.gateway_observability import GatewayRequestLog
from src.db.models.grant import UserModelGrant
from src.db.models.identity import (
    Department,
    Menu,
    Role,
    RoleDept,
    RoleMenu,
    User,
    UserOAuth,
    UserRole,
)
from src.db.models.model_catalog import (
    ChannelKey,
    LogicalModel,
    ModelDeployment,
    UpstreamChannel,
)
from src.db.models.quota import Quota
from src.db.models.rate_limit import RateLimitRule
from src.db.models.sys_config import SysConfig
from src.db.models.usage import UsageRecord

__all__ = [
    # identity & RBAC
    "User",
    "Department",
    "UserOAuth",
    "Role",
    "Menu",
    "UserRole",
    "RoleMenu",
    "RoleDept",
    # credential
    "ApiKey",
    # model catalog
    "UpstreamChannel",
    "ChannelKey",
    "LogicalModel",
    "ModelDeployment",
    # grant
    "UserModelGrant",
    # usage & quota
    "UsageRecord",
    "Quota",
    # rate limiting
    "RateLimitRule",
    # audit
    "OperLog",
    "LoginLog",
    # gateway observability
    "GatewayRequestLog",
    # catalog operations
    "CatalogChangeLog",
    "CatalogConfigSnapshot",
    # platform config
    "SysConfig",
    # attachment
    "SysAttach",
]
