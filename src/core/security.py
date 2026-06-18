"""安全原语:密码哈希 / sk-key 生成校验 / 平台自签 JWT。

本模块是**纯横切基础设施**——只做密码学原语,不依赖 web 层(不抛 ``AppError``、
不碰 FastAPI)。失败用本模块自有的 :class:`TokenError` 表达;由上层(service /
dependency)翻译成带 ``ErrorCode`` 的 ``AppError``。

三类原语,各按其熵选哈希(数据模型 §484 已定):
- ``sys_user.password``:低熵人造密码 → **argon2**(慢哈希,抗暴力)。
- ``api_key`` 的 sk-key:高熵随机串 → **sha256**(快哈希,可建唯一索引等值查表)。
- 平台 JWT:本地账密登录换发,**RS256**——签发权(私钥)与热路径验签(公钥)分离,
  副本只需公钥即可验签(对齐'无状态副本'横向扩前提)。
"""

from __future__ import annotations

import asyncio
import hashlib
import secrets
import time
import uuid
import weakref

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import Argon2Error, InvalidHashError
from cryptography.hazmat.primitives import serialization
from pydantic import BaseModel, ConfigDict, ValidationError

from src.config import get_settings

# ---- 密码(argon2,低熵人造密码) ---------------------------------------------

_password_hasher = PasswordHasher()

# 限制并发 argon2 操作数(见 config.argon2_max_concurrency 注释:memory-hard 哈希的
# DoS 护栏)。信号量与事件循环绑定,而测试每个用例各自起循环,故按 loop 维护一把——用
# WeakKeyDictionary 让循环结束即自动回收,绝不跨循环复用同一信号量(会 RuntimeError)。
_argon2_semaphores: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop, asyncio.Semaphore
] = weakref.WeakKeyDictionary()


def _argon2_gate() -> asyncio.Semaphore:
    """Return the argon2 concurrency semaphore bound to the running event loop."""
    loop = asyncio.get_running_loop()
    sem = _argon2_semaphores.get(loop)
    if sem is None:
        sem = asyncio.Semaphore(get_settings().argon2_max_concurrency)
        _argon2_semaphores[loop] = sem
    return sem


def hash_password(plain: str) -> str:
    """用 argon2 哈希明文密码(仅后台管理员设密码场景)。"""
    return _password_hasher.hash(plain)


def verify_password(password_hash: str, plain: str) -> bool:
    """校验明文与 argon2 哈希是否匹配。任何不匹配/坏哈希都返回 False(不抛)。

    捕获两类:``Argon2Error``(含 VerifyMismatchError,密码不匹配)与
    ``InvalidHashError``(哈希串本身畸形——它是 ValueError 子类,不在 Argon2Error 下)。
    """
    try:
        return _password_hasher.verify(password_hash, plain)
    except (Argon2Error, InvalidHashError):
        return False


async def hash_password_async(plain: str) -> str:
    """``hash_password`` 的异步包装:argon2 是故意慢的 CPU 密集哈希,直接在事件循环里
    跑会阻塞整个进程的所有并发请求。丢到线程池跑,让循环在哈希期间继续服务他人;并发数
    受 :func:`_argon2_gate` 上限约束(防洪峰打爆内存/CPU)。"""
    async with _argon2_gate():
        return await asyncio.to_thread(hash_password, plain)


async def verify_password_async(password_hash: str, plain: str) -> bool:
    """``verify_password`` 的异步包装(同 ``hash_password_async`` 的事件循环 + 并发考量)。"""
    async with _argon2_gate():
        return await asyncio.to_thread(verify_password, password_hash, plain)


def password_strength_violations(plain: str, *, min_length: int) -> list[str]:
    """检查密码强度,返回违规项列表(空列表 = 合格)。

    纯横切原语:只判定、返回机器可读的违规标签,**不抛 ``AppError``**(由上层 service
    翻译成带 ``ErrorCode`` 的响应)。基线规则(见 config 注释):长度下限 + 至少含一个
    字母与一个数字(挡纯数字/纯字母弱口令);不强推大小写/符号复杂度(对齐 NIST 取向)。
    违规标签随 ``AppError.params`` 透传给前端做 i18n 插值。
    """
    violations: list[str] = []
    if len(plain) < min_length:
        violations.append("too_short")
    if not any(c.isalpha() for c in plain):
        violations.append("no_letter")
    if not any(c.isdigit() for c in plain):
        violations.append("no_digit")
    return violations


# ---- sk-key(高熵随机串,sha256 快哈希) -------------------------------------

_SK_PREFIX = "sk-"
_SK_PREFIX_STORE_LEN = 8  # 存储用前缀长度(如 "sk-a1b2"),列表脱敏展示,非敏感。


def generate_api_key() -> tuple[str, str, str]:
    """生成一把 sk-key,返回 (明文, sha256 哈希, 存储前缀)。

    明文仅创建时返回一次(展示给用户),DB 只存哈希 + 前缀(§522 安全硬要求)。
    """
    plaintext = _SK_PREFIX + secrets.token_urlsafe(32)
    return plaintext, hash_api_key(plaintext), plaintext[:_SK_PREFIX_STORE_LEN]


