"""Tests for security primitives (src/core/security.py).

Cover the three primitive families: argon2 password hash/verify, sk-key
generation + sha256 hashing, and RS256 platform-JWT issue/decode. JWT tests
inject a throwaway RSA keypair into the cached Settings via monkeypatch (auto
-restored per test) so no real key material is needed.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterator

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from pydantic import SecretStr

from src.config import Settings, get_settings
from src.core import security
from src.core.security import (
    PlatformAccessClaims,
    TokenError,
    decode_access_token,
    generate_api_key,
    hash_api_key,
    hash_password,
    hash_password_async,
    issue_access_token,
    verify_password,
    verify_password_async,
)

# ---- password (argon2) ------------------------------------------------------


def test_hash_password_then_verify_succeeds() -> None:
    h = hash_password("s3cret-pw")
    assert h != "s3cret-pw"  # 绝不明文
    assert verify_password(h, "s3cret-pw") is True


def test_verify_password_wrong_returns_false() -> None:
    h = hash_password("s3cret-pw")
    assert verify_password(h, "wrong") is False


def test_verify_password_malformed_hash_returns_false() -> None:
    """坏哈希(非 argon2 串)不抛,返回 False。"""
    assert verify_password("not-a-real-hash", "anything") is False


def test_hash_password_is_salted() -> None:
    """同一密码两次哈希应不同(盐随机)。"""
    assert hash_password("same") != hash_password("same")


# ---- password (async wrappers + concurrency gate) --------------------------


async def test_hash_password_async_roundtrip() -> None:
    """异步包装产出的哈希,同步 verify 能验过(等价于同步路径)。"""
    h = await hash_password_async("async-pw")
    assert h != "async-pw"
    assert await verify_password_async(h, "async-pw") is True
    assert await verify_password_async(h, "nope") is False


async def test_argon2_concurrency_is_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """并发 ``hash_password_async`` 同时在跑的数量不超过配置上限。

    把上限压到 2,用一个计数 + 锁记录峰值在飞数;替换 ``security.hash_password`` 为带短
    暂停的探针(在线程池里跑),并发发起 6 个,断言观察到的峰值 ≤ 2。
    """
    import threading
    import time as _time

    monkeypatch.setattr(get_settings(), "argon2_max_concurrency", 2)
    # 清掉可能已按当前 loop 建好的旧信号量,确保用上面压低的上限重建。
    security._argon2_semaphores.clear()

    lock = threading.Lock()
    state = {"current": 0, "peak": 0}

    def _probe(_plain: str) -> str:
        with lock:
            state["current"] += 1
            state["peak"] = max(state["peak"], state["current"])
        _time.sleep(0.02)  # 拉长窗口,逼并发真正叠加
        with lock:
            state["current"] -= 1
        return "h"

    monkeypatch.setattr(security, "hash_password", _probe)

    await asyncio.gather(*(hash_password_async(f"p{i}") for i in range(6)))

    assert state["peak"] <= 2, f"argon2 并发峰值 {state['peak']} 超过上限 2"
    assert state["peak"] >= 2, "探针未真正并发,测试无意义"



# ---- sk-key (sha256) --------------------------------------------------------


def test_generate_api_key_shape() -> None:
    plaintext, key_hash, prefix = generate_api_key()
    assert plaintext.startswith("sk-")
    assert len(key_hash) == 64  # sha256 hex
    assert prefix == plaintext[:8]
    assert hash_api_key(plaintext) == key_hash  # 哈希自洽


def test_generate_api_key_is_unique() -> None:
    first, _, _ = generate_api_key()
    second, _, _ = generate_api_key()
    assert first != second


def test_hash_api_key_is_deterministic() -> None:
    assert hash_api_key("sk-fixed") == hash_api_key("sk-fixed")


# ---- platform JWT (RS256) ---------------------------------------------------


@pytest.fixture
def rsa_settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[Settings]:
    """Inject a throwaway RSA private key into the cached Settings."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    priv_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode("utf-8")
    settings = get_settings()
    monkeypatch.setattr(settings, "platform_jwt_private_key", SecretStr(priv_pem))
    monkeypatch.setattr(settings, "platform_jwt_public_key", None)  # 强制从私钥推导
    monkeypatch.setattr(settings, "platform_access_token_ttl_seconds", 3600)
    yield settings


def test_issue_then_decode_roundtrip(rsa_settings: Settings) -> None:
    token, ttl = issue_access_token(user_id=12345)
    assert ttl == 3600
    claims = decode_access_token(token)
    assert isinstance(claims, PlatformAccessClaims)
    assert claims.sub == "12345"
    assert claims.token_use == "access"
    assert claims.exp - claims.iat == 3600


def test_issue_without_private_key_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "platform_jwt_private_key", None)
    with pytest.raises(TokenError):
        issue_access_token(user_id=1)


