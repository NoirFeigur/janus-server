# Janus Server

> 公司统一内部 AI 基础设施平台 —— 服务端。
>
> 一个项目同时提供两类 AI 能力：**① LLM 网关**（多协议矩阵 / 路由 / 配额 / 记账）与 **② MCP 服务器**（平台自身即标准 MCP server，工具是平台内代码）。统一身份（企微 JWT + sk-key）、统一管理后台 API（用户 / 账单 / 统计）。

双面神 Janus —— 一面朝客户端（员工 / Hermes / Claude Code / OA），一面朝上游厂商。本仓库是后端；管理后台前端在 [`janus-web`](../janus-web)。

---

## 定位与边界

- **自建，不 fork 任何开源网关**：不要 one-api，不要 LiteLLM Proxy。`import litellm` **当库用**（仅做协议翻译 + Router 韧性），**绝不 fork / vendor**。
- **核心是窄腰，能力在边缘**：MCP 工具 = 平台内代码，内部对接第三方 REST API；不聚合外部 MCP。
- **MCP 零新表**：MCP 调用复用账户/key 鉴权，工具是代码不是数据；MCP 调用在平台侧不留痕（下游第三方是业务真相源）。
- **账单是内部成本记账**：`usage_record` 流水按内部计价系数算成本点，**不接 Stripe 等支付**——这是内部用量核算，不是对外计费。
- **术语**：统一用「客户端协议 / 上游协议」，不用「北向 / 南向」。
- **规模**：约 2000 员工，峰值并发约 500（I/O bound，asyncio 主场）。

---

## 技术栈

| 层 | 选型 | 说明 |
|---|---|---|
| 语言 / 运行时 | **Python 3.11** | — |
| Web 框架 | **FastAPI + Uvicorn** | 全异步；对标 LiteLLM Proxy 业内标准 |
| LLM 协议翻译 | **`litellm`（当库 import）** | 仅协议翻译 + `litellm.Router` 韧性；按 `model` 前缀自动路由；**不写转换层** |
| MCP 服务器 | **官方 `mcp` Python SDK 的 `FastMCP`** | `stateless_http=True`；三 primitive 全实现（Tools / Resources / Prompts）；Streamable HTTP |
| ORM | **SQLAlchemy 2.0（async）** | `Mapped[]` 声明式；配 **asyncpg** 驱动 |
| 校验 / 序列化 | **Pydantic v2** | 出入参 schema |
| 配置 | **pydantic-settings** | 行为配置 + 密钥分离 |
| 迁移 | **Alembic** | 数据库版本化 |
| 数据库 | **PostgreSQL（共享实例）** | 账户 / key / 模型分配 / 用量；本项目不部署 DB，连共享实例 |
| 缓存 / 协调 | **Redis（共享实例）** | 实时配额计数 + 配置短 TTL 缓存 + 跨副本发布订阅 |
| 鉴权 | **企微 JWT + `sk-xxxx` 静态 key** | 同一 token 通吃 LLM 与 MCP；JWT 验签无状态（只需公钥） |
| 包管理 | **uv** | `uv.lock` 提交入库（锁版本 + 哈希） |
| Lint / 格式化 | **Ruff** | lint + format 一把梭 |
| 类型检查 | **mypy（strict）** | CI 门禁 |
| 测试 | **pytest + pytest-asyncio + httpx** | 异步测试 + ASGI 测试客户端 |
| 定时 / 后台任务 | **ARQ**（Redis 后端） | async 原生，复用已有 Redis；scheduler 单实例 + worker 可 N 个。Celery 为可替换的重型方案 |
| 编排 | **Docker Compose** | 不上 k8s（该规模过度工程） |
| 反向代理 | **nginx** | 负载均衡 + TLS 终结 + SSE 流式 |

---

## 架构要点

### 模块化单体（默认部署 = N 个全功能副本）

一份代码，按领域划清模块边界。默认每个副本都是全功能（auth + admin + gateway + mcp），nginx 做 HA / 负载均衡。规模拐点后可**按角色拆进程**（auth+admin / llm-gateway / mcp），同一份代码换启动参数即可，无需推倒重写。

