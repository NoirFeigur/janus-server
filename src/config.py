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
    platform_refresh_token_ttl_seconds: int = 1_209_600  # refresh token 有效期(14d)

    # 登录防爆破(B6,纯 Redis 计数,无新表)。按「用户名」累计失败,达阈值即锁定一段时间;
    # 锁定期内即便密码正确也拒绝(发 auth.account_locked),登录成功则清零计数。另设「按 IP」
    # 滑窗计数作为粗粒度限流(同一来源短时间海量尝试),防止枚举大量用户名绕开单账户锁。
    login_max_failures: int = 5  # 单用户名连续失败上限,达到即锁定
    login_lockout_seconds: int = 900  # 锁定时长(15min);锁定期内拒绝登录
    login_failure_window_seconds: int = 900  # 失败计数滑窗(15min);窗口内无新失败则计数自然过期
    login_ip_max_failures: int = 50  # 单 IP 滑窗内失败上限(粗粒度限流,防跨用户名枚举)
    login_ip_window_seconds: int = 300  # 单 IP 失败计数滑窗(5min)

    # 自助改密强度策略(B7)。规模 2000 内部员工,口径取「长度 + 字符多样性」的务实下限
    # (NIST 不强推复杂度规则,但内部基线要求至少含字母与数字,挡纯数字/纯字母弱口令)。
    password_min_length: int = 8  # 新密码最短长度

    default_locale: str = "zh-CN"
    supported_locales: tuple[str, ...] = ("zh-CN", "en-US")

    # 对象存储(MinIO,S3 兼容)。私有桶——读取一律用短期预签名 URL(后端用 access/secret
    # 本地算 HMAC 签名,零网络开销),DB 只存桶内 object key,绝不存永久 URL。endpoint/桶名/
    # 密钥都是部署相关的连接信息,走 .env(见 .env.example);这里只给本地/占位默认,绝不写生产值。
    # 换 S3/阿里云 OSS 只改 .env。
    oss_endpoint_url: str = "http://localhost:9000"  # S3 端点(.env JANUS_OSS_ENDPOINT_URL)
    oss_region: str = "us-east-1"  # MinIO 不校验区域,给个 S3 默认值占位
    oss_bucket: str = "local"  # 桶名(.env JANUS_OSS_BUCKET);头像/附件统一落此桶,按 biz_type 分前缀
    oss_access_key: SecretStr | None = None  # access key(.env JANUS_OSS_ACCESS_KEY)
    oss_secret_key: SecretStr | None = None  # secret key(.env JANUS_OSS_SECRET_KEY)
    oss_presign_ttl_seconds: int = 900  # 读预签名 URL 有效期(15min);私有桶读取凭证
    # 头像上传约束。后端代理上传 → 统一校验大小/类型并转 webp(省存储 + 转码即剥 EXIF 隐私元数据)。
    avatar_max_bytes: int = 2 * 1024 * 1024  # 头像原图大小上限(2MB);超限拒绝
    # 通用附件上传约束。后端代理上传 → 仅校验大小,原样落桶(保留原始扩展名/类型,不转码)。
    attachment_max_bytes: int = 10 * 1024 * 1024  # 通用附件大小上限(10MB);超限拒绝

    cors_allow_origins: list[str] = Field(default_factory=list)

    log_level: str = "INFO"  # 根 logger 级别（DEBUG/INFO/WARNING/ERROR）
    log_json: bool = True  # True=JSON 行（生产/采集）；False=彩色控制台（本地开发）


@lru_cache
def get_settings() -> Settings:
    return Settings()
