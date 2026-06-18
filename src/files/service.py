"""附件业务逻辑(service 层)。

两条上传链路,共用「对象优先」持久化收口(``_persist``):

- :meth:`upload_avatar` —— 头像:校验+转码(``core.image``,丢线程池跑同步 Pillow)→
  webp 原图剥 EXIF → object key ``avatar/{YYYY}/{MM}/{id}.webp``。
- :meth:`upload_attachment` —— 通用附件:仅校验大小,原样落桶(保留原始扩展名/类型,
  不解码不转码)→ object key ``attachment/{YYYY}/{MM}/{id}{ext}``。

**上传顺序**:先上传对象、再落库。若请求边界提交失败,桶里至多留个孤儿对象(可后续按
``sys_attach`` 元数据扫未引用对象清理);反过来(先落库后上传)会留下指向不存在对象的
悬空行,体验更差。故选「对象优先」。

事务边界由请求级 Unit of Work 持有,本层只 ``flush()``;凭据校验在 ``ObjectStorage``
构造期已 fail-fast。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import PurePosixPath

from sqlalchemy.ext.asyncio import AsyncSession
from starlette import status

from src.auth.service import AuthenticatedUser
from src.core.image import ImageTooLargeError, InvalidImageError, to_webp_avatar
from src.core.oss import ObjectStorage
from src.core.snowflake import next_id
from src.db.models.attach import SysAttach
from src.enums import AttachBizType, ErrorCode
from src.exceptions import AppError
from src.files.repository import SysAttachRepository

# 通用附件回退 content-type(客户端未声明时)。
_DEFAULT_CONTENT_TYPE = "application/octet-stream"
# 扩展名白名单字符(防 object key 注入/路径穿越)。
_SAFE_EXT_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
)


def _safe_extension(original_name: str | None) -> str:
    """从原始文件名安全地取扩展名(含点,如 ``.pdf``),无法判定则返回空串。

    仅保留字母数字后缀,杜绝 ``../`` 或控制字符混进 object key;过长(>10)的可疑
    后缀直接丢弃(当作无扩展名),宁可丢扩展名也不写危险的 key。
    """
    if not original_name:
        return ""
    suffix = PurePosixPath(original_name).suffix  # 含前导点,如 ".PDF"
    if not suffix:
        return ""
    body = suffix[1:]
    if not body or len(body) > 10 or any(ch not in _SAFE_EXT_CHARS for ch in body):
        return ""
    return f".{body.lower()}"


class AttachService:
    def __init__(self, session: AsyncSession, storage: ObjectStorage) -> None:
        self.session = session
        self.repo = SysAttachRepository(session)
        self.storage = storage

    async def upload_avatar(
        self,
        raw: bytes,
        *,
        original_name: str | None,
        actor: AuthenticatedUser,
        max_bytes: int,
    ) -> tuple[SysAttach, str]:
        """校验+转 webp,上传桶并落 ``sys_attach`` 行,返回 (行, 预签名 URL)。

        ``core.image`` 的 web-agnostic 异常在此翻译成 ``AppError``(与 ``AuthService``
        翻译 ``TokenError`` 同构):超限 → ``attach_too_large``、非图片 →
        ``attach_invalid_image``,均 400。翻译落在 service 层,故 router 无需 try/except,
        全局拦截器统一出信封。校验失败时**绝不上传、绝不落库**。
        """
        # Pillow is sync CPU work — offload so the event loop keeps serving.
        try:
            converted = await asyncio.to_thread(
                to_webp_avatar, raw, max_bytes=max_bytes
            )
        except ImageTooLargeError as exc:
            raise AppError(
                ErrorCode.attach_too_large, status.HTTP_400_BAD_REQUEST
            ) from exc
        except InvalidImageError as exc:
            raise AppError(
                ErrorCode.attach_invalid_image, status.HTTP_400_BAD_REQUEST
            ) from exc

        attach_id = next_id()
        now = datetime.now(tz=UTC)
        object_key = f"avatar/{now:%Y}/{now:%m}/{attach_id}.webp"
        return await self._persist(
            attach_id=attach_id,
            object_key=object_key,
            data=converted.data,
            content_type=converted.content_type,
            original_name=original_name,
            biz_type=AttachBizType.avatar,
            actor=actor,
        )

    async def upload_attachment(
        self,
        raw: bytes,
        *,
        original_name: str | None,
        content_type: str | None,
        actor: AuthenticatedUser,
        max_bytes: int,
    ) -> tuple[SysAttach, str]:
        """校验大小,原样上传桶并落 ``sys_attach`` 行,返回 (行, 预签名 URL)。

        通用附件不解码不转码:保留客户端声明的 ``content_type``(缺省回退
        ``application/octet-stream``)与原始扩展名。超限 → ``attach_too_large`` (400)。
        校验失败时**绝不上传、绝不落库**。
        """
        if len(raw) > max_bytes:
            raise AppError(ErrorCode.attach_too_large, status.HTTP_400_BAD_REQUEST)

        attach_id = next_id()
        now = datetime.now(tz=UTC)
        ext = _safe_extension(original_name)
        object_key = f"attachment/{now:%Y}/{now:%m}/{attach_id}{ext}"
        return await self._persist(
            attach_id=attach_id,
            object_key=object_key,
            data=raw,
            content_type=content_type or _DEFAULT_CONTENT_TYPE,
            original_name=original_name,
            biz_type=AttachBizType.attachment,
            actor=actor,
            force_download=True,
        )

    async def _persist(
        self,
        *,
        attach_id: int,
        object_key: str,
        data: bytes,
        content_type: str,
        original_name: str | None,
        biz_type: AttachBizType,
        actor: AuthenticatedUser,
        force_download: bool = False,
    ) -> tuple[SysAttach, str]:
        """对象优先持久化:上传桶 → 落行(flush)→ 现算预签名 URL。

        事务由请求级 Unit of Work 在边界提交;本层只 flush 让行物化。失败的提交至多在
        桶里留个孤儿对象(可按 ``sys_attach`` 元数据清理),绝不会留下指向不存在对象的
        悬空行。预签名 URL 仅按 object key 现算,不依赖行已落库,故 flush 后即可生成。
        两条上传链路共用此收口,保证顺序与事务边界一致。``force_download`` 透传给预签名:
        通用附件强制 ``Content-Disposition: attachment`` 防内联渲染(stored-XSS),头像不强制。
        """
        await self.storage.upload(
            object_key=object_key,
            data=data,
            content_type=content_type,
        )

        attach = SysAttach(
            id=attach_id,
            object_key=object_key,
            bucket=self.storage.bucket,
            original_name=original_name,
            content_type=content_type,
            file_size=len(data),
            biz_type=biz_type,
            created_by=actor.user_id,
            create_dept=actor.department_id,
            updated_by=actor.user_id,
        )
        await self.repo.create(attach)
        await self.session.flush()

        url = await self.storage.presign_get(object_key, force_download=force_download)
        return attach, url

    async def presigned_url(self, attach_id: int) -> str | None:
        """为已存在的附件现算预签名 URL;不存在/软删返回 ``None``。"""
        attach = await self.repo.get(attach_id)
        if attach is None:
            return None
        return await self.storage.presign_get(attach.object_key)
