"""Admin catalog business logic (service layer)."""

from __future__ import annotations

from contextlib import suppress

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette import status

from src.admin.catalog.repository import (
    ChannelKeyRepository,
    LogicalModelRepository,
    ModelDeploymentRepository,
    UpstreamChannelRepository,
)
from src.admin.catalog.schemas import (
    ChannelKeyCreate,
    ChannelKeyUpdate,
    LogicalModelCreate,
    LogicalModelUpdate,
    ModelDeploymentCreate,
    ModelDeploymentUpdate,
    UpstreamChannelCreate,
    UpstreamChannelUpdate,
)
from src.auth.service import AuthenticatedUser
from src.core.channel_crypto import encrypt_channel_key, key_hint
from src.core.pagination import PageResult, page_result
from src.core.query import ListQuery, resolve_sort
from src.core.redis import get_redis
from src.db.models.model_catalog import (
    ChannelKey,
    LogicalModel,
    ModelDeployment,
    UpstreamChannel,
)
from src.db.session import add_after_commit_hook
from src.enums import ErrorCode
from src.exceptions import AppError

CHANNEL_SORT_COLUMNS = {
    "id": UpstreamChannel.id,
    "name": UpstreamChannel.name,
    "provider": UpstreamChannel.provider,
    "protocol": UpstreamChannel.protocol,
    "created_at": UpstreamChannel.created_at,
    "updated_at": UpstreamChannel.updated_at,
}

KEY_SORT_COLUMNS = {
    "id": ChannelKey.id,
    "alias": ChannelKey.alias,
    "status": ChannelKey.status,
    "weight": ChannelKey.weight,
    "priority": ChannelKey.priority,
    "created_at": ChannelKey.created_at,
    "updated_at": ChannelKey.updated_at,
}

MODEL_SORT_COLUMNS = {
    "id": LogicalModel.id,
    "name": LogicalModel.name,
    "display_name": LogicalModel.display_name,
    "sort_order": LogicalModel.sort_order,
    "created_at": LogicalModel.created_at,
    "updated_at": LogicalModel.updated_at,
}

DEPLOYMENT_SORT_COLUMNS = {
    "id": ModelDeployment.id,
    "logical_model_id": ModelDeployment.logical_model_id,
    "channel_id": ModelDeployment.channel_id,
    "upstream_model": ModelDeployment.upstream_model,
    "weight": ModelDeployment.weight,
    "priority": ModelDeployment.priority,
    "created_at": ModelDeployment.created_at,
    "updated_at": ModelDeployment.updated_at,
}


