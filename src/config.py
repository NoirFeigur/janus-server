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
    # Redis 网络超时。默认 redis-py 无 socket 超时——一旦 Redis 卡死或网络黑洞,调用会
    # 无限挂起,拖垮整个事件循环(鉴权热路径、配额计数都走 Redis)。给 connect 与读写都
    # 设上限,失败快速上抛由调用方降级,胜过无声悬挂。健康探针另有更短的整体超时(见下)。
    redis_socket_connect_timeout_seconds: float = 2.0  # 建连超时;Redis 不可达时快速失败
    redis_socket_timeout_seconds: float = 2.0  # 读写超时;单次命令阻塞上限

    # 对象存储网络超时(MinIO/S3)。同理,aioboto3 默认超时偏长(connect 60s/read 60s),
    # 上传/预签名走请求热路径,后端卡在挂死的存储上会耗尽 worker。给 connect/read 设务实
    # 上限,并禁用 botocore 自带重试(让上层显式决定,不在底层静默放大延迟)。
    oss_connect_timeout_seconds: float = 5.0  # 建连超时
    oss_read_timeout_seconds: float = 10.0  # 读写超时(上传大附件留余量)

    # 健康探针整体超时。readiness 逐个探测 PG + Redis;即便单项已有 socket 超时,也用一个
    # 更短的整体闸把每项包起来,确保 /health/ready 永不超过此时长返回(LB 探测有自己的
    # deadline,探针自身绝不能成为悬挂点)。
    health_probe_timeout_seconds: float = 3.0  # 单项依赖探测的硬上限;超时即判该项 down

    # Snowflake worker-id 租约(data-model §0.2)。多副本部署时每个副本必须持有唯一的
    # 10-bit worker-id(0..1023),否则主键会跨副本撞车。启动时从 Redis 原子租约一个空闲
    # id,后台心跳续租;拿不到则 fail-fast(生产),local 环境回落 0(单进程开发)。
    # TTL 须 > 心跳间隔(心跳 = TTL/3),副本崩溃后 TTL 到期自动释放该 id 供复用。
    snowflake_worker_id_ttl_seconds: int = 30  # worker-id 租约 TTL(秒);崩溃后此时长内自动回收

    # 平台自签 JWT(本地账密登录换发)。RS256:签发权(私钥,.env 密钥)与热路径
    # 验签(公钥)分离——副本只需公钥即可验签,不必持有签发密钥(对齐'无状态副本,
    # 公钥验签'的横向扩前提)。算法在 core/security.py 硬锁,不走配置。
    platform_jwt_private_key: SecretStr | None = None  # PKCS8 PEM;仅签发方(登录)需要
    platform_jwt_public_key: str | None = None  # 缺省时启动期从私钥推导
    platform_access_token_ttl_seconds: int = 7200  # access token 有效期(2h)
    platform_refresh_token_ttl_seconds: int = 1_209_600  # refresh token 有效期(14d)

    # 鉴权热路径权限缓存(每请求的 RBAC 聚合提速,见 auth/perm_cache.py)。正确性靠
    # 「分层 generation 版本化 key + commit 后失效」保证,与 TTL 无关;此 TTL 仅为兜底:
    # 万一某次失效 INCR 静默失败(Redis 写抖动),陈旧权限至多存活这一窗口后随快照过期自愈。
    # 取值短(60s)以收窄该极端窗口——权限是安全敏感数据,宁可短 TTL 多查库也不留长陈旧窗。
    permission_cache_ttl_seconds: int = 60  # 权限快照 TTL(秒);失效机制之外的兜底窗口

    # 登录防爆破(B6,纯 Redis 计数,无新表)。按「用户名」累计失败,达阈值即锁定一段时间;
    # 锁定期内即便密码正确也拒绝(发 auth.account_locked),登录成功则清零计数。另设「按 IP」
    # 滑窗计数作为粗粒度限流(同一来源短时间海量尝试),防止枚举大量用户名绕开单账户锁。
    login_max_failures: int = 5  # 单用户名连续失败上限,达到即锁定
    login_lockout_seconds: int = 900  # 锁定时长(15min);锁定期内拒绝登录
    login_failure_window_seconds: int = 900  # 失败计数滑窗(15min);窗口内无新失败则计数自然过期
    login_ip_max_failures: int = 50  # 单 IP 滑窗内失败上限(粗粒度限流,防跨用户名枚举)
    login_ip_window_seconds: int = 300  # 单 IP 失败计数滑窗(5min)

    # 反代信任跳数。副本部署在 nginx 后面,request.client.host 拿到的是代理 IP——直接拿它
    # 做按-IP 限流会把所有人归一桶。此值 = 我方可信代理层数(nginx=1),按 X-Forwarded-For
    # 从右往左数第 N 跳取真实客户端 IP。默认 0(不信任 XFF,直连场景);**生产置 1**。
    # 仅取信任跳数内的条目,杜绝客户端伪造 XFF 头绕过限流(伪造的条目落在信任边界左侧被丢弃)。
    trusted_proxy_count: int = 0  # 我方可信反代层数;0=不信任 X-Forwarded-For

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

    # 上游厂商 key 的可逆加密密钥(channel_key.api_key_encrypted 用)。Fernet 对称密钥,
    # 32 字节 url-safe base64(`Fernet.generate_key()` 生成)。是密钥 → 走 .env;缺省为 None,
    # 此时加解密 accessor 在被调用时 fail-fast(网关装配 Router 解密上游 key 时才需要,
    # auth/admin 纯管理面不触发)。轮换:支持以逗号分隔多 key,第一个用于加密,其余仅解密
    # (滚动轮换:先加新 key 到队首重新加密,再下线旧 key)。
    channel_encryption_keys: SecretStr | None = None  # Fernet key(s),逗号分隔;队首加密

    cors_allow_origins: list[str] = Field(default_factory=list)

    log_level: str = "INFO"  # 根 logger 级别（DEBUG/INFO/WARNING/ERROR）
    log_json: bool = True  # True=JSON 行（生产/采集）；False=彩色控制台（本地开发）


