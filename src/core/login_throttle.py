"""登录防爆破节流（纯 Redis）：单用户名失败锁定 + 单 IP 滑窗粗限流。

横切基础设施——只读写 Redis、返回布尔/计数,**不抛 ``AppError``、不碰 FastAPI**;由
上层 service 把「已锁定」翻译成带 ``ErrorCode`` 的响应(与 ``session_store.py`` 同层定位)。

两道闸(用户决策:纯 Redis,不建表):

1. **按用户名锁定**——登录失败累加 ``login:fail:{username}`` 计数(TTL = 失败滑窗);
   达到 ``max_failures`` 即写 ``login:lock:{username}`` 锁标记(TTL = 锁定时长)。锁定期内
   ``is_locked`` 为真,即便密码正确 service 也应拒绝。登录成功调 ``reset`` 清零计数与锁。

2. **按 IP 滑窗粗限流**——登录失败累加 ``login:ip:{ip}`` 计数(TTL = IP 滑窗),用于挡住
   「同一来源短时间枚举大量用户名」绕开单账户锁的爆破;达到 ``ip_max_failures`` 即视为
   超限(``is_ip_limited``)。IP 维度仅计失败、不设独立锁标记(滑窗到期自然恢复)。

key 模式:
- ``login:fail:{username}`` -> int 失败计数, TTL = ``failure_window_seconds``
- ``login:lock:{username}``  -> "1" 锁标记,   TTL = ``lockout_seconds``
- ``login:ip:{ip}``          -> int 失败计数, TTL = ``ip_window_seconds``
"""

from __future__ import annotations

from dataclasses import dataclass

from src.core.redis import AsyncRedis

_FAIL_PREFIX = "login:fail:"
_LOCK_PREFIX = "login:lock:"
_IP_PREFIX = "login:ip:"


@dataclass(frozen=True, slots=True)
class ThrottlePolicy:
    """节流阈值与时间窗（来自 settings，注入便于测试与调参）。"""

    max_failures: int  # 单用户名连续失败上限,达到即锁定
    lockout_seconds: int  # 锁定时长(锁定期内拒绝登录)
    failure_window_seconds: int  # 单用户名失败计数滑窗
    ip_max_failures: int  # 单 IP 滑窗内失败上限(粗粒度限流)
    ip_window_seconds: int  # 单 IP 失败计数滑窗


class LoginThrottle:
    """登录防爆破的 Redis 读写封装（注入 Redis client + 策略,便于测试）。"""

    def __init__(self, redis: AsyncRedis, policy: ThrottlePolicy) -> None:
        self._redis = redis
        self._policy = policy

    # ---- key 拼装 -----------------------------------------------------------

    @staticmethod
    def _fail_key(username: str) -> str:
        return f"{_FAIL_PREFIX}{username}"

    @staticmethod
    def _lock_key(username: str) -> str:
        return f"{_LOCK_PREFIX}{username}"

    @staticmethod
    def _ip_key(ip: str) -> str:
        return f"{_IP_PREFIX}{ip}"

    # ---- 登录前:闸门检查 ----------------------------------------------------

    async def is_locked(self, username: str) -> bool:
        """该用户名当前是否处于锁定期(失败累计达阈值后的冷却窗)。"""
        return bool(await self._redis.exists(self._lock_key(username)))

    async def is_ip_limited(self, ip: str) -> bool:
        """该 IP 当前是否已达滑窗失败上限(粗粒度限流闸)。"""
        raw = await self._redis.get(self._ip_key(ip))
        if raw is None:
            return False
        try:
            return int(raw) >= self._policy.ip_max_failures
        except (ValueError, TypeError):
            return False

    # ---- 登录失败:累计 + 触发锁定 ------------------------------------------

    async def record_failure(self, username: str, *, ip: str | None = None) -> None:
        """记一次登录失败:累加用户名计数(并在达阈值时落锁),同时累加 IP 计数。

        计数用 ``INCR``;首次累加(返回 1)时设 TTL = 失败滑窗,使「窗口内无新失败则
        计数自然过期」。达到 ``max_failures`` 即写锁标记(TTL = 锁定时长)。IP 计数同理:
        首次设 TTL = IP 滑窗。
        """
        fail_key = self._fail_key(username)
        count = await self._redis.incr(fail_key)
        if count == 1:
            await self._redis.expire(fail_key, self._policy.failure_window_seconds)
        if count >= self._policy.max_failures:
            await self._redis.set(
                self._lock_key(username), "1", ex=self._policy.lockout_seconds
            )
        if ip is not None:
            ip_key = self._ip_key(ip)
            ip_count = await self._redis.incr(ip_key)
            if ip_count == 1:
                await self._redis.expire(ip_key, self._policy.ip_window_seconds)

    # ---- 登录成功:清零 ------------------------------------------------------

    async def reset(self, username: str) -> None:
        """登录成功后清零该用户名的失败计数与锁标记(IP 计数保留,自然过期)。"""
        await self._redis.delete(self._fail_key(username), self._lock_key(username))
