"""会话存储（src/core/session_store.py）行为测试。

直接对注入的内存 Redis double 驱动 SessionStore,断言「关系契约」而非快照:
- 建会话后 access 在册、refresh 可消费;
- 登出删 access 记录 + 移出用户索引 + 连带删绑定 refresh;
- refresh 轮换是「单赢」原子消费(并发只有一方拿到);
- 已轮换的 refresh 被重放 → 判定重放并吊销该用户全部会话;
- 改密/重放触发的全端吊销清空该用户所有会话与绑定 refresh。
"""

from __future__ import annotations

from typing import cast

import pytest
from redis.asyncio import Redis

from src.core.session_store import (
    RefreshConsumeResult,
    RefreshOutcome,
    SessionStore,
)
from tests._async_redis_double import AsyncRedisDouble

pytestmark = pytest.mark.asyncio


@pytest.fixture
def double() -> AsyncRedisDouble:
    return AsyncRedisDouble(decode_responses=True)


@pytest.fixture
def store(double: AsyncRedisDouble) -> SessionStore:
    return SessionStore(cast(Redis, double))


async def _login(
    store: SessionStore,
    *,
    user_id: int = 1,
    access_jti: str = "acc-1",
    refresh_hash: str = "ref-1",
    access_ttl: int = 7200,
    refresh_ttl: int = 1_209_600,
) -> None:
    await store.create_session(
        user_id=user_id,
        access_jti=access_jti,
        access_ttl=access_ttl,
        refresh_hash=refresh_hash,
        refresh_ttl=refresh_ttl,
    )


# ---- create_session + is_access_active ------------------------------------


async def test_created_access_is_active(store: SessionStore) -> None:
    await _login(store, access_jti="acc-1")
    assert await store.is_access_active("acc-1") is True


async def test_unknown_access_is_inactive(store: SessionStore) -> None:
    assert await store.is_access_active("never-issued") is False


async def test_created_session_indexes_jti_under_user(
    store: SessionStore, double: AsyncRedisDouble
) -> None:
    await _login(store, user_id=42, access_jti="acc-x")
    assert "acc-x" in await double.smembers("sess:user:42")


async def test_user_index_carries_refresh_ttl(
    store: SessionStore, double: AsyncRedisDouble
) -> None:
    """用户索引集合 TTL 取 refresh 寿命,不能早于成员过期。"""
    await _login(store, user_id=7, refresh_ttl=1000)
    ttl = await double.ttl("sess:user:7")
    assert 1 <= ttl <= 1000


# ---- revoke_access (登出 / 踢单会话) --------------------------------------


async def test_revoke_access_deactivates(store: SessionStore) -> None:
    await _login(store, access_jti="acc-1")
    await store.revoke_access("acc-1")
    assert await store.is_access_active("acc-1") is False


async def test_revoke_access_removes_from_user_index(
    store: SessionStore, double: AsyncRedisDouble
) -> None:
    await _login(store, user_id=5, access_jti="acc-1")
    await store.revoke_access("acc-1")
    assert "acc-1" not in await double.smembers("sess:user:5")


async def test_revoke_access_deletes_bound_refresh(
    store: SessionStore, double: AsyncRedisDouble
) -> None:
    """登出必须连带废掉同会话的 refresh,否则可凭旧 refresh 复活会话。"""
    await _login(store, access_jti="acc-1", refresh_hash="ref-1")
    await store.revoke_access("acc-1")
    assert await double.exists("sess:refresh:ref-1") == 0


async def test_revoke_access_is_idempotent(store: SessionStore) -> None:
    await _login(store, access_jti="acc-1")
    await store.revoke_access("acc-1")
    await store.revoke_access("acc-1")  # 二次无副作用
    assert await store.is_access_active("acc-1") is False


# ---- consume_refresh:正常轮换 --------------------------------------------


async def test_consume_refresh_ok_returns_rotation_info(store: SessionStore) -> None:
    await _login(store, user_id=9, access_jti="acc-1", refresh_hash="ref-1")
    result = await store.consume_refresh("ref-1", used_marker_ttl=600)
    assert result == RefreshConsumeResult(
        RefreshOutcome.ok, user_id=9, old_access_jti="acc-1"
    )


