"""会话存储（纯 Redis）：access token 白名单 + 不透明 refresh token 轮换。

会话状态全部落 Redis（用户决策：不建 refresh_token 表，纯 Redis）。本模块是
**横切基础设施**——只读写 Redis、返回数据/结果对象，不抛 ``AppError``、不碰 FastAPI；
由上层 service 把结果翻译成带 ``ErrorCode`` 的响应（与 ``security.py`` 同层定位）。

设计（Oracle 评审锁定）——**白名单**模型而非黑名单：每签发一个 access token，其
``jti`` 注册进 Redis；``resolve_access_token`` 校验「该 jti 是否仍在册」，登出/踢下线
即删除该 jti。白名单天然支撑「在线会话枚举 + 踢人」（M-B5）与「改密强制全端下线」
（M-B7），黑名单则需另建并行索引、变成两套系统。

refresh token 用**不透明随机串**（``secrets.token_urlsafe``，Redis 只存其 sha256），
而非 refresh-JWT：Redis 已是真相源，refresh-JWT 只徒增验签边界、不省 Redis 依赖。

轮换的原子性靠 ``GETDEL``（非 Lua）：「取旧 refresh 记录并删除」这一步即串行化点——
并发轮换里第一个调用方拿到值并删除，竞争者读到 ``None`` 即出局。轮换后把旧 refresh
hash 短期记入 ``used:`` 标记；若已轮换的 refresh 被再次提交（典型失窃信号），判定为
重放并吊销该用户全部会话（连同攻击者与合法用户一起踢下线，强制重新登录）。

Redis key 模式：
- ``sess:access:{jti}``        -> JSON ``{"user_id", "refresh_hash"}``，TTL = access 寿命
- ``sess:user:{user_id}``      -> set[access_jti]，用于按用户枚举/批量吊销
- ``sess:refresh:{hash}``      -> JSON ``{"user_id", "access_jti"}``，TTL = refresh 寿命
- ``sess:refresh:used:{hash}`` -> JSON ``{"user_id"}``，重放检测标记，TTL = 有界安全窗
"""

from __future__ import annotations

import json
from collections.abc import Awaitable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum, auto
from typing import cast

from redis.asyncio import Redis

_ACCESS_PREFIX = "sess:access:"
_USER_PREFIX = "sess:user:"
_REFRESH_PREFIX = "sess:refresh:"
_REFRESH_USED_PREFIX = "sess:refresh:used:"
_USERS_INDEX = "sess:users"  # set[user_id]:有活跃会话的用户,供「在线列表」枚举(B5)


class RefreshOutcome(Enum):
    """``consume_refresh`` 的三态结果（内部控制流信号，非对外 ErrorCode）。"""

    ok = auto()  # refresh 有效且被本次调用消费(轮换胜出方)
    invalid = auto()  # refresh 不存在且无重放痕迹(过期/伪造)
    reuse_detected = auto()  # 已轮换的 refresh 被重放——已吊销该用户全部会话


@dataclass(frozen=True, slots=True)
class RefreshConsumeResult:
    """``consume_refresh`` 的结果:结局 + 取决于结局的附带数据。

    - ``ok``:``user_id`` 与 ``old_access_jti`` 均给出(供 service 签发新对、吊销旧 access)。
    - ``reuse_detected``:``user_id`` 给出(会话已被吊销,供审计/日志);``old_access_jti`` 为 None。
    - ``invalid``:两者皆 None。
    """

    outcome: RefreshOutcome
    user_id: int | None = None
    old_access_jti: str | None = None


@dataclass(frozen=True, slots=True)
class SessionInfo:
    """单个活跃会话的元数据快照(供「在线会话」列表 B5)。

    ``username`` 不落 Redis(避免改名后陈旧),由上层 service 用 ``user_id`` 批量补齐;
    ``login_at`` 为本会话建立(登录或轮换)时刻的 ISO-8601 UTC 串。
    """

    access_jti: str
    user_id: int
    ip: str | None
    user_agent: str | None
    login_at: str