```
nginx（反向代理 + 负载均衡 + TLS）
  ├─ 副本 1：全功能（auth + admin + gateway + mcp）
  ├─ 副本 2：全功能（一模一样）
  └─ 副本 N：按并发增减
旁路进程（不在 nginx upstream 内）：
  ├─ ARQ scheduler：定时触发器，**必须单实例**（多副本会重复触发）
  └─ ARQ worker：消费任务队列，**可 N 个**（无状态，水平扩展）
共享后端（现成实例，本项目不部署）：
  ├─ PostgreSQL
  └─ Redis（兼作 ARQ 任务队列）
```

### 定时/后台任务（ARQ）

重活与周期活从请求热路径剥离，进 ARQ：夜间用量汇总、周期配额重置、账单滚动等。任务函数住在各领域的 `tasks.py`（贴近业务），由 `src/tasks/` 统一注册 + 调度。

- **scheduler 单实例铁律**：cron 触发器只能跑一个进程，多开会让每个周期任务执行 N 次。worker 无此约束，按吞吐加副本。
- **任务幂等**：worker 可能重试，任务函数按「可安全重跑」写（用 upsert / 状态机，不靠「只执行一次」假设）。

### 无状态副本

副本不在内存存会话态——JWT 验签无状态、配额计数在 Redis、持久数据在 Postgres。**无需会话粘滞**，加副本 = nginx upstream 加一行。

### SSE 流式必配

LLM 响应是流式，nginx 必须 `proxy_buffering off` + `proxy_read_timeout 600s`，否则首字延迟爆炸。

---

## 目录结构