async def test_consume_refresh_consumes_token(store: SessionStore) -> None:
    """消费后该 refresh 立即失效(GETDEL 语义)。"""
    await _login(store, refresh_hash="ref-1")
    await store.consume_refresh("ref-1", used_marker_ttl=600)
    second = await store.consume_refresh("ref-1", used_marker_ttl=600)
    # 二次提交触发重放分支(used 标记还在),而非再次 ok
    assert second.outcome is RefreshOutcome.reuse_detected


async def test_concurrent_consume_single_winner(store: SessionStore) -> None:
    """并发轮换:仅一方拿到 ok,另一方落入重放检测(不得双赢)。"""
    await _login(store, refresh_hash="ref-1")
    first = await store.consume_refresh("ref-1", used_marker_ttl=600)
    second = await store.consume_refresh("ref-1", used_marker_ttl=600)
    outcomes = {first.outcome, second.outcome}
    assert RefreshOutcome.ok in outcomes
    assert outcomes == {RefreshOutcome.ok, RefreshOutcome.reuse_detected}


# ---- consume_refresh:无效 / 重放 -----------------------------------------


async def test_consume_unknown_refresh_is_invalid(store: SessionStore) -> None:
    result = await store.consume_refresh("never-existed", used_marker_ttl=600)
    assert result.outcome is RefreshOutcome.invalid
    assert result.user_id is None


async def test_reuse_detected_revokes_all_user_sessions(
    store: SessionStore, double: AsyncRedisDouble
) -> None:
    """重放已轮换的 refresh → 该用户全部会话被吊销(攻击者与合法用户同被踢)。"""
    await _login(store, user_id=3, access_jti="acc-a", refresh_hash="ref-1")
    # 同用户的第二个并行会话,验证重放会连带它一起吊销
    await store.create_session(
        user_id=3,
        access_jti="acc-b",
        access_ttl=7200,
        refresh_hash="ref-2",
        refresh_ttl=1000,
    )
    await store.consume_refresh("ref-1", used_marker_ttl=600)  # 正常轮换
    replay = await store.consume_refresh("ref-1", used_marker_ttl=600)  # 重放
    assert replay.outcome is RefreshOutcome.reuse_detected
    assert replay.user_id == 3
    assert await store.is_access_active("acc-a") is False
    assert await store.is_access_active("acc-b") is False
    assert await double.exists("sess:user:3") == 0


# ---- revoke_all_sessions (B7 改密强制下线 / 重放兜底) ----------------------


async def test_revoke_all_sessions_clears_everything(
    store: SessionStore, double: AsyncRedisDouble
) -> None:
    await _login(store, user_id=8, access_jti="acc-a", refresh_hash="ref-a")
    await store.create_session(
        user_id=8,
        access_jti="acc-b",
        access_ttl=7200,
        refresh_hash="ref-b",
        refresh_ttl=1000,
    )
    await store.revoke_all_sessions(8)
    assert await store.is_access_active("acc-a") is False
    assert await store.is_access_active("acc-b") is False
    assert await double.exists("sess:refresh:ref-a") == 0
    assert await double.exists("sess:refresh:ref-b") == 0
    assert await double.exists("sess:user:8") == 0


async def test_revoke_all_sessions_no_sessions_is_noop(store: SessionStore) -> None:
    await store.revoke_all_sessions(999)  # 不抛


# ---- 脏数据容错 ------------------------------------------------------------


async def test_corrupt_access_record_revokes_without_error(
    store: SessionStore, double: AsyncRedisDouble
) -> None:
    """access 记录被写脏(非 JSON)时,revoke 仍删主键、不抛。"""
    await double.set("sess:access:acc-corrupt", "not-json{")
    await store.revoke_access("acc-corrupt")
    assert await store.is_access_active("acc-corrupt") is False


async def test_corrupt_refresh_record_is_invalid(
    store: SessionStore, double: AsyncRedisDouble
) -> None:
    await double.set("sess:refresh:ref-corrupt", "not-json{")
    result = await store.consume_refresh("ref-corrupt", used_marker_ttl=600)
    assert result.outcome is RefreshOutcome.invalid


# ---- 在线会话枚举（B5）----------------------------------------------------