class SessionStore:
    """会话白名单 + refresh 轮换的 Redis 读写封装(注入 Redis client,便于测试)。"""

    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    # ---- key 拼装 -----------------------------------------------------------

    @staticmethod
    def _access_key(jti: str) -> str:
        return f"{_ACCESS_PREFIX}{jti}"

    @staticmethod
    def _user_key(user_id: int) -> str:
        return f"{_USER_PREFIX}{user_id}"

    @staticmethod
    def _refresh_key(refresh_hash: str) -> str:
        return f"{_REFRESH_PREFIX}{refresh_hash}"

    @staticmethod
    def _refresh_used_key(refresh_hash: str) -> str:
        return f"{_REFRESH_USED_PREFIX}{refresh_hash}"

    # ---- 写入:登录建会话 ---------------------------------------------------

    async def create_session(
        self,
        *,
        user_id: int,
        access_jti: str,
        access_ttl: int,
        refresh_hash: str,
        refresh_ttl: int,
        ip: str | None = None,
        user_agent: str | None = None,
    ) -> None:
        """登录成功后落一对 (access, refresh) 会话记录,并把 access jti 入用户索引。

        用户索引集合的 TTL 取 refresh 寿命(最长寿命的会话维度),每次登录/轮换刷新,
        避免索引早于其成员过期而漏掉仍活跃的会话。``ip``/``user_agent`` 与 ``login_at``
        随 access 记录落盘,供「在线会话」列表(B5)展示;轮换视作新会话,元数据按本次
        请求重新采集。``sess:users`` 全局索引登记该用户(SADD),供跨用户枚举在线列表。
        """
        access_record = json.dumps(
            {
                "user_id": user_id,
                "refresh_hash": refresh_hash,
                "ip": ip,
                "user_agent": user_agent,
                "login_at": datetime.now(UTC).isoformat(),
            }
        )
        refresh_record = json.dumps({"user_id": user_id, "access_jti": access_jti})
        await self._redis.set(self._access_key(access_jti), access_record, ex=access_ttl)
        await self._redis.set(
            self._refresh_key(refresh_hash), refresh_record, ex=refresh_ttl
        )
        user_key = self._user_key(user_id)
        await cast("Awaitable[int]", self._redis.sadd(user_key, access_jti))
        await self._redis.expire(user_key, refresh_ttl)
        await cast("Awaitable[int]", self._redis.sadd(_USERS_INDEX, str(user_id)))

    # ---- 读取:每请求吊销校验 -----------------------------------------------

    async def is_access_active(self, access_jti: str) -> bool:
        """该 access jti 是否仍在白名单(未登出/未被踢/未过期)。"""
        return bool(await self._redis.exists(self._access_key(access_jti)))

    # ---- 吊销:登出 / 踢单个会话 --------------------------------------------

    async def revoke_access(self, access_jti: str) -> None:
        """吊销单个 access 会话:删 access 记录 + 移出用户索引 + 删其绑定的 refresh。

        幂等:记录已不在(已登出/已过期)时静默返回。绑定的 refresh 若已被轮换消费,
        二次删除返回 0,无副作用。
        """
        raw = await self._redis.get(self._access_key(access_jti))
        await self._redis.delete(self._access_key(access_jti))
        if raw is None:
            return
        record = self._parse_record(raw)
        if record is None:
            return
        user_id = record.get("user_id")
        refresh_hash = record.get("refresh_hash")
        if isinstance(user_id, int):
            await cast(
                "Awaitable[int]", self._redis.srem(self._user_key(user_id), access_jti)
            )
            await self._prune_user_index(user_id)
        if isinstance(refresh_hash, str):
            await self._redis.delete(self._refresh_key(refresh_hash))

    # ---- refresh 轮换:原子消费 + 重放检测 ---------------------------------

    async def consume_refresh(
        self, refresh_hash: str, *, used_marker_ttl: int
    ) -> RefreshConsumeResult:
        """原子消费一个 refresh token,返回轮换所需信息或重放/无效判定。

        ``GETDEL`` 是串行化点:并发轮换中仅一方取到记录,竞争方读到 None。成功消费后
        写 ``used:`` 标记(TTL = 有界安全窗),使该 refresh 的再次提交可被识别为重放并
        吊销该用户全部会话。service 拿到 ``ok`` 后负责签发新对、并吊销 ``old_access_jti``。
        """
        raw = await self._redis.getdel(self._refresh_key(refresh_hash))
        if raw is not None:
            record = self._parse_record(raw)
            if record is None:
                return RefreshConsumeResult(RefreshOutcome.invalid)
            user_id = record.get("user_id")
            old_access_jti = record.get("access_jti")
            if not isinstance(user_id, int) or not isinstance(old_access_jti, str):
                return RefreshConsumeResult(RefreshOutcome.invalid)
            await self._redis.set(
                self._refresh_used_key(refresh_hash),
                json.dumps({"user_id": user_id}),
                ex=used_marker_ttl,
            )
            return RefreshConsumeResult(
                RefreshOutcome.ok, user_id=user_id, old_access_jti=old_access_jti
            )

        # refresh 不存在:可能是已轮换后的重放(used 标记还在),也可能纯属无效。
        used_raw = await self._redis.get(self._refresh_used_key(refresh_hash))
        if used_raw is None:
            return RefreshConsumeResult(RefreshOutcome.invalid)
        used = self._parse_record(used_raw)
        user_id = used.get("user_id") if used is not None else None
        if isinstance(user_id, int):
            await self.revoke_all_sessions(user_id)
            return RefreshConsumeResult(RefreshOutcome.reuse_detected, user_id=user_id)
        return RefreshConsumeResult(RefreshOutcome.invalid)

    # ---- 批量吊销:重放检测 / 改密强制全端下线(B7) -----------------------

    async def revoke_all_sessions(self, user_id: int) -> None:
        """吊销某用户的全部会话:逐个删 access 记录 + 其绑定 refresh,最后清空索引。"""
        user_key = self._user_key(user_id)
        jtis = await cast("Awaitable[set[str]]", self._redis.smembers(user_key))
        for jti in jtis:
            raw = await self._redis.get(self._access_key(jti))
            await self._redis.delete(self._access_key(jti))
            if raw is None:
                continue
            record = self._parse_record(raw)
            if record is None:
                continue
            refresh_hash = record.get("refresh_hash")
            if isinstance(refresh_hash, str):
                await self._redis.delete(self._refresh_key(refresh_hash))
        await self._redis.delete(user_key)
        await cast("Awaitable[int]", self._redis.srem(_USERS_INDEX, str(user_id)))

    # ---- 在线会话枚举(B5)-------------------------------------------------

    async def list_user_sessions(self, user_id: int) -> list[SessionInfo]:
        """列出某用户当前全部活跃会话(读时惰性剔除已过期的索引成员)。

        用户索引(set)的成员是 access jti;其对应 access 记录可能已 TTL 过期而 jti 仍
        滞留索引中——逐个取记录,缺失者顺手 ``SREM`` 清理,返回仍存活的会话元数据。
        """
        user_key = self._user_key(user_id)
        jtis = await cast("Awaitable[set[str]]", self._redis.smembers(user_key))
        sessions: list[SessionInfo] = []
        for jti in jtis:
            raw = await self._redis.get(self._access_key(jti))
            if raw is None:
                await cast("Awaitable[int]", self._redis.srem(user_key, jti))
                continue
            record = self._parse_record(raw)
            if record is None:
                continue
            sessions.append(self._to_session_info(jti, user_id, record))
        await self._prune_user_index(user_id)
        return sessions

    async def list_all_sessions(self) -> list[SessionInfo]:
        """列出全平台所有用户的活跃会话(在线列表),读时惰性剔除空用户索引。

        遍历 ``sess:users`` 全局索引,对每个用户调 :meth:`list_user_sessions`;某用户已无
        存活会话时,从全局索引中 ``SREM`` 该用户,避免索引无界增长。
        """
        user_ids = await cast("Awaitable[set[str]]", self._redis.smembers(_USERS_INDEX))
        sessions: list[SessionInfo] = []
        for raw_uid in user_ids:
            try:
                uid = int(raw_uid)
            except ValueError:
                await cast("Awaitable[int]", self._redis.srem(_USERS_INDEX, raw_uid))
                continue
            user_sessions = await self.list_user_sessions(uid)
            if not user_sessions:
                await cast("Awaitable[int]", self._redis.srem(_USERS_INDEX, raw_uid))
                continue
            sessions.extend(user_sessions)
        return sessions

    async def _prune_user_index(self, user_id: int) -> None:
        """该用户已无活跃会话时,从 ``sess:users`` 全局索引中移除(否则保留)。"""
        remaining = await self._redis.exists(self._user_key(user_id))
        if not remaining:
            await cast("Awaitable[int]", self._redis.srem(_USERS_INDEX, str(user_id)))

    @staticmethod
    def _to_session_info(
        access_jti: str, user_id: int, record: dict[str, object]
    ) -> SessionInfo:
        """把 access 记录 dict 容错地映射成 :class:`SessionInfo`(非串字段降级为 None/空)。"""
        ip = record.get("ip")
        user_agent = record.get("user_agent")
        login_at = record.get("login_at")
        return SessionInfo(
            access_jti=access_jti,
            user_id=user_id,
            ip=ip if isinstance(ip, str) else None,
            user_agent=user_agent if isinstance(user_agent, str) else None,
            login_at=login_at if isinstance(login_at, str) else "",
        )

    # ---- 内部:容错解析 -----------------------------------------------------

    @staticmethod
    def _parse_record(raw: str) -> dict[str, object] | None:
        """把 Redis 取回的 JSON 串解析为 dict;非 JSON object 一律视作脏数据返回 None。"""
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            return None
        if isinstance(parsed, dict):
            return parsed
        return None