**领域驱动布局**（参考 [zhanymkanov/fastapi-best-practices](https://github.com/zhanymkanov/fastapi-best-practices)、Netflix Dispatch；网关分协议入口对齐 LiteLLM 的 provider 分组）。每个领域是一个包，自带 `router / schemas / service / dependencies / constants / exceptions`。

> **关键取舍：ORM 模型集中，不按领域散放。** 我们的数据模型是**一张高内聚的 ERD**（雪花主键基类、三类 Entity 基类、`users`/`api_key`/`logical_model`/`usage_record`/`quota` 间交叉外键密布，枚举强制集中在 `enums.py`）。集中放 `db/models/` 让 Alembic autogenerate 扫一处即可、跨聚合关系一目了然。**按域散放也能跑**（SQLAlchemy 用字符串形式的关系引用即可避免 import 顺序问题），是另一种合法流派；这里选集中，是与「数据模型逐表敲定、全局横切约定」的设计取向一致，并非唯一正解。

```
janus-server/
├── alembic/
│   ├── versions/                 # 自动生成的迁移脚本
│   └── env.py
├── docker/
│   ├── Dockerfile
│   ├── compose.yaml              # 开发：app + arq-worker + arq-scheduler（共享 PG/Redis 连现成实例）
│   └── nginx/                    # nginx 配置（SSE 流式 + upstream）
├── src/
│   ├── __init__.py
│   ├── main.py                   # FastAPI app factory + lifespan（挂载各域 router + MCP session_manager）
│   ├── config.py                 # pydantic-settings（行为配置走 config / settings，密钥走 .env）
│   ├── enums.py                  # 全局 StrEnum 集中（ActiveStatus/UsageStatus/ErrorCode… 成员强制行内注释）
│   ├── exceptions.py             # 全局异常 + 统一错误信封 handler（方案 B：与成功响应同构的扁平信封，纯 code+params，见 responses.py）
│   │
│   ├── db/                       # 持久层（ORM 集中 + 基础仓储）
│   │   ├── __init__.py
│   │   ├── base.py               # DeclarativeBase + 三类 Entity 基类（雪花主键/软删/审计列）
│   │   ├── session.py            # AsyncEngine + async_sessionmaker + get_session() 依赖
│   │   ├── repository.py         # BaseRepository[Model]（通用 get/list/create/update/soft_delete）
│   │   └── models/               # SQLAlchemy 2.0 模型（按聚合分文件，同一声明基下）
│   │       ├── __init__.py       # 汇总导出（Alembic autogenerate 扫这里）
│   │       ├── identity.py       # users / role / department / 关联表
│   │       ├── credential.py     # api_key
│   │       ├── model_catalog.py  # logical_model / model_deployment / channel
│   │       ├── grant.py          # user_model_grant（模型分配）
│   │       ├── usage.py          # usage_record（含 downgraded_features）
│   │       └── quota.py          # quota 配置
│   │
│   ├── core/                     # 跨领域基础设施（非业务）
│   │   ├── __init__.py
│   │   ├── security.py           # JWT 验签 / sk-key 校验 / 哈希
│   │   ├── redis.py              # Redis 连接 + 发布订阅 + 配额计数原语
│   │   ├── pagination.py         # 通用分页 schema
│   │   └── i18n/                 # locale 解析 middleware + ContextVar + I18n 类
│   │       ├── __init__.py
│   │       ├── context.py        # ContextVar[locale]（请求级隔离）
│   │       └── middleware.py     # query→cookie→header→user→default 解析链
│   │
│   ├── auth/                     # 领域：身份与鉴权
│   │   ├── __init__.py
│   │   ├── router.py             # 登录 / token 校验 / 当前用户
│   │   ├── schemas.py
│   │   ├── service.py            # 企微 JWT 校验 / sk-key 解析 / RBAC 判定（编排 repository）
│   │   ├── repository.py         # 用户/角色/key 查询（继承 BaseRepository）
│   │   ├── dependencies.py       # CurrentUser / RequireRole / AuthenticatedApiKey
│   │   ├── constants.py
│   │   └── exceptions.py
│   │
│   ├── gateway/                  # 领域：LLM 网关（热路径）
│   │   ├── __init__.py
│   │   ├── router.py             # 聚合 provider 路由
│   │   ├── schemas.py
│   │   ├── service.py            # 路由 / fallback / 配额扣减 / 记账落 usage_record
│   │   ├── repository.py         # 鉴权+授权+用量的读写（跨聚合查询落点）
│   │   ├── dependencies.py       # RateLimitGated / QuotaGated
│   │   ├── constants.py
│   │   ├── exceptions.py
│   │   ├── tasks.py              # 定时任务：用量滚动汇总等（被 src/tasks 调度引用）
│   │   ├── router_factory.py     # litellm.Router 装配（号池→deployment）+ 韧性配置
│   │   └── providers/            # 客户端协议原生入口（对齐 LiteLLM provider 分组）
│   │       ├── __init__.py
│   │       ├── _base.py          # 共享 provider 入口接口
│   │       ├── openai.py         # /v1/chat/completions、/v1/embeddings
│   │       ├── anthropic.py      # /v1/messages
│   │       └── google.py         # /v1beta/models/{model}:generateContent
│   │
│   ├── mcp/                      # 领域：MCP 服务器（FastMCP，零新表）
│   │   ├── __init__.py
│   │   ├── server.py             # FastMCP 实例（stateless_http=True）+ TokenVerifier
│   │   ├── service.py            # 工具解析 / 身份透传（X-Forwarded-User）
│   │   ├── constants.py
│   │   ├── exceptions.py
│   │   └── tools/                # MCP 工具实现（平台内代码，内部调第三方 REST）
│   │       ├── __init__.py
│   │       ├── _base.py
│   │       └── <domain>_tool.py
│   │
│   ├── admin/                    # 领域：管理后台 API（资源子包）
│   │   ├── __init__.py
│   │   ├── router.py             # 挂载各资源子 router
│   │   ├── schemas.py            # 共享 admin schema
│   │   ├── dependencies.py       # CurrentAdmin / 资源校验
│   │   ├── constants.py
│   │   ├── exceptions.py
│   │   ├── users/                # 资源：用户/角色/部门（RBAC）
│   │   │   ├── __init__.py
│   │   │   ├── router.py
│   │   │   ├── schemas.py
│   │   │   ├── service.py
│   │   │   ├── repository.py
│   │   │   └── dependencies.py
│   │   ├── credentials/          # 资源：api_key 管理
│   │   ├── catalog/              # 资源：逻辑模型 / 渠道 / 号池
│   │   ├── grants/               # 资源：模型分配（user_model_grant）
│   │   ├── usage/                # 资源：用量 / 内部账单（读 usage_record 聚合）
│   │   │                         #   含 tasks.py：夜间用量汇总 / 账单滚动
│   │   └── quota/                # 资源：配额配置（含 tasks.py：周期配额重置）
│   │
│   └── tasks/                    # ARQ 定时/后台任务（调度 + worker 入口）
│       ├── __init__.py
│       ├── worker.py             # ARQ WorkerSettings + 进程入口（消费队列）
│       ├── schedule.py           # cron 调度表（引用各领域 tasks.py 的任务函数）
│       └── registry.py           # 任务函数注册（领域 tasks.py 在此汇总）
│
├── tests/                        # 镜像 src/ 结构
│   ├── conftest.py               # 临时 DB/Redis fixture（不碰共享实例）
│   ├── auth/
│   ├── gateway/
│   │   └── test_providers/
│   ├── mcp/
│   ├── admin/
│   │   ├── test_users/
│   │   └── test_usage/
│   └── tasks/                    # 定时/后台任务函数测试（幂等性/重跑安全）
│
├── .env.example                  # 密钥模板（仅密钥）
├── pyproject.toml                # uv 项目配置 + ruff/mypy/pytest 配置
├── uv.lock                       # 入库
└── README.md
```

### 每个领域包的文件契约

| 文件 | 职责 | 是否必有 |
|---|---|---|
| `__init__.py` | 包标记；向上暴露 `router` | ✅ |
| `router.py` | `APIRouter` + 端点定义；薄，只编排 | ✅ |
| `schemas.py` | Pydantic v2 出入参（API 契约，≠ ORM 模型） | ✅ |
| `service.py` | 业务逻辑；编排 `repository` + 跨域协作，不直接写 SQL | ✅ |
| `repository.py` | 数据访问；继承 `BaseRepository`，封装本域的查询/写入 | ✅ |
| `dependencies.py` | FastAPI `Depends()` 工厂（鉴权 / 校验 / 资源查找） | ✅ |
| `tasks.py` | 本域的定时/后台任务函数（被 `src/tasks` 调度引用） | 按需 |
| `constants.py` | 模块内常量 | 按需 |
| `exceptions.py` | 领域异常（继承全局基类） | 按需 |

> ORM 模型**不在领域包内**，统一在 `src/db/models/`；枚举统一在 `src/enums.py`。领域 `repository.py` import 这两处，`service.py` 只依赖本域 `repository`。

---

## 开发规范

### 全异步（硬约束）

- **路由层 `async def` 内严禁同步阻塞调用**（如 psycopg2 同步驱动）——会卡死整个事件循环。
- DB 走 **asyncpg + SQLAlchemy async**；CPU 密集 / 不可避免的同步库用 `asyncio.to_thread()` 卸载。

### 类型安全（零容忍）

- **禁止 `# type: ignore` 抑制类型错误**；`mypy --strict` 是 CI 门禁。
- ORM 用 `Mapped[]` 注解；Pydantic 模型字段全标注。

### 分层纪律

- **四层单向依赖**：`router → service → repository → db`，依赖只向下，不反向、不跨层（router 不直接碰 repository，service 不直接写 SQL）。
- **router 薄、service 厚**：路由只做参数解析 + 调 service + 拼响应；业务逻辑全在 `service.py`。
- **repository 收口数据访问**：所有 ORM 查询/写入封装在 `repository.py`，继承 `BaseRepository` 复用通用 CRUD；service 拿到的是领域对象，不是裸 `select()`。换底层存储只动 repository。
- **DB 访问经注入的 `AsyncSession`**：session 由 `get_session()` 依赖注入，repository 接收 session，不自建 engine/session。
- **领域间不互相 import service**：需要跨域协作时经 router 编排或事件，保持边界可移动。

### 枚举与 i18n（G16）

- **枚举集中在 `src/enums.py`**，用 `StrEnum`；**每个成员强制行内注释**说明业务语义（给后端开发者，非面向用户 label）。
- **后端 locale-agnostic 只发 code**，前端 i18n 拥有展示文本。**不建字典表、不出 `/system/dict` 接口**。
- **业务错误走统一错误信封**（方案 B，与成功响应同构的扁平结构）：后端发 `code`（`ErrorCode` 枚举值）+ 结构化 `params`，前端按 code 查 i18n 文案并插值。后端永不拼中文错误文案。沿用 RFC 9457 的机器可读 `code`+`params` 理念，但不采用 `application/problem+json` media type（保持成功/错误信封同构，前端 discriminated union 收窄干净，见 [`responses.py`](src/responses.py)）。
- 例外：Pydantic 422 校验错误、后端外发消息（邮件/IM）由后端按 `preferred_locale` 渲染本地化正文。

### 配置与密钥

- **`.env` 仅放密钥**（API key / token / 密码）；行为配置（超时 / 阈值 / 开关）走 `config.py`（pydantic-settings）。
- 新增密钥 → 同步更新 `.env.example`。

### 依赖锁定（供应链安全）

| 来源 | 写法 | 示例 |
|---|---|---|
| PyPI 包 | `>=floor,<next_major` | `"httpx>=0.28.1,<1"` |
| Git URL | 提交 SHA | `git+https://...@<40-char-sha>` |
| CI-only pip | `==exact` | `pyyaml==6.0.2` |

绝不提交无上界的裸 `>=X.Y.Z`。改依赖后跑 `uv lock` 重生成锁文件。

### 缓存一致性（实时失效）

跨副本实时需求走 Redis 发布订阅。例：「后台禁用 key，网关 1 秒内停用」→ admin 发 Redis 频道 `key.revoked`，gateway 订阅清缓存。

---

## 单元测试规范

**框架**：`pytest` + `pytest-asyncio`（异步测试）+ `httpx.AsyncClient`（ASGI 测试客户端，不起真实端口）。

### 布局与命名

- **`tests/` 镜像 `src/` 结构**：`src/gateway/service.py` 的测试在 `tests/gateway/test_service.py`，开发者一眼能定位。
- 文件 `test_*.py`，函数 `test_<行为>_<条件>_<预期>`（如 `test_quota_exceeded_returns_429`）。

### 隔离（硬约束）

- **绝不碰共享 PG/Redis 实例**：`conftest.py` 用临时数据库（每个测试 session 建 schema / 用 `testcontainers` 或本地 ephemeral PG）+ `fakeredis` 或临时 Redis DB。
- 每个测试自带干净状态：事务回滚 fixture 或 truncate，测试间零泄漏。
- 外部上游厂商调用一律 mock（不打真实 LLM API）；但**协议路由 / 降级逻辑用 mock 上游跑真实代码路径**。

### 测哪些（分层）

| 层 | 测什么 | 怎么测 |
|---|---|---|
| service | 业务逻辑分支、边界、异常 | 纯单测，注入 fake session/redis |
| router | 鉴权门禁、参数校验、状态码、统一错误信封格式 | `AsyncClient` 打 ASGI app |
| 关键路径 | 鉴权 / 配额扣减 / 协议路由 / 协议降级 | 集成测试跑真实代码路径，只 mock 最外层上游 |
| 迁移 | Alembic upgrade/downgrade 可逆 | 临时库跑 head↔base |

### 不变式 > 快照（禁 change-detector）

- **不写「快照型」测试**（如断言模型目录里有某个具体模型名、断言枚举成员数、断言 config 版本号字面量）——这类测试只在数据例行更新时炸 CI，零行为价值。
- **断言关系契约**：如「每个 deployment 都能在 Router 里找到」「downgraded_features 非空 ⟺ 响应头带 X-Gateway-Downgraded」「usage_record.cost = tokens × 单价 / 1e6」。

### 门禁（硬约束）

> **必须通过单元测试。** `uv run pytest` 全绿是合并/收工的**前置条件**，不是可选项。
> 测试红、或新增代码无测试覆盖，一律视为**功能未完成**——不允许「先合后补」。

提 PR 前（以及任何「这块做完了」的判定前）本地三道门**全部必过**：

| 门 | 命令 | 通过标准 |
|---|---|---|
| 单元测试 | `uv run pytest` | 全部通过，**0 失败 0 错误**；不得以 `-k`/skip 跳过失败用例 |
| Lint / 格式 | `uv run ruff check` | 0 告警 |
| 类型检查 | `uv run mypy src` | 0 error |

规则：

- **新增功能 / 修 bug 必带测试**：先写「能复现问题、当前会失败」的测试，再写让它通过的代码（红 → 绿）。
- **测试通过必须是「代码正确」的副产物**：禁止硬编码期望值、禁止为骗过断言加特判分支、禁止删/skip 失败测试「让 CI 绿」。
- **绝不碰共享 PG/Redis 实例**：单测用内存 SQLite（`aiosqlite`）/ `fakeredis` / 临时库；测试间状态零泄漏。
- **Windows 无 `bash` 脚本**：直接用下方 `uv run ...` 命令（仓库不提供 `run_tests.sh`，跨平台统一走 `uv run`）。

```bash
uv run pytest                          # 全量（合并前必过）
uv run pytest tests/db/ -q             # 单目录
uv run pytest tests/db/test_repository.py::test_soft_delete_excluded_from_get
uv run pytest --cov=src --cov-report=term-missing   # 覆盖率（参考线 ≥ 80%，service 层应更高）
```

---

## 提交规范

遵循 **[Conventional Commits](https://www.conventionalcommits.org/)**。格式：

```
<type>(<scope>): <subject>

<body 可选：为什么这么改，不只是改了什么>

<footer 可选：BREAKING CHANGE / 关联 issue>
```

### type（必填）

| type | 用途 |
|---|---|
| `feat` | 新功能 |
| `fix` | 修 bug |
| `refactor` | 重构（不改外部行为） |
| `perf` | 性能优化 |
| `test` | 加/改测试 |
| `docs` | 文档 |
| `build` | 构建 / 依赖 / Docker |
| `ci` | CI 配置 |
| `chore` | 杂项（不入 changelog） |

### scope（建议填，对齐领域）

`auth` / `gateway` / `mcp` / `admin` / `db` / `core` / `quota` / `i18n` / `deps`。

### 约定

- **subject 用中文，祈使句、≤ 50 字、不加句号**：如 `feat(gateway): 支持 Anthropic /v1/messages 原生入口`。
- **原子提交**：一个提交只干一件事；重构与功能分开提交。
- **body 说「为什么」**：复杂改动写动机/取舍，让 reviewer 不用猜。
- **破坏性变更**：footer 写 `BREAKING CHANGE: <说明>`，或 type 后加 `!`（如 `feat(db)!: ...`）。
- **绝不提交密钥**：`.env`、凭据文件已被 `.gitignore`；提交前 `git diff --staged` 自查。
- 仅在明确需要时提交；不擅自 `--amend` 已推送提交、不 force push。

示例：

```
feat(gateway): 接入 litellm.Router 做号池→deployment 路由

按 model 前缀路由到逻辑模型，号池内 key 摊平为 Router deployment；
失败自动 fallback 到同逻辑模型的其他渠道。不写协议转换层，全交 litellm。

Refs: 架构稿 G10/G12
```

---

## 快速开始

```bash
# 1. 安装 uv（若未装）
#    Windows: powershell -c "irm https://astral.sh/uv/install.ps1 | iex"
#    Unix:    curl -LsSf https://astral.sh/uv/install.sh | sh

# 2. 建虚拟环境 + 装依赖
uv venv .venv --python 3.11
# Windows: .venv\Scripts\activate    Unix: source .venv/bin/activate
uv pip install -e ".[dev]"

# 3. 配置密钥
cp .env.example .env   # 填入企微凭据 / 上游厂商 key / DB / Redis 连接

# 4. 跑迁移
uv run alembic upgrade head

# 5. 启动（开发）
uv run uvicorn src.main:app --reload --port 8000

# 5b. 定时/后台任务（按需，另开终端）
uv run arq src.tasks.worker.WorkerSettings        # worker：消费队列（可多开）
uv run arq src.tasks.worker.WorkerSettings --check # 健康检查
#   scheduler 触发器随 worker 配置启动；生产环境务必只跑一个 scheduler 实例

# 质量门
uv run pytest && uv run ruff check && uv run mypy src
```

容器化：`docker compose -f docker/compose.yaml up`。

---

## 相关文档

- 架构决策：`统一AI网关后台-架构决策.md`（G1–G16）
- 核心数据模型：`统一AI网关后台-数据模型设计.md`
- 前端仓库：[`janus-web`](../janus-web)