def test_decode_expired_token_raises(rsa_settings: Settings) -> None:
    """过期 token 解码应抛 TokenError。"""
    priv = rsa_settings.platform_jwt_private_key
    assert priv is not None
    now = int(time.time())
    expired = jwt.encode(
        {"sub": "1", "iat": now - 7200, "exp": now - 3600, "token_use": "access"},
        priv.get_secret_value().replace("\\n", "\n"),
        algorithm="RS256",
    )
    with pytest.raises(TokenError):
        decode_access_token(expired)


def test_decode_wrong_token_use_raises(rsa_settings: Settings) -> None:
    """token_use != 'access' 应被拒(防 M6 refresh token 被当 access 用)。"""
    priv = rsa_settings.platform_jwt_private_key
    assert priv is not None
    now = int(time.time())
    refresh_like = jwt.encode(
        {"sub": "1", "iat": now, "exp": now + 3600, "token_use": "refresh"},
        priv.get_secret_value().replace("\\n", "\n"),
        algorithm="RS256",
    )
    with pytest.raises(TokenError):
        decode_access_token(refresh_like)


def test_decode_tampered_token_raises(rsa_settings: Settings) -> None:
    token, _ = issue_access_token(user_id=1)
    tampered = token[:-4] + ("AAAA" if not token.endswith("AAAA") else "BBBB")
    with pytest.raises(TokenError):
        decode_access_token(tampered)


def test_decode_missing_claims_raises(rsa_settings: Settings) -> None:
    """缺必需 claim(如无 sub)应抛 TokenError(Pydantic 校验失败)。"""
    priv = rsa_settings.platform_jwt_private_key
    assert priv is not None
    now = int(time.time())
    incomplete = jwt.encode(
        {"iat": now, "exp": now + 3600, "token_use": "access"},  # 缺 sub
        priv.get_secret_value().replace("\\n", "\n"),
        algorithm="RS256",
    )
    with pytest.raises(TokenError):
        decode_access_token(incomplete)


def test_decode_rejects_hs256_algorithm_confusion(rsa_settings: Settings) -> None:
    """算法混淆防御:任何非 RS256 算法的 token 必须被拒。

    解码硬锁 ``algorithms=['RS256']``,绝不信任 token header 自报的 alg。
    pyjwt 2.x 会拦截"用 PEM 公钥当 HMAC 密钥"的编码,所以这里用一个
    合法签名的 HS256 token(普通字符串密钥)验证:即使签名自洽,只要
    alg 不在白名单内就必须被拒。
    """
    now = int(time.time())
    # validly-signed HS256 with a plain secret; wrong algorithm must still be rejected
    forged = jwt.encode(
        {"sub": "1", "iat": now, "exp": now + 3600, "token_use": "access"},
        "attacker-controlled-secret-padded-to-32-bytes-min",
        algorithm="HS256",
    )
    with pytest.raises(TokenError):
        decode_access_token(forged)


def test_decode_rejects_extra_claims(rsa_settings: Settings) -> None:
    """extra='forbid':多余 claim(如偷塞 perms)的 token 必须被拒。"""
    priv = rsa_settings.platform_jwt_private_key
    assert priv is not None
    now = int(time.time())
    bloated = jwt.encode(
        {
            "sub": "1",
            "iat": now,
            "exp": now + 3600,
            "token_use": "access",
            "perms": ["*:*:*"],  # smuggled extra claim
        },
        priv.get_secret_value().replace("\\n", "\n"),
        algorithm="RS256",
    )
    with pytest.raises(TokenError):
        decode_access_token(bloated)


def test_decode_uses_configured_public_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """配置了独立验签公钥时,decode 走配置公钥分支(而非从私钥推导)。"""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    priv_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode("utf-8")
    pub_pem = (
        key.public_key()
        .public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode("utf-8")
    )
    settings = get_settings()
    monkeypatch.setattr(settings, "platform_jwt_private_key", SecretStr(priv_pem))
    monkeypatch.setattr(settings, "platform_jwt_public_key", pub_pem)  # 配置公钥分支
    monkeypatch.setattr(settings, "platform_access_token_ttl_seconds", 3600)

    token, _ = issue_access_token(user_id=7)
    claims = decode_access_token(token)
    assert claims.sub == "7"


def test_issue_with_invalid_private_key_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """私钥串存在但无法解析为有效 PEM 时,签名失败必须转成 TokenError。"""
    settings = get_settings()
    monkeypatch.setattr(
        settings,
        "platform_jwt_private_key",
        SecretStr("-----BEGIN PRIVATE KEY-----\nnot-a-valid-key\n-----END PRIVATE KEY-----"),
    )
    with pytest.raises(TokenError):
        issue_access_token(user_id=1)


def test_decode_non_dict_payload_raises(
    rsa_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """防御性分支:jwt 解出的 payload 不是 JSON object 时必须拒绝。

    正常 JWT 规范下 payload 必是对象,pyjwt 也会拦截非对象 payload;这里直接
    打桩 ``jwt.decode`` 返回一个列表,验证服务层的 isinstance 守卫确实生效。
    """
    token, _ = issue_access_token(user_id=1)
    monkeypatch.setattr(
        security.jwt, "decode", lambda *_a, **_k: ["not", "a", "dict"]
    )
    with pytest.raises(TokenError):
        decode_access_token(token)
