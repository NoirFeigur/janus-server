"""平台配置的运行时读取器 + 类型解析 —— core 层无业务叶子模块。

``config`` 行以字符串存值,``value_type`` 标注如何解析。本模块提供两件事:

- :func:`parse_config_value` —— 把 ``(原始字符串, 值类型)`` 解析成 Python 值;解析
  失败抛 ``ValueError``。写侧(``admin.config`` service)用它做落库前校验,读侧用它
  解析缓存命中的原始值,**一处实现两处复用**(类型语义不漂移)。
- :func:`get_str` / :func:`get_int` / :func:`get_bool` / :func:`get_json` ——
  运行时按 key 读单个配置值,经 :func:`src.core.cache.get_or_load` 短 TTL 旁路缓存
  (fail-open:Redis 宕机直接查库)。未命中行返回调用方给的 ``default``。

刻意从 ``admin.config`` 拆出:运行时读取者(如未来的 auth/gateway 热路径)只想读一个
配置值,不该为此 import 整个 admin service(那会把 core→admin 的反向依赖拉进来)。
本模块只依赖 ``core.cache`` + ``db``,谁都能安全 import。

**一致性**:与 ``dept_tree_cache`` 同构——短 TTL 兜跨副本陈旧,写副本 commit 后主动
失效(:func:`invalidate_config`)。配置值有显式写/失效通道,适合缓存(区别于权限码)。
"""

from __future__ import annotations

import json

from sqlalchemy import select

from src.core import cache
from src.db.models.config import Config
from src.db.session import async_session_factory
from src.enums import ConfigValueType

TTL_SECONDS = 30


def _cache_key(config_key: str) -> str:
    return f"sys-config:{config_key}"


def parse_config_value(value: str, value_type: ConfigValueType) -> object:
    """把字符串配置值按 ``value_type`` 解析成 Python 值;非法值抛 ``ValueError``。

    - ``string`` —— 原样返回。
    - ``int``    —— base-10 整数(``int(value)``,空/非数字抛 ValueError)。
    - ``bool``   —— ``true/1/yes/on`` → True,``false/0/no/off`` → False(大小写不敏感);
      其余抛 ValueError(不静默当 False,避免拼写错误被吞)。
    - ``json``   —— ``json.loads``(对象/数组/标量皆可),非法 JSON 抛 ValueError。
    """
    if value_type == ConfigValueType.string:
        return value
    if value_type == ConfigValueType.int:
        return int(value)
    if value_type == ConfigValueType.bool:
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "on"}:
            return True
        if normalized in {"false", "0", "no", "off"}:
            return False
        raise ValueError(f"not a boolean: {value!r}")
    if value_type == ConfigValueType.json:
        try:
            return json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSON: {exc}") from exc
    raise ValueError(f"unknown value_type: {value_type!r}")


async def _load_raw(config_key: str) -> str | None:
    """查库取单个配置的原始字符串值(未命中/软删返回 None)。"""
    async with async_session_factory() as session:
        stmt = (
            select(Config.config_value)
            .where(Config.config_key == config_key)
            .where(Config.is_deleted.is_(False))
        )
        raw: str | None = await session.scalar(stmt)
        return raw


async def _get_raw(config_key: str) -> str | None:
    """旁路缓存读原始字符串值。

    缓存层只接受 ``str``,而「键不存在」是合法状态(返回 ``None``)。用 sentinel
    ``"\\x00<none>"`` 编码缺失,使缺失也能被缓存(避免对不存在的 key 反复穿透查库)。
    """
    _MISSING = "\x00<none>"

    async def loader() -> str:
        raw = await _load_raw(config_key)
        return _MISSING if raw is None else raw

    cached = await cache.get_or_load(
        _cache_key(config_key),
        loader,
        ttl_seconds=TTL_SECONDS,
        dumps=lambda v: v,
        loads=lambda v: v,
    )
    return None if cached == _MISSING else cached


async def get_str(config_key: str, *, default: str | None = None) -> str | None:
    """读字符串配置;key 不存在时返回 ``default``。"""
    raw = await _get_raw(config_key)
    if raw is None:
        return default
    return raw


async def get_int(config_key: str, *, default: int | None = None) -> int | None:
    """读整数配置;key 不存在或值非法时返回 ``default``。"""
    raw = await _get_raw(config_key)
    if raw is None:
        return default
    try:
        parsed = parse_config_value(raw, ConfigValueType.int)
    except ValueError:
        return default
    return parsed if isinstance(parsed, int) else default


async def get_bool(config_key: str, *, default: bool | None = None) -> bool | None:
    """读布尔配置;key 不存在或值非法时返回 ``default``。"""
    raw = await _get_raw(config_key)
    if raw is None:
        return default
    try:
        parsed = parse_config_value(raw, ConfigValueType.bool)
    except ValueError:
        return default
    return parsed if isinstance(parsed, bool) else default


async def get_json(config_key: str, *, default: object = None) -> object:
    """读 JSON 配置;key 不存在或值非法时返回 ``default``。"""
    raw = await _get_raw(config_key)
    if raw is None:
        return default
    try:
        return parse_config_value(raw, ConfigValueType.json)
    except ValueError:
        return default


async def invalidate_config(config_key: str) -> None:
    """失效单个配置的缓存(写侧 commit 后调用)。"""
    await cache.invalidate(_cache_key(config_key))
