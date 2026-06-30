"""Admin 元数据端点(meta)。

向前端暴露**与请求语言对应**的元数据。当前提供枚举码表
(``GET /admin/meta/enums``):返回当前 locale 下全部 ``enum.*`` 词条
(``{ "enum.userStatus.active": "启用", ... }``),作为前端 ProTable ``valueEnum``
与 Excel/CSV 导出 code→label 的**单一可信源**(架构决策 G16 / 6.12)。

鉴权:仅要求**已认证**(:data:`CurrentJwtUser`),不挂具体权限——码表是所有管理面
页面渲染所需的通用参照数据,任一登录管理员都需读取。
"""

from __future__ import annotations

from fastapi import APIRouter

from src.auth.dependencies import CurrentJwtUser, TraceId
from src.core.i18n import get_i18n, get_locale
from src.responses import SuccessEnvelope, success

router = APIRouter(prefix="/meta", tags=["admin:meta"])


@router.get("/enums", response_model=SuccessEnvelope[dict[str, str]])
async def get_enums(
    _: CurrentJwtUser,
    trace_id: TraceId,
) -> SuccessEnvelope[dict[str, str]]:
    """返回当前 locale 下全部 ``enum.*`` 码表(code→label)。"""
    labels = get_i18n().prefixed("enum.", get_locale())
    return success(labels, trace_id=trace_id)