def hash_api_key(plaintext: str) -> str:
    """对 sk-key 明文取 sha256 十六进制摘要(用于 DB 等值查表)。"""
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


# ---- 平台 JWT(RS256) -------------------------------------------------------

# 算法在代码里**硬锁** RS256,绝不从配置/token header 读取——防算法混淆攻击
# (如攻击者把 alg 改成 HS256 用公钥当 HMAC 密钥伪造)。签名与验签一律用本常量。
_PLATFORM_JWT_ALGORITHM = "RS256"


class TokenError(Exception):
    """JWT 签发/解码失败(缺密钥、过期、签名无效、claims 不合法等)。"""


class PlatformAccessClaims(BaseModel):
    """平台 access token 的 claims 契约(M1 最小集)。

    刻意不含 username/roles/perms/department/企微 claims:权限每请求从 DB 取(角色变更
    立即生效,不等 token 过期);企微 claims 契约是 M6 独立的事。``extra="forbid"`` 锁死
    最小集——多余 claim 直接判非法,不给未来误塞敏感信息留口子。
    """

    model_config = ConfigDict(extra="forbid")

    sub: str  # sys_user.id (stringified)
    jti: str  # token 唯一 id（UUID4 hex）；会话吊销/登出按 jti 在 Redis 标记
    iat: int  # 签发时刻(unix 秒)
    exp: int  # 过期时刻(unix 秒)
    token_use: str  # 固定 "access";为 M6 加 refresh 留前向兼容判别位


def _normalize_pem(raw: str) -> str:
    r"""把 .env 单行里的转义 ``\n`` 还原成真实换行(PEM 需要多行)。"""
    return raw.replace("\\n", "\n")


def _load_private_key_pem() -> str:
    settings = get_settings()
    if settings.platform_jwt_private_key is None:
        raise TokenError("platform_jwt_private_key is not configured")
    return _normalize_pem(settings.platform_jwt_private_key.get_secret_value())


def _load_public_key_pem() -> str:
    """取验签公钥:优先用配置的公钥,缺省时从私钥推导。"""
    settings = get_settings()
    if settings.platform_jwt_public_key:
        return _normalize_pem(settings.platform_jwt_public_key)
    private_key = serialization.load_pem_private_key(
        _load_private_key_pem().encode("utf-8"), password=None
    )
    public_pem: bytes = private_key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return public_pem.decode("utf-8")


def issue_access_token(user_id: int) -> tuple[str, int, str]:
    """为用户签发 access token,返回 (token, 有效秒数, jti)。RS256 私钥签名。

    回传 ``jti`` 供会话层把该 token 注册进 Redis 白名单(登出/踢下线按 jti 吊销);
    它已是 token 内的 claim,一并返回省得调用方再解码取。
    """
    settings = get_settings()
    ttl = settings.platform_access_token_ttl_seconds
    now = int(time.time())
    jti = uuid.uuid4().hex
    claims = {
        "sub": str(user_id),
        "jti": jti,
        "iat": now,
        "exp": now + ttl,
        "token_use": "access",
    }
    try:
        token = jwt.encode(
            claims, _load_private_key_pem(), algorithm=_PLATFORM_JWT_ALGORITHM
        )
    except (jwt.PyJWTError, ValueError) as exc:
        raise TokenError(f"failed to sign access token: {exc}") from exc
    return token, ttl, jti


def issue_refresh_token() -> tuple[str, str]:
    """签发一个不透明 refresh token,返回 (明文, sha256 哈希)。

    明文(高熵随机串)仅返回给客户端,服务端只存其 sha256(Redis 等值查表);与 sk-key
    同套「高熵 → 快哈希」思路。refresh 不携带身份,身份/绑定关系存于 Redis 会话记录。
    """
    plaintext = secrets.token_urlsafe(48)
    return plaintext, hash_refresh_token(plaintext)


def hash_refresh_token(plaintext: str) -> str:
    """对 refresh token 明文取 sha256 十六进制摘要(用于 Redis 等值查表)。"""
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def decode_access_token(token: str) -> PlatformAccessClaims:
    """验签并解析 access token。失败(过期/签名错/claims 不合法)抛 TokenError。

    显式限定 ``algorithms=[RS256]``——绝不信任 token header 自报的 alg(防算法混淆)。
    """
    try:
        payload: object = jwt.decode(
            token, _load_public_key_pem(), algorithms=[_PLATFORM_JWT_ALGORITHM]
        )
    except jwt.PyJWTError as exc:
        raise TokenError(f"invalid token: {exc}") from exc
    if not isinstance(payload, dict):
        raise TokenError("token payload is not a JSON object")
    try:
        claims = PlatformAccessClaims.model_validate(payload)
    except ValidationError as exc:
        raise TokenError(f"token claims invalid: {exc}") from exc
    if claims.token_use != "access":
        raise TokenError(f"unexpected token_use: {claims.token_use!r}")
    return claims
