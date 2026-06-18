"""Platform configuration aggregate (Wave D, table sys_config).

RuoYi-style key-value platform configuration. Each row holds one runtime
parameter as a typed string: ``value_type`` tells the typed accessor how to
parse ``config_value`` (``string``/``int``/``bool``/``json``). Builtin rows
(``is_builtin = true``) ship with the platform and may be updated but never
deleted.

Reads on the hot path go through a short-TTL Redis cache (see
``src/core/config_accessor.py``); writes invalidate that cache so other
replicas pick up the new value.
"""

from __future__ import annotations

from sqlalchemy import Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from src.db.base import BaseEntity
from src.enums import ConfigValueType


class SysConfig(BaseEntity):
    """Platform runtime configuration as typed key-value rows (Wave D)."""

    __tablename__ = "sys_config"
    __table_args__ = (
        Index(
            "uq_sysconfig_key", "config_key", unique=True
        ),  # Global key uniqueness (no is_deleted filter: one row per logical key).
        {"comment": "平台配置：键值型运行时参数（typed），内置项可改不可删"},
    )

    config_key: Mapped[str] = mapped_column(
        String(128),
        comment="配置键（全局唯一），如 auth.login_max_failures",
    )
    config_value: Mapped[str] = mapped_column(
        Text, comment="配置值，统一以字符串存储；按 value_type 解析"
    )
    value_type: Mapped[str] = mapped_column(
        String(16),
        default=ConfigValueType.string,
        comment="值类型 ConfigValueType：string | int | bool | json",
    )
    config_name: Mapped[str] = mapped_column(
        String(128), comment="人类可读名称（管理后台展示）"
    )
    is_builtin: Mapped[bool] = mapped_column(
        default=False,
        comment="是否平台内置项：true=可改不可删",
    )

    remark: Mapped[str | None] = mapped_column(
        String(255), nullable=True, comment="备注"
    )
