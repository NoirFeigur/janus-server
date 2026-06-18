"""对象存储抽象(MinIO / S3 兼容)—— core 层无业务横切基础设施。

私有桶的访问模型:**读取一律走短期预签名 URL**(后端用 access/secret 本地算 HMAC
签名,零网络往返),**DB 只存桶内 object key**,绝不存永久 URL(私有桶无公开 URL,
存了也立刻过期)。换 S3 / 阿里云 OSS 只改 ``.env`` 里的 endpoint/桶/密钥,本模块不动。

``ObjectStorage`` 是薄封装:每次操作开一个 aioboto3 client(无状态 session,与
'无状态副本'一致),做完即关。三个原语:

- :meth:`upload` —— 上传字节流(后端代理上传:字节先过应用做校验/转码,再落桶)。
- :meth:`presign_get` —— 为某 object key 现算一个限时可读的预签名 URL。
- :meth:`delete` —— 删除对象(孤儿清理 / 头像替换后清旧图)。

凭据缺失在构造期即拒绝(fail-fast),不留到首次调用才在桶里炸。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Annotated

import aioboto3
from aiobotocore.config import AioConfig
from fastapi import Depends

from src.config import get_settings

if TYPE_CHECKING:
    from botocore.awsrequest import AWSPreparedRequest
    from types_aiobotocore_s3 import S3Client


def _strip_expect_header(request: AWSPreparedRequest, **kwargs: object) -> None:
    """Drop ``Expect: 100-continue`` from outgoing requests.

    boto3 attaches ``Expect: 100-continue`` to body-bearing writes (PUT) and then
    waits for an interim ``100 Continue`` before sending the body. Some MinIO
    builds / reverse proxies never send it, so the write stalls indefinitely
    (reads have no body, no ``Expect`` header, and are unaffected). Stripping the
    header makes the client send the body immediately — the universal fix for
    "uploads hang against S3-compatible storage".
    """
    if "Expect" in request.headers:
        del request.headers["Expect"]


class ObjectStorage:
    """私有桶对象存储的最小封装(upload / presign_get / delete)。"""

    def __init__(
        self,
        *,
        endpoint_url: str,
        region: str,
        bucket: str,
        access_key: str | None,
        secret_key: str | None,
        presign_ttl_seconds: int,
    ) -> None:
        if not access_key or not secret_key:
            raise ValueError(
                "object storage credentials are not configured "
                "(set JANUS_OSS_ACCESS_KEY / JANUS_OSS_SECRET_KEY)"
            )
        self._endpoint_url = endpoint_url
        self._region = region
        self._bucket = bucket
        self._access_key = access_key
        self._secret_key = secret_key
        self._presign_ttl = presign_ttl_seconds

    @property
    def bucket(self) -> str:
        """The bucket all objects land in (stored redundantly on each row)."""
        return self._bucket

    def _session(self) -> aioboto3.Session:
        return aioboto3.Session()

    def _config(self) -> AioConfig:
        """botocore config tuned for MinIO / S3-compatible servers.

        Path-style addressing (``endpoint/bucket/key``) — virtual-host style
        (``bucket.endpoint``) cannot resolve against a bare IP endpoint. Safe for
        AWS S3 too (it accepts path-style), so the wrapper stays portable.
        """
        return AioConfig(s3={"addressing_style": "path"})

    @asynccontextmanager
    async def _client(self) -> AsyncIterator[S3Client]:
        """开一个配置好端点/区域/凭据的 S3 client(异步上下文管理器)。

        注册 ``before-send`` 钩子剥掉 ``Expect: 100-continue``,否则写请求会在等待
        MinIO/代理永不返回的 ``100 Continue`` 时无限挂起(读请求无 body 不受影响)。
        """
        async with self._session().client(
            "s3",
            endpoint_url=self._endpoint_url,
            region_name=self._region,
            aws_access_key_id=self._access_key,
            aws_secret_access_key=self._secret_key,
            config=self._config(),
        ) as client:
            client.meta.events.register("before-send.s3", _strip_expect_header)
            yield client

    async def upload(
        self, *, object_key: str, data: bytes, content_type: str
    ) -> None:
        """把字节流写入桶内 ``object_key``(后端代理上传,字节已过应用校验)。"""
        async with self._client() as client:
            await client.put_object(
                Bucket=self._bucket,
                Key=object_key,
                Body=data,
                ContentType=content_type,
            )

    async def presign_get(self, object_key: str) -> str:
        """为 ``object_key`` 现算一个限时(``presign_ttl``)可读预签名 URL。"""
        async with self._client() as client:
            return await client.generate_presigned_url(
                "get_object",
                Params={"Bucket": self._bucket, "Key": object_key},
                ExpiresIn=self._presign_ttl,
            )

    async def delete(self, object_key: str) -> None:
        """删除桶内 ``object_key``(孤儿清理 / 头像替换后清旧图)。"""
        async with self._client() as client:
            await client.delete_object(Bucket=self._bucket, Key=object_key)


def get_object_storage() -> ObjectStorage:
    """FastAPI 依赖:从 settings 装配 ObjectStorage(凭据缺失即在此抛错)。"""
    settings = get_settings()
    return ObjectStorage(
        endpoint_url=settings.oss_endpoint_url,
        region=settings.oss_region,
        bucket=settings.oss_bucket,
        access_key=(
            settings.oss_access_key.get_secret_value()
            if settings.oss_access_key is not None
            else None
        ),
        secret_key=(
            settings.oss_secret_key.get_secret_value()
            if settings.oss_secret_key is not None
            else None
        ),
        presign_ttl_seconds=settings.oss_presign_ttl_seconds,
    )


ObjectStorageDep = Annotated[ObjectStorage, Depends(get_object_storage)]


def get_object_storage_optional() -> ObjectStorage | None:
    """Like :func:`get_object_storage` but returns ``None`` when credentials are
    absent instead of raising.

    For read paths that merely *enrich* a response with a presigned URL (e.g. the
    avatar URL on ``/auth/me``): if OSS is not configured the endpoint should
    still work and simply omit the URL, rather than 500. Upload paths keep using
    the strict :func:`get_object_storage`.
    """
    try:
        return get_object_storage()
    except ValueError:
        return None


OptionalObjectStorageDep = Annotated[
    ObjectStorage | None, Depends(get_object_storage_optional)
]