class CatalogService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.channels = UpstreamChannelRepository(session)
        self.keys = ChannelKeyRepository(session)
        self.models = LogicalModelRepository(session)
        self.deployments = ModelDeploymentRepository(session)

    async def _require_channel(self, channel_id: int) -> UpstreamChannel:
        channel = await self.channels.get(channel_id)
        if channel is None:
            raise AppError(ErrorCode.request_invalid, status.HTTP_404_NOT_FOUND)
        return channel

    async def _require_active_channel(self, channel_id: int) -> UpstreamChannel:
        channel = await self._require_channel(channel_id)
        if channel.status != "active":
            raise AppError(ErrorCode.request_invalid, status.HTTP_400_BAD_REQUEST)
        return channel

    async def _require_key(self, key_id: int) -> ChannelKey:
        key = await self.keys.get(key_id)
        if key is None:
            raise AppError(ErrorCode.request_invalid, status.HTTP_404_NOT_FOUND)
        return key

    async def _require_model(self, model_id: int) -> LogicalModel:
        model = await self.models.get(model_id)
        if model is None:
            raise AppError(ErrorCode.request_invalid, status.HTTP_404_NOT_FOUND)
        return model

    async def _require_active_model(self, model_id: int) -> LogicalModel:
        model = await self._require_model(model_id)
        if model.status != "active":
            raise AppError(ErrorCode.request_invalid, status.HTTP_400_BAD_REQUEST)
        return model

    async def _require_deployment(self, deployment_id: int) -> ModelDeployment:
        deployment = await self.deployments.get(deployment_id)
        if deployment is None:
            raise AppError(ErrorCode.request_invalid, status.HTTP_404_NOT_FOUND)
        return deployment

    async def _ensure_channel_name_unique(
        self, name: str, *, current_id: int | None = None
    ) -> None:
        channel = await self.channels.get_by_name(name)
        if channel is not None and channel.id != current_id:
            raise AppError(ErrorCode.request_invalid, status.HTTP_400_BAD_REQUEST)

    async def _ensure_model_name_unique(
        self, name: str, *, current_id: int | None = None
    ) -> None:
        model = await self.models.get_by_name(name)
        if model is not None and model.id != current_id:
            raise AppError(ErrorCode.request_invalid, status.HTTP_400_BAD_REQUEST)

    async def _ensure_deployment_unique(
        self,
        *,
        logical_model_id: int,
        channel_id: int,
        current_id: int | None = None,
    ) -> None:
        deployment = await self.deployments.get_by_logical_model_channel(
            logical_model_id=logical_model_id,
            channel_id=channel_id,
        )
        if deployment is not None and deployment.id != current_id:
            raise AppError(ErrorCode.request_invalid, status.HTTP_400_BAD_REQUEST)

    async def list_channels(
        self, *, query: ListQuery | None = None
    ) -> PageResult[UpstreamChannel]:
        query = query or ListQuery()
        sort = resolve_sort(query, allowed=CHANNEL_SORT_COLUMNS, default="name")
        total = await self.channels.count_channels(keyword=query.keyword)
        items = await self.channels.list_channels(
            keyword=query.keyword,
            sort=sort,
            limit=query.limit,
            offset=query.offset,
        )
        return page_result(
            list(items), total=total, limit=query.limit, offset=query.offset
        )

    async def get_channel(self, channel_id: int) -> UpstreamChannel:
        return await self._require_channel(channel_id)

    async def create_channel(
        self, payload: UpstreamChannelCreate, *, actor: AuthenticatedUser
    ) -> UpstreamChannel:
        await self._ensure_channel_name_unique(payload.name)
        channel = UpstreamChannel(
            name=payload.name,
            provider=payload.provider,
            protocol=payload.protocol,
            api_base=payload.api_base,
            extra_config=payload.extra_config,
            status=payload.status,
            remark=payload.remark,
            created_by=actor.user_id,
            create_dept=actor.department_id,
            updated_by=actor.user_id,
        )
        await self.channels.create(channel)
        add_after_commit_hook(self.session, _publish_router_invalidation)
        return channel

    async def update_channel(
        self,
        channel_id: int,
        payload: UpstreamChannelUpdate,
        *,
        actor: AuthenticatedUser,
    ) -> UpstreamChannel:
        channel = await self._require_channel(channel_id)
        values = payload.model_dump(exclude_unset=True)
        name = values.get("name")
        if name is not None:
            await self._ensure_channel_name_unique(name, current_id=channel.id)
        values["updated_by"] = actor.user_id
        await self.channels.update(channel, **values)
        await self.session.refresh(channel)
        add_after_commit_hook(self.session, _publish_router_invalidation)
        return channel

    async def delete_channel(
        self, channel_id: int, *, actor: AuthenticatedUser
    ) -> None:
        channel = await self._require_channel(channel_id)
        active_deployment_count = await self.session.scalar(
            select(func.count()).select_from(ModelDeployment).where(
                ModelDeployment.channel_id == channel_id,
                ModelDeployment.is_deleted.is_(False),
                ModelDeployment.status == "active",
            )
        )
        if active_deployment_count and active_deployment_count > 0:
            raise AppError(ErrorCode.request_conflict, status.HTTP_409_CONFLICT)

        channel.updated_by = actor.user_id
        await self.channels.soft_delete(channel)
        add_after_commit_hook(self.session, _publish_router_invalidation)

    async def list_keys(
        self,
        *,
        channel_id: int | None = None,
        query: ListQuery | None = None,
    ) -> PageResult[ChannelKey]:
        query = query or ListQuery()
        sort = resolve_sort(query, allowed=KEY_SORT_COLUMNS, default="priority")
        total = await self.keys.count_keys(
            channel_id=channel_id, keyword=query.keyword
        )
        items = await self.keys.list_keys(
            channel_id=channel_id,
            keyword=query.keyword,
            sort=sort,
            limit=query.limit,
            offset=query.offset,
        )
        return page_result(
            list(items), total=total, limit=query.limit, offset=query.offset
        )

    async def get_key(self, key_id: int) -> ChannelKey:
        return await self._require_key(key_id)

    async def create_key(
        self, payload: ChannelKeyCreate, *, actor: AuthenticatedUser
    ) -> ChannelKey:
        await self._require_channel(payload.channel_id)
        key = ChannelKey(
            channel_id=payload.channel_id,
            alias=payload.alias,
            api_key_encrypted=encrypt_channel_key(payload.api_key),
            key_hint=key_hint(payload.api_key),
            status=payload.status,
            rpm_limit=payload.rpm_limit,
            tpm_limit=payload.tpm_limit,
            weight=payload.weight,
            priority=payload.priority,
            remark=payload.remark,
            created_by=actor.user_id,
            create_dept=actor.department_id,
            updated_by=actor.user_id,
        )
        await self.keys.create(key)
        add_after_commit_hook(self.session, _publish_router_invalidation)
        return key

    async def update_key(
        self, key_id: int, payload: ChannelKeyUpdate, *, actor: AuthenticatedUser
    ) -> ChannelKey:
        key = await self._require_key(key_id)
        values = payload.model_dump(exclude_unset=True)
        channel_id = values.get("channel_id")
        if channel_id is not None:
            await self._require_channel(channel_id)
        values["updated_by"] = actor.user_id
        await self.keys.update(key, **values)
        await self.session.refresh(key)
        add_after_commit_hook(self.session, _publish_router_invalidation)
        return key

    async def delete_key(self, key_id: int, *, actor: AuthenticatedUser) -> None:
        key = await self._require_key(key_id)
        key.updated_by = actor.user_id
        await self.keys.soft_delete(key)
        add_after_commit_hook(self.session, _publish_router_invalidation)

    async def list_models(
        self, *, query: ListQuery | None = None
    ) -> PageResult[LogicalModel]:
        query = query or ListQuery()
        sort = resolve_sort(query, allowed=MODEL_SORT_COLUMNS, default="sort_order")
        total = await self.models.count_models(keyword=query.keyword)
        items = await self.models.list_models(
            keyword=query.keyword,
            sort=sort,
            limit=query.limit,
            offset=query.offset,
        )
        return page_result(
            list(items), total=total, limit=query.limit, offset=query.offset
        )

    async def get_model(self, model_id: int) -> LogicalModel:
        return await self._require_model(model_id)

    async def create_model(
        self, payload: LogicalModelCreate, *, actor: AuthenticatedUser
    ) -> LogicalModel:
        await self._ensure_model_name_unique(payload.name)
        model = LogicalModel(
            name=payload.name,
            display_name=payload.display_name,
            category=payload.category,
            sort_order=payload.sort_order,
            context_length=payload.context_length,
            price_input=payload.price_input,
            price_output=payload.price_output,
            status=payload.status,
            remark=payload.remark,
            created_by=actor.user_id,
            create_dept=actor.department_id,
            updated_by=actor.user_id,
        )
        await self.models.create(model)
        add_after_commit_hook(self.session, _publish_router_invalidation)
        return model

    async def update_model(
        self,
        model_id: int,
        payload: LogicalModelUpdate,
        *,
        actor: AuthenticatedUser,
    ) -> LogicalModel:
        model = await self._require_model(model_id)
        values = payload.model_dump(exclude_unset=True)
        name = values.get("name")
        if name is not None:
            await self._ensure_model_name_unique(name, current_id=model.id)
        values["updated_by"] = actor.user_id
        await self.models.update(model, **values)
        await self.session.refresh(model)
        add_after_commit_hook(self.session, _publish_router_invalidation)
        return model

    async def delete_model(self, model_id: int, *, actor: AuthenticatedUser) -> None:
        model = await self._require_model(model_id)
        active_deployment_count = await self.session.scalar(
            select(func.count()).select_from(ModelDeployment).where(
                ModelDeployment.logical_model_id == model_id,
                ModelDeployment.is_deleted.is_(False),
                ModelDeployment.status == "active",
            )
        )
        if active_deployment_count and active_deployment_count > 0:
            raise AppError(ErrorCode.request_conflict, status.HTTP_409_CONFLICT)

        model.updated_by = actor.user_id
        await self.models.soft_delete(model)
        add_after_commit_hook(self.session, _publish_router_invalidation)

    async def list_deployments(
        self,
        *,
        logical_model_id: int | None = None,
        query: ListQuery | None = None,
    ) -> PageResult[ModelDeployment]:
        query = query or ListQuery()
        sort = resolve_sort(
            query, allowed=DEPLOYMENT_SORT_COLUMNS, default="priority"
        )
        total = await self.deployments.count_deployments(
            logical_model_id=logical_model_id, keyword=query.keyword
        )
        items = await self.deployments.list_deployments(
            logical_model_id=logical_model_id,
            keyword=query.keyword,
            sort=sort,
            limit=query.limit,
            offset=query.offset,
        )
        return page_result(
            list(items), total=total, limit=query.limit, offset=query.offset
        )

    async def get_deployment(self, deployment_id: int) -> ModelDeployment:
        return await self._require_deployment(deployment_id)

    async def create_deployment(
        self, payload: ModelDeploymentCreate, *, actor: AuthenticatedUser
    ) -> ModelDeployment:
        await self._require_active_model(payload.logical_model_id)
        await self._require_active_channel(payload.channel_id)
        await self._ensure_deployment_unique(
            logical_model_id=payload.logical_model_id,
            channel_id=payload.channel_id,
        )
        deployment = ModelDeployment(
            logical_model_id=payload.logical_model_id,
            channel_id=payload.channel_id,
            upstream_model=payload.upstream_model,
            weight=payload.weight,
            priority=payload.priority,
            status=payload.status,
            remark=payload.remark,
            created_by=actor.user_id,
            create_dept=actor.department_id,
            updated_by=actor.user_id,
        )
        await self.deployments.create(deployment)
        add_after_commit_hook(self.session, _publish_router_invalidation)
        return deployment

    async def update_deployment(
        self,
        deployment_id: int,
        payload: ModelDeploymentUpdate,
        *,
        actor: AuthenticatedUser,
    ) -> ModelDeployment:
        deployment = await self._require_deployment(deployment_id)
        values = payload.model_dump(exclude_unset=True)
        logical_model_id = values.get("logical_model_id", deployment.logical_model_id)
        channel_id = values.get("channel_id", deployment.channel_id)
        if "logical_model_id" in values:
            await self._require_active_model(logical_model_id)
        if "channel_id" in values:
            await self._require_active_channel(channel_id)
        if "logical_model_id" in values or "channel_id" in values:
            await self._ensure_deployment_unique(
                logical_model_id=logical_model_id,
                channel_id=channel_id,
                current_id=deployment.id,
            )
        values["updated_by"] = actor.user_id
        await self.deployments.update(deployment, **values)
        await self.session.refresh(deployment)
        add_after_commit_hook(self.session, _publish_router_invalidation)
        return deployment

    async def delete_deployment(
        self, deployment_id: int, *, actor: AuthenticatedUser
    ) -> None:
        deployment = await self._require_deployment(deployment_id)
        deployment.updated_by = actor.user_id
        await self.deployments.soft_delete(deployment)
        add_after_commit_hook(self.session, _publish_router_invalidation)


def _publish_router_invalidation() -> None:
    """Publish router invalidation event (best-effort, non-blocking)."""
    import asyncio

    async def _pub() -> None:
        with suppress(Exception):
            await get_redis().publish("gateway:router:invalidate", "1")

    with suppress(RuntimeError):
        asyncio.get_event_loop().create_task(_pub())
