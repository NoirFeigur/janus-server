"""Tests for security primitives (src/core/security.py).

Cover the three primitive families: argon2 password hash/verify, sk-key
generation + sha256 hashing, and RS256 platform-JWT issue/decode. JWT tests
inject a throwaway RSA keypair into the cached Settings via monkeypatch (auto
-restored per test) so no real key material is needed.
"""

from __future__ import annotations

import time
from collections.abc import Iterator

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from pydantic import SecretStr

from src.config import Settings, get_settings
from src.core.security import (
    PlatformAccessClaims,
    TokenError,
    decode_access_token,
    generate_api_key,
    hash_api_key,
    hash_password,
    issue_access_token,
    verify_password,
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
    token, ttl = issue_access_token(account_id=12345)
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
        issue_access_token(account_id=1)


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
    token, _ = issue_access_token(account_id=1)
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
