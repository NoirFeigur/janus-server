"""登录防爆破节流（src/core/login_throttle.py）行为测试。

直接对注入的内存 Redis double 驱动 LoginThrottle,断言「关系契约」而非快照:
- 失败累计达阈值即锁定,锁定期内 is_locked 为真;
- 登录成功 reset 清零计数与锁;
- 按 IP 滑窗累计达上限即 is_ip_limited;
- 失败计数首次累加即带滑窗 TTL(窗口内无新失败则自然过期)。
"""

from __future__ import annotations

from typing import cast

import pytest
from redis.asyncio import Redis

from src.core.login_throttle import LoginThrottle, ThrottlePolicy
from tests._async_redis_double import AsyncRedisDouble

pytestmark = pytest.mark.asyncio


@pytest.fixture
def double() -> AsyncRedisDouble:
    return AsyncRedisDouble(decode_responses=True)


@pytest.fixture
def policy() -> ThrottlePolicy:
    return ThrottlePolicy(
        max_failures=3,
        lockout_seconds=900,
        failure_window_seconds=900,
        ip_max_failures=5,
        ip_window_seconds=300,
    )


@pytest.fixture
def throttle(double: AsyncRedisDouble, policy: ThrottlePolicy) -> LoginThrottle:
    return LoginThrottle(cast(Redis, double), policy)


# ---- 单用户名锁定 ----------------------------------------------------------


async def test_fresh_username_is_not_locked(throttle: LoginThrottle) -> None:
    assert await throttle.is_locked("alice") is False


async def test_below_threshold_not_locked(throttle: LoginThrottle) -> None:
    await throttle.record_failure("alice")
    await throttle.record_failure("alice")  # 2 < max_failures(3)
    assert await throttle.is_locked("alice") is False


async def test_reaching_threshold_locks(throttle: LoginThrottle) -> None:
    for _ in range(3):
        await throttle.record_failure("alice")
    assert await throttle.is_locked("alice") is True


async def test_lock_carries_lockout_ttl(
    throttle: LoginThrottle, double: AsyncRedisDouble
) -> None:
    for _ in range(3):
        await throttle.record_failure("alice")
    ttl = await double.ttl("login:lock:alice")
    assert 1 <= ttl <= 900


async def test_failure_counter_carries_window_ttl(
    throttle: LoginThrottle, double: AsyncRedisDouble
) -> None:
    """失败计数首次累加即带滑窗 TTL(窗口内无新失败则计数自然过期)。"""
    await throttle.record_failure("alice")
    ttl = await double.ttl("login:fail:alice")
    assert 1 <= ttl <= 900


async def test_reset_clears_count_and_lock(
    throttle: LoginThrottle, double: AsyncRedisDouble
) -> None:
    for _ in range(3):
        await throttle.record_failure("alice")
    assert await throttle.is_locked("alice") is True
    await throttle.reset("alice")
    assert await throttle.is_locked("alice") is False
    assert await double.exists("login:fail:alice") == 0


async def test_reset_on_fresh_username_is_noop(throttle: LoginThrottle) -> None:
    await throttle.reset("never-failed")  # 不抛


# ---- 单 IP 滑窗限流 --------------------------------------------------------


async def test_fresh_ip_is_not_limited(throttle: LoginThrottle) -> None:
    assert await throttle.is_ip_limited("10.0.0.1") is False


async def test_ip_below_limit_not_limited(throttle: LoginThrottle) -> None:
    for _ in range(4):  # 4 < ip_max_failures(5)
        await throttle.record_failure("u", ip="10.0.0.1")
    assert await throttle.is_ip_limited("10.0.0.1") is False


async def test_ip_reaching_limit_is_limited(throttle: LoginThrottle) -> None:
    for i in range(5):
        await throttle.record_failure(f"user{i}", ip="10.0.0.1")
    assert await throttle.is_ip_limited("10.0.0.1") is True


async def test_ip_counter_carries_window_ttl(
    throttle: LoginThrottle, double: AsyncRedisDouble
) -> None:
    await throttle.record_failure("u", ip="10.0.0.1")
    ttl = await double.ttl("login:ip:10.0.0.1")
    assert 1 <= ttl <= 300


async def test_ip_limit_independent_of_username_lock(throttle: LoginThrottle) -> None:
    """IP 限流跨用户名累计:5 个不同用户名各失败一次即触发 IP 闸,但无单账户锁。"""
    for i in range(5):
        await throttle.record_failure(f"user{i}", ip="10.0.0.9")
    assert await throttle.is_ip_limited("10.0.0.9") is True
    assert await throttle.is_locked("user0") is False  # 每个仅 1 次,未达账户锁阈值


async def test_record_failure_without_ip_skips_ip_counter(
    throttle: LoginThrottle, double: AsyncRedisDouble
) -> None:
    await throttle.record_failure("alice")  # ip=None
    assert await double.exists("login:ip:None") == 0


async def test_ip_limited_tolerates_corrupt_counter(
    throttle: LoginThrottle, double: AsyncRedisDouble
) -> None:
    """IP 计数被写脏(非整数)时降级为「未超限」,不抛。"""
    await double.set("login:ip:10.0.0.2", "not-an-int")
    assert await throttle.is_ip_limited("10.0.0.2") is False
