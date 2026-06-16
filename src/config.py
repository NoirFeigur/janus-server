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

    default_locale: str = "zh-CN"
    supported_locales: tuple[str, ...] = ("zh-CN", "en-US")

    cors_allow_origins: list[str] = Field(default_factory=list)


@lru_cache
def get_settings() -> Settings:
    return Settings()