@lru_cache
def get_settings() -> Settings:
    return Settings()


class ConfigError(RuntimeError):
    """Startup configuration is unsafe for the target environment (fail-fast)."""


def validate_runtime(settings: Settings) -> None:
    """Fail-fast on unsafe production config at startup (called from lifespan).

    ``local`` skips every check (developer convenience: in-memory keys, wildcard
    CORS, debug). Any non-local ``environment`` must satisfy every invariant below
    or the process refuses to start — far better than discovering a missing JWT
    key at first-login or a wildcard CORS hole in production. Collect ALL failures
    and raise once so an operator fixes them in a single pass, not one reboot each.
    """
    if settings.environment == "local":
        return

    problems: list[str] = []

    # JWT signing key: absence only surfaces at first token issuance otherwise
    # (a login-time 500 in prod). Demand it up front.
    if settings.platform_jwt_private_key is None:
        problems.append(
            "JANUS_PLATFORM_JWT_PRIVATE_KEY is required outside local "
            "(RS256 signing key for login)"
        )

    # debug=True leaks exception detail into error envelopes (see
    # exceptions.unhandled_exception_handler) — never in production.
    if settings.debug:
        problems.append("debug must be False outside local (leaks stack detail)")

    # Wildcard CORS with credentials is a cross-origin credential-theft hole; an
    # internal admin platform must pin explicit frontend origins.
    if "*" in settings.cors_allow_origins:
        problems.append(
            "cors_allow_origins must not contain '*' outside local "
            "(pin explicit frontend origins)"
        )

    # Behind nginx, request.client.host is the proxy IP; trusting 0 hops collapses
    # every client into one bucket for per-IP login throttling. Production sits
    # behind a reverse proxy, so the trusted hop count must be set.
    if settings.trusted_proxy_count < 1:
        problems.append(
            "trusted_proxy_count must be >= 1 outside local "
            "(replicas run behind nginx; 0 makes per-IP throttling useless)"
        )

    # The avatar/attachment upload router is always mounted, so a missing OSS
    # credential only surfaces as a 500 on the first upload (get_object_storage
    # raises ValueError when access/secret are absent). Demand both up front: an
    # operator who ships the upload feature without storage credentials should
    # learn at startup, not from a user-facing error.
    if settings.oss_access_key is None or not settings.oss_access_key.get_secret_value():
        problems.append(
            "JANUS_OSS_ACCESS_KEY is required outside local "
            "(upload endpoints 500 without object-storage credentials)"
        )
    if settings.oss_secret_key is None or not settings.oss_secret_key.get_secret_value():
        problems.append(
            "JANUS_OSS_SECRET_KEY is required outside local "
            "(upload endpoints 500 without object-storage credentials)"
        )

    if problems:
        raise ConfigError(
            "unsafe configuration for environment="
            f"{settings.environment!r}: " + "; ".join(problems)
        )
