"""Admin catalog business logic (service layer)."""

from __future__ import annotations

import ipaddress
from contextlib import suppress
from urllib.parse import urlsplit

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
from src.auth.service import AuthenticatedUser, AuthService, DataScopeFilter
from src.config import get_settings
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
    """目录服务：上游渠道 / 号池 key / 逻辑模型 / 部署的 CRUD。

    访问模型（有意的读写不对称，非缺陷）：

    - **catalog 是平台基础设施，不是 user-owned 数据**。channel/key/model/
      deployment 是全公司共享的上游厂商连接与逻辑模型定义，与 usage / credential
      等带强制 ``user_id`` 的用户数据表性质不同。``created_by`` / ``create_dept``
      仅为 ``BaseEntity`` 继承来的审计列，不构成 catalog 的数据权限边界。
    - **读取（list/get）按权限门控，不做 data-scope 过滤**：端点要求
      ``ai:catalog:list`` / ``ai:catalog:query``，持有者即平台管理员，需要看到
      全部渠道才能配置部署（restricted-scope admin 配 deployment 时必须能引用
      他人创建的 channel）。secret 在 ``ChannelKeyRead`` 已脱敏（仅暴露
      ``key_hint``），读取无敏感泄露面。
    - **写入（create/update/delete）才施加 scope**：create 要求 unrestricted
      scope；update/delete 要求 catalog wildcard + (unrestricted 或本人创建)。
      因 restricted-scope admin 无法 create，正常不会 own 任何 catalog 记录，
      ownership 分支仅覆盖「创建后被降权」的边缘场景。

    若未来 catalog 需按部门隔离（如多租户上游隔离），应在 list/get 接入
    ``DataScopeFilter`` 并同步调整部署配置流程，避免破坏跨部门引用。
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.channels = UpstreamChannelRepository(session)
        self.keys = ChannelKeyRepository(session)
        self.models = LogicalModelRepository(session)
        self.deployments = ModelDeploymentRepository(session)
        self.auth = AuthService(session)

    async def _scope(self, actor: AuthenticatedUser) -> DataScopeFilter:
        return await self.auth.resolve_data_scope(actor)

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

    def _require_catalog_wildcard(self, actor: AuthenticatedUser) -> None:
        if not (actor.is_superuser or actor.has_permission("ai:catalog:*")):
            raise AppError(ErrorCode.auth_forbidden, status.HTTP_403_FORBIDDEN)

    async def _check_catalog_write_access(
        self, actor: AuthenticatedUser, resource: ChannelKey | UpstreamChannel
    ) -> None:
        self._require_catalog_wildcard(actor)
        scope = await self._scope(actor)
        if scope.unrestricted or resource.created_by == actor.user_id:
            return
        raise AppError(ErrorCode.auth_forbidden, status.HTTP_403_FORBIDDEN)

    async def _require_catalog_create_access(self, actor: AuthenticatedUser) -> None:
        self._require_catalog_wildcard(actor)
        scope = await self._scope(actor)
        if not scope.unrestricted:
            raise AppError(ErrorCode.auth_forbidden, status.HTTP_403_FORBIDDEN)

    def _validate_api_base(self, api_base: str | None) -> None:
        if api_base is None:
            return
        parsed = urlsplit(api_base)
        if parsed.scheme == "https" and parsed.hostname:
            return
        if parsed.scheme == "http" and parsed.hostname:
            settings = get_settings()
            if settings.environment not in {"prod", "production"} and _is_internal_host(
                parsed.hostname
            ):
                return
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
        await self._require_catalog_create_access(actor)
        self._validate_api_base(payload.api_base)
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
        add_after_commit_hook(self.session, _bump_catalog_cache_generation)
        return channel

    async def update_channel(
        self,
        channel_id: int,
        payload: UpstreamChannelUpdate,
        *,
        actor: AuthenticatedUser,
    ) -> UpstreamChannel:
        channel = await self._require_channel(channel_id)
        await self._check_catalog_write_access(actor, channel)
        values = payload.model_dump(exclude_unset=True)
        if "api_base" in values:
            self._validate_api_base(values["api_base"])
        name = values.get("name")
        if name is not None:
            await self._ensure_channel_name_unique(name, current_id=channel.id)
        values["updated_by"] = actor.user_id
        await self.channels.update(channel, **values)
        await self.session.refresh(channel)
        add_after_commit_hook(self.session, _publish_router_invalidation)
        add_after_commit_hook(self.session, _bump_catalog_cache_generation)
        return channel

    async def delete_channel(
        self, channel_id: int, *, actor: AuthenticatedUser
    ) -> None:
        channel = await self._require_channel(channel_id)
        await self._check_catalog_write_access(actor, channel)
        if await self.channels.count_active_deployments(channel_id) > 0:
            raise AppError(ErrorCode.request_conflict, status.HTTP_409_CONFLICT)

        channel.updated_by = actor.user_id
        await self.channels.soft_delete(channel)
        add_after_commit_hook(self.session, _publish_router_invalidation)
        add_after_commit_hook(self.session, _bump_catalog_cache_generation)

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
        self._require_catalog_wildcard(actor)
        channel = await self._require_channel(payload.channel_id)
        await self._check_catalog_write_access(actor, channel)
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
        add_after_commit_hook(self.session, _bump_catalog_cache_generation)
        return key

    async def update_key(
        self, key_id: int, payload: ChannelKeyUpdate, *, actor: AuthenticatedUser
    ) -> ChannelKey:
        key = await self._require_key(key_id)
        await self._check_catalog_write_access(actor, key)
        values = payload.model_dump(exclude_unset=True)
        channel_id = values.get("channel_id")
        if channel_id is not None:
            channel = await self._require_channel(channel_id)
            await self._check_catalog_write_access(actor, channel)
        values["updated_by"] = actor.user_id
        await self.keys.update(key, **values)
        await self.session.refresh(key)
        add_after_commit_hook(self.session, _publish_router_invalidation)
        add_after_commit_hook(self.session, _bump_catalog_cache_generation)
        return key

    async def delete_key(self, key_id: int, *, actor: AuthenticatedUser) -> None:
        key = await self._require_key(key_id)
        await self._check_catalog_write_access(actor, key)
        key.updated_by = actor.user_id
        await self.keys.soft_delete(key)
        add_after_commit_hook(self.session, _publish_router_invalidation)
        add_after_commit_hook(self.session, _bump_catalog_cache_generation)

    async def rotate_key(
        self, key_id: int, new_api_key: str, *, actor: AuthenticatedUser
    ) -> ChannelKey:
        """Replace the encrypted upstream key material for a channel key."""
        key = await self._require_key(key_id)
        await self._check_catalog_write_access(actor, key)
        key.api_key_encrypted = encrypt_channel_key(new_api_key)
        key.key_hint = key_hint(new_api_key)
        key.updated_by = actor.user_id
        await self.session.flush()
        add_after_commit_hook(self.session, _publish_router_invalidation)
        add_after_commit_hook(self.session, _bump_catalog_cache_generation)
        return key

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
        add_after_commit_hook(self.session, _bump_catalog_cache_generation)
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
        add_after_commit_hook(self.session, _bump_catalog_cache_generation)
        return model

    async def delete_model(self, model_id: int, *, actor: AuthenticatedUser) -> None:
        model = await self._require_model(model_id)
        if await self.models.count_active_deployments(model_id) > 0:
            raise AppError(ErrorCode.request_conflict, status.HTTP_409_CONFLICT)

        model.updated_by = actor.user_id
        await self.models.soft_delete(model)
        add_after_commit_hook(self.session, _publish_router_invalidation)
        add_after_commit_hook(self.session, _bump_catalog_cache_generation)

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
        add_after_commit_hook(self.session, _bump_catalog_cache_generation)
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
        add_after_commit_hook(self.session, _bump_catalog_cache_generation)
        return deployment

    async def delete_deployment(
        self, deployment_id: int, *, actor: AuthenticatedUser
    ) -> None:
        deployment = await self._require_deployment(deployment_id)
        deployment.updated_by = actor.user_id
        await self.deployments.soft_delete(deployment)
        add_after_commit_hook(self.session, _publish_router_invalidation)
        add_after_commit_hook(self.session, _bump_catalog_cache_generation)


async def _publish_router_invalidation() -> None:
    """Publish router invalidation event (best-effort, non-blocking)."""
    with suppress(Exception):
        await get_redis().publish("gateway:router:invalidate", "1")


async def _bump_catalog_cache_generation() -> None:
    """Bump catalog generation counter so cached resolutions become stale."""
    with suppress(Exception):
        from src.gateway.cache import bump_catalog_generation

        await bump_catalog_generation()


def _is_internal_host(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_loopback or ip.is_private or ip.is_link_local
