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
    # 连接池调优。**总连接数 = 副本数 ×(pool_size + max_overflow)**,必须 ≤ PG 的
    # max_connections(默认仅 100)。默认按「可安全跑多副本」取保守值:5+10=15/副本 →
    # 4 副本 = 60 条,仍留余量给 psql/迁移/监控。异步驱动下单副本 15 条连接即可支撑很高
    # 并发(连接按需复用,非每请求一条)。生产放大并发时,要么上调这里并同步调大 PG
    # max_connections,要么前置 PgBouncer 做连接复用——别盲目把单副本拉到 30+。
    database_pool_size: int = 5  # 稳态常驻连接数/副本
    database_max_overflow: int = 10  # 突发时额外可借(合计上限 = pool_size + 此值)
    database_pool_timeout_seconds: int = 30  # 池满时等待连接的上限,超时即抛(防堆积)
    database_pool_recycle_seconds: int = 1800  # 30min,须 < PG 空闲连接超时

    # argon2 是 memory-hard 慢哈希,每次约数十 MB。登录洪峰下并发哈希数若无上限,内存/CPU
    # 会被打爆(DoS 面)。此值给「同时在跑的 argon2 操作数」设硬上限(超出则排队等待)。
    # 8 × ~64MB ≈ 0.5GB 峰值,兼顾吞吐与内存;生产按机器内存与 QPS 调。
    argon2_max_concurrency: int = 8
    redis_url: str = "redis://localhost:6379/0"  # 业务缓存 / 实时配额计数（db 0）
    redis_arq_url: str = "redis://localhost:6379/1"  # ARQ 任务队列（db 1，与业务缓存隔离）

    # 平台自签 JWT(本地账密登录换发)。RS256:签发权(私钥,.env 密钥)与热路径
    # 验签(公钥)分离——副本只需公钥即可验签,不必持有签发密钥(对齐'无状态副本,
    # 公钥验签'的横向扩前提)。算法在 core/security.py 硬锁,不走配置。
    platform_jwt_private_key: SecretStr | None = None  # PKCS8 PEM;仅签发方(登录)需要
    platform_jwt_public_key: str | None = None  # 缺省时启动期从私钥推导
    platform_access_token_ttl_seconds: int = 7200  # access token 有效期(2h)

    default_locale: str = "zh-CN"
    supported_locales: tuple[str, ...] = ("zh-CN", "en-US")

    cors_allow_origins: list[str] = Field(default_factory=list)

    log_level: str = "INFO"  # 根 logger 级别（DEBUG/INFO/WARNING/ERROR）
    log_json: bool = True  # True=JSON 行（生产/采集）；False=彩色控制台（本地开发）


@lru_cache
def get_settings() -> Settings:
    return Settings()
