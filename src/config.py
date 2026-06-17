from functools import lru_cache

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="JANUS_",
        extra="ignore",
    )

    app_name: str = "Janus Server"
    environment: str = "local"
    debug: bool = False
    api_prefix: str = ""

    database_url: str = "postgresql+asyncpg://janus:janus@localhost:5432/janus"
    database_echo: bool = False
    redis_url: str = "redis://localhost:6379/0"  # 业务缓存 / 实时配额计数（db 0）
    redis_arq_url: str = "redis://localhost:6379/1"  # ARQ 任务队列（db 1，与业务缓存隔离）

    jwt_public_key: str | None = None
    jwt_jwks_url: str | None = None
    fernet_key: SecretStr | None = None

    # 平台自签 JWT（本地账密登录换发）。与上面 jwt_public_key/jwt_jwks_url 正交：
    # 那两个是给 M6 企微外部 JWT 验签预留的,本组是平台自己作为签发方用的。
    # RS256:签发权(私钥,.env 密钥)与热路径验签(公钥)分离——副本只需公钥即可验签,
    # 不必持有签发密钥(对齐'无状态副本,公钥验签'的横向扩前提)。
    platform_jwt_private_key: SecretStr | None = None  # PKCS8 PEM;仅签发方(登录)需要
    platform_jwt_public_key: str | None = None  # 缺省时启动期从私钥推导
    platform_jwt_algorithm: str = "RS256"  # 解码时显式限定,不信任 token header 的 alg
    platform_access_token_ttl_seconds: int = 7200  # access token 有效期(2h)

    default_locale: str = "zh-CN"
    supported_locales: tuple[str, ...] = ("zh-CN", "en-US")

    cors_allow_origins: list[str] = Field(default_factory=list)

    log_level: str = "INFO"  # 根 logger 级别（DEBUG/INFO/WARNING/ERROR）
    log_json: bool = True  # True=JSON 行（生产/采集）；False=彩色控制台（本地开发）


@lru_cache
def get_settings() -> Settings:
    return Settings()