async def test_create_session_persists_metadata(store: SessionStore) -> None:
    """登录采集的 ip/ua/login_at 落 access 记录,可经 list_user_sessions 取回。"""
    await store.create_session(
        user_id=11,
        access_jti="acc-meta",
        access_ttl=7200,
        refresh_hash="ref-meta",
        refresh_ttl=1000,
        ip="10.0.0.9",
        user_agent="curl/8.0",
    )
    sessions = await store.list_user_sessions(11)
    assert len(sessions) == 1
    info = sessions[0]
    assert info.access_jti == "acc-meta"
    assert info.user_id == 11
    assert info.ip == "10.0.0.9"
    assert info.user_agent == "curl/8.0"
    assert info.login_at  # ISO 串,非空


async def test_create_session_metadata_defaults_to_none(store: SessionStore) -> None:
    """不传 ip/ua 时降级为 None(向后兼容旧调用点)。"""
    await _login(store, user_id=12, access_jti="acc-bare")
    sessions = await store.list_user_sessions(12)
    assert sessions[0].ip is None
    assert sessions[0].user_agent is None


async def test_list_user_sessions_returns_all_active(store: SessionStore) -> None:
    await _login(store, user_id=20, access_jti="acc-a", refresh_hash="ref-a")
    await store.create_session(
        user_id=20,
        access_jti="acc-b",
        access_ttl=7200,
        refresh_hash="ref-b",
        refresh_ttl=1000,
    )
    sessions = await store.list_user_sessions(20)
    assert {s.access_jti for s in sessions} == {"acc-a", "acc-b"}


async def test_list_user_sessions_prunes_expired_index_member(
    store: SessionStore, double: AsyncRedisDouble
) -> None:
    """access 记录已过期但 jti 滞留索引时,读时顺手 SREM 清理。"""
    await _login(store, user_id=21, access_jti="acc-live", refresh_hash="ref-live")
    # 模拟另一个会话的 access 记录已 TTL 过期(只删主键,jti 仍在用户索引)
    await double.sadd("sess:user:21", "acc-dead")
    sessions = await store.list_user_sessions(21)
    assert {s.access_jti for s in sessions} == {"acc-live"}
    assert "acc-dead" not in await double.smembers("sess:user:21")


async def test_list_all_sessions_spans_users(store: SessionStore) -> None:
    await _login(store, user_id=30, access_jti="acc-30", refresh_hash="ref-30")
    await _login(store, user_id=31, access_jti="acc-31", refresh_hash="ref-31")
    sessions = await store.list_all_sessions()
    assert {s.user_id for s in sessions} == {30, 31}
    assert {s.access_jti for s in sessions} == {"acc-30", "acc-31"}


async def test_create_session_registers_in_global_index(
    store: SessionStore, double: AsyncRedisDouble
) -> None:
    await _login(store, user_id=40, access_jti="acc-40")
    assert "40" in await double.smembers("sess:users")


async def test_revoke_last_session_prunes_global_index(
    store: SessionStore, double: AsyncRedisDouble
) -> None:
    """用户最后一个会话被登出后,从全局在线索引中剔除。"""
    await _login(store, user_id=41, access_jti="acc-41")
    await store.revoke_access("acc-41")
    assert "41" not in await double.smembers("sess:users")


async def test_revoke_one_of_many_keeps_global_index(
    store: SessionStore, double: AsyncRedisDouble
) -> None:
    """还有其它会话时,登出单个不应把用户移出全局索引。"""
    await _login(store, user_id=42, access_jti="acc-a", refresh_hash="ref-a")
    await store.create_session(
        user_id=42,
        access_jti="acc-b",
        access_ttl=7200,
        refresh_hash="ref-b",
        refresh_ttl=1000,
    )
    await store.revoke_access("acc-a")
    assert "42" in await double.smembers("sess:users")


async def test_revoke_all_sessions_prunes_global_index(
    store: SessionStore, double: AsyncRedisDouble
) -> None:
    await _login(store, user_id=43, access_jti="acc-43")
    await store.revoke_all_sessions(43)
    assert "43" not in await double.smembers("sess:users")


async def test_list_all_sessions_prunes_empty_user(
    store: SessionStore, double: AsyncRedisDouble
) -> None:
    """全局索引含一个已无活跃会话的用户时,枚举时顺手剔除。"""
    await double.sadd("sess:users", "999")  # 索引里有,但无任何会话记录
    sessions = await store.list_all_sessions()
    assert all(s.user_id != 999 for s in sessions)
    assert "999" not in await double.smembers("sess:users")
