"""系统级列表/查询/批量请求侧基建(README:core 跨领域基础设施,非业务)。

本模块收口**整个系统**(不限管理后台)复用的「请求侧」集合处理契约,与
:mod:`src.core.pagination` 的「响应侧」(``Page`` / ``PageResult``)互为一对:

- :class:`ListQuery` + :func:`resolve_sort` —— 列表端点统一的查询入参与排序解析。
  ``ListQuery`` 本身与资源无关(只认排序「键」字符串),具体允许排序的列由调用方
  按资源传入白名单。:func:`resolve_sort` 把字符串键映射成预先批准的 SQLAlchemy 列,
  **杜绝把裸列名透传进 ORDER BY**(SQL 注入护栏)。
- :func:`mask_fields` —— 统一的敏感字段脱敏:非超管看到掩码,超管看到原值。仅依赖
  :class:`MaskActor` 这一最小结构化协议(只读 ``is_superuser``),因此 core 层不反向
  依赖 auth —— ``AuthenticatedUser`` 以鸭子类型天然满足该协议。
- :class:`BatchIdsRequest` / :class:`BatchResult` —— 批量操作的入参与结果 DTO。

放在 ``src/core/`` 而非某个领域包内,是因为这些原语与领域无关:网关列用量、auth 列
会话等任何领域都应直接复用,而不是各自造轮子。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Literal, Protocol, TypeVar

from fastapi import status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm.attributes import InstrumentedAttribute

from src.enums import ErrorCode
from src.exceptions import AppError

T = TypeVar("T", bound=BaseModel)


# --- 列表查询与排序解析 ---------------------------------------------------------


class ListQuery(BaseModel):
    """列表端点通用查询入参。

    与具体资源解耦:``keyword`` 的检索列、``sort_by`` 的可选列均由调用方(各资源
    service/repository)决定。``sort_by`` 只是一个字符串键,真正能否排序由
    :func:`resolve_sort` 对照调用方传入的白名单裁决。
    """

    keyword: str | None = None  # 自由文本检索;具体匹配哪些列由 service 决定。
    sort_by: str | None = None  # 排序键(白名单内的列别名),None 表示用默认列。
    sort_order: Literal["asc", "desc"] = "asc"
    limit: int = Field(default=50, ge=1, le=200)
    offset: int = Field(default=0, ge=0)


def resolve_sort(
    query: ListQuery,
    *,
    allowed: Mapping[str, InstrumentedAttribute[Any]],
    default: str,
) -> tuple[InstrumentedAttribute[Any], bool]:
    """把 :class:`ListQuery` 的排序意图解析成 (列, 是否降序)。

    ``allowed`` 是调用方按资源提供的白名单:键为对外暴露的排序别名,值为预先批准的
    SQLAlchemy 列。``sort_by`` 为空时回落到 ``default``;只要 ``sort_by`` 不在白名单内
    就抛 :class:`AppError`,**绝不把未经核对的列名透传到 ORDER BY**(SQL 注入护栏)。

    返回的列 + 降序布尔可直接供 repository 拼 ``col.desc() if desc else col.asc()``。
    """
    key = query.sort_by if query.sort_by is not None else default
    column = allowed.get(key)
    if column is None:
        raise AppError(ErrorCode.request_invalid, status.HTTP_400_BAD_REQUEST)
    return column, query.sort_order == "desc"


# --- 敏感字段脱敏 ---------------------------------------------------------------


class MaskActor(Protocol):
    """脱敏所需的最小行为者契约:只关心调用者是否为超管。

    用结构化协议而非具体的 ``AuthenticatedUser``,让 core 层不反向依赖 auth 领域;
    任何带只读 ``is_superuser`` 的对象都能传入。
    """

    @property
    def is_superuser(self) -> bool: ...


def _mask_value(value: str) -> str:
    """对单个非空字符串做确定性脱敏。

    邮箱(含 ``@``)保留首字符与域名:``alice@x.com`` -> ``a***@x.com``。
    其余按手机号风格:保留前 3 后 4、中间统一 4 个 ``*``:``13800001111`` ->
    ``138****1111``。过短字符串优雅降级为「首字符 + ``***``」,信息不外泄。
    """
    if "@" in value:
        local, _, domain = value.partition("@")
        head = local[0] if local else ""
        return f"{head}***@{domain}"
    if len(value) <= 7:
        head = value[0] if value else ""
        return f"{head}***"
    return f"{value[:3]}****{value[-4:]}"


def mask_fields(
    read: T,
    *,
    actor: MaskActor,
    sensitive: Sequence[str] = ("mobile", "email"),
) -> T:
    """按调用者身份对 Pydantic 读模型做敏感字段脱敏。

    超管(``actor.is_superuser``)原样返回同一对象;否则对每个为非空 ``str`` 的敏感
    字段脱敏,经 ``model_copy(update=...)`` 产出新对象(不原地修改)。``None`` 仍为
    ``None``,非字符串字段不动,函数全量且确定性。
    """
    if actor.is_superuser:
        return read
    updates: dict[str, str] = {}
    for field in sensitive:
        value = getattr(read, field, None)
        if isinstance(value, str) and value:
            updates[field] = _mask_value(value)
    if not updates:
        return read
    return read.model_copy(update=updates)


# --- 批量操作 DTO ---------------------------------------------------------------


class BatchIdsRequest(BaseModel):
    """批量操作的雪花 ID 入参,兼容前端以字符串传输 ID。"""

    ids: list[int] = Field(min_length=1, max_length=200)

    @field_validator("ids", mode="before")
    @classmethod
    def coerce_string_ids(cls, value: object) -> object:
        """把字符串 ID 序列转换为整数序列,其余校验交给 Pydantic。"""
        if isinstance(value, list):
            return [int(item) if isinstance(item, str) else item for item in value]
        return value


class BatchResult(BaseModel):
    """批量操作结果,对外把跳过的雪花 ID 序列化为字符串。"""

    requested: int
    affected: int
    skipped_ids: list[str]

    @classmethod
    def of(cls, requested: int, affected: int, skipped: Sequence[int]) -> BatchResult:
        """按雪花 ID wire-format 构造批量操作结果。"""
        return cls(
            requested=requested,
            affected=affected,
            skipped_ids=[str(id_) for id_ in skipped],
        )
