from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import and_, case, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models.grant import UserModelGrant
from src.db.models.model_catalog import (
    ChannelKey,
    LogicalModel,
    ModelDeployment,
    UpstreamChannel,
)
from src.db.models.quota import Quota
from src.enums import ActiveStatus, ChannelKeyStatus, ChannelStatus, GrantScope, QuotaScope


@dataclass(frozen=True, slots=True)
class RouterDeploymentRow:
    logical_model_name: str
    logical_model_id: int
    upstream_model: str
    provider: str
    protocol: str
    api_base: str | None
    extra_config: dict[str, Any] | None
    api_key_encrypted: str
    channel_id: int
    channel_key_id: int
    deployment_weight: int
    deployment_priority: int
    key_weight: int
    key_rpm_limit: int | None
    key_tpm_limit: int | None


class GatewayRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_router_config(self) -> list[RouterDeploymentRow]:
        """Return the active logical-model/deployment/channel/key expansion."""
        stmt = (
            select(
                LogicalModel.name,
                LogicalModel.id,
                ModelDeployment.upstream_model,
                UpstreamChannel.provider,
                UpstreamChannel.protocol,
                UpstreamChannel.api_base,
                UpstreamChannel.extra_config,
                ChannelKey.api_key_encrypted,
                UpstreamChannel.id,
                ChannelKey.id,
                ModelDeployment.weight,
                ModelDeployment.priority,
                ChannelKey.weight,
                ChannelKey.rpm_limit,
                ChannelKey.tpm_limit,
            )
            .select_from(LogicalModel)
            .join(ModelDeployment, ModelDeployment.logical_model_id == LogicalModel.id)
            .join(UpstreamChannel, UpstreamChannel.id == ModelDeployment.channel_id)
            .join(ChannelKey, ChannelKey.channel_id == UpstreamChannel.id)
            .where(
                LogicalModel.is_deleted.is_(False),
                ModelDeployment.is_deleted.is_(False),
                UpstreamChannel.is_deleted.is_(False),
                ChannelKey.is_deleted.is_(False),
                LogicalModel.status == ActiveStatus.active.value,
                ModelDeployment.status == ActiveStatus.active.value,
                UpstreamChannel.status == ChannelStatus.active.value,
                ChannelKey.status == ChannelKeyStatus.active.value,
            )
            .order_by(
                LogicalModel.name,
                ModelDeployment.priority,
                ChannelKey.priority,
                UpstreamChannel.id,
                ChannelKey.id,
            )
        )
        rows = (await self.session.execute(stmt)).all()
        return [
            RouterDeploymentRow(
                logical_model_name=row[0],
                logical_model_id=row[1],
                upstream_model=row[2],
                provider=row[3],
                protocol=row[4],
                api_base=row[5],
                extra_config=row[6],
                api_key_encrypted=row[7],
                channel_id=row[8],
                channel_key_id=row[9],
                deployment_weight=row[10],
                deployment_priority=row[11],
                key_weight=row[12],
                key_rpm_limit=row[13],
                key_tpm_limit=row[14],
            )
            for row in rows
        ]

    async def get_user_granted_models(self, user_id: int, dept_id: int | None) -> set[int]:
        predicates = [
            and_(
                UserModelGrant.scope == GrantScope.user.value,
                UserModelGrant.scope_id == user_id,
            )
        ]
        if dept_id is not None:
            predicates.append(
                and_(
                    UserModelGrant.scope == GrantScope.department.value,
                    UserModelGrant.scope_id == dept_id,
                )
            )
        stmt = select(UserModelGrant.logical_model_id).where(
            UserModelGrant.is_deleted.is_(False), or_(*predicates)
        )
        return set((await self.session.scalars(stmt)).all())

    async def get_default_model_id(self, user_id: int, dept_id: int | None) -> int | None:
        predicates = [
            and_(
                UserModelGrant.scope == GrantScope.user.value,
                UserModelGrant.scope_id == user_id,
            )
        ]
        if dept_id is not None:
            predicates.append(
                and_(
                    UserModelGrant.scope == GrantScope.department.value,
                    UserModelGrant.scope_id == dept_id,
                )
            )
        scope_rank = case((UserModelGrant.scope == GrantScope.user.value, 0), else_=1)
        stmt = (
            select(UserModelGrant.logical_model_id)
            .where(
                UserModelGrant.is_deleted.is_(False),
                UserModelGrant.is_default.is_(True),
                or_(*predicates),
            )
            .order_by(scope_rank)
            .limit(1)
        )
        result: int | None = await self.session.scalar(stmt)
        return result

    async def get_logical_model_by_name(self, name: str) -> LogicalModel | None:
        stmt = select(LogicalModel).where(
            LogicalModel.is_deleted.is_(False),
            LogicalModel.status == ActiveStatus.active.value,
            LogicalModel.name == name,
        )
        result: LogicalModel | None = await self.session.scalar(stmt)
        return result

    async def get_logical_model_by_id(self, logical_model_id: int) -> LogicalModel | None:
        stmt = select(LogicalModel).where(
            LogicalModel.is_deleted.is_(False),
            LogicalModel.status == ActiveStatus.active.value,
            LogicalModel.id == logical_model_id,
        )
        result: LogicalModel | None = await self.session.scalar(stmt)
        return result

    async def get_logical_models_by_ids(
        self, logical_model_ids: Sequence[int]
    ) -> list[LogicalModel]:
        if not logical_model_ids:
            return []
        stmt = (
            select(LogicalModel)
            .where(
                LogicalModel.is_deleted.is_(False),
                LogicalModel.status == ActiveStatus.active.value,
                LogicalModel.id.in_(logical_model_ids),
            )
            .order_by(LogicalModel.name)
        )
        return list((await self.session.scalars(stmt)).all())

    async def get_active_quotas(
        self,
        user_id: int,
        department_id: int | None,
        logical_model_id: int,
    ) -> list[Quota]:
        predicates = [
            and_(Quota.scope == QuotaScope.user.value, Quota.scope_id == user_id),
            and_(Quota.scope == QuotaScope.global_.value, Quota.scope_id.is_(None)),
        ]
        if department_id is not None:
            predicates.append(
                and_(
                    Quota.scope == QuotaScope.department.value,
                    Quota.scope_id == department_id,
                )
            )
        stmt = select(Quota).where(
            Quota.is_deleted.is_(False),
            Quota.status == ActiveStatus.active.value,
            or_(*predicates),
            or_(Quota.logical_model_id == logical_model_id, Quota.logical_model_id.is_(None)),
        )
        return list((await self.session.scalars(stmt)).all())
