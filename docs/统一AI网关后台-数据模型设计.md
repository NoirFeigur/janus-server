# 统一 AI 基础设施平台 — 核心数据模型设计

> 版本：v0.5.5（第〇章 + 第1批 + 第1.5批 + 第2批 + 第3批 全部定稿，含两轮 Oracle 评审 + 全文一致性审查修正 + i18n 落地 + MCP 第4批零新表 + 第5批错误码契约定稿、调用审计表砍掉 + 枚举 label 归属修正）
> 日期：2026-06-16
> 定稿说明：v0.5 经全文一致性锁前审查,修正 10 处 must-fix(枚举集中定义、3 张软删表唯一约束改部分唯一索引、quota NULLS NOT DISTINCT + CHECK、cost 公式除百万、cost 单位统一 Decimal、Redis key 补 metric/bucket、api_key scope 锁定、channel_key 列名统一、status 两态化)+ 6 处 should-fix + 全部 ★ 待定点收口。v0.5.1 补:枚举成员强制行内注释约定 + 全枚举注释补齐(枚举转义走纯 Python 规范——后端只出 code,label 由前端 i18n 维护)。v0.5.2 落 i18n(架构稿 G16/6.12):`sys_user` 加 `preferred_locale`、`sys_menu.name` 语义改 i18n key + 加 `remark` 列、第 5 批预告 `ErrorCode` 错误码目录。v0.5.3 定 MCP(架构稿 G11/四之五):平台自身即标准 MCP 服务器、工具是代码,第 4 批**无新表**——复用 `sys_user`/`api_key`(鉴权)、工具内 LLM 调用直调自有网关(复用配额/记账)、MCP 调用平台侧不留痕(下游第三方是业务真相源);删除原计划 `mcp_server`/`mcp_grant`/工具目录/第三方连接表。**v0.5.4 定第 5 批:`call_audit` 调用审计表砍掉**——`usage_record`(第3批)已覆盖审计主要价值(谁/哪把key/何时/什么模型/结果/耗时),源 IP/正文/MCP 调用是合规边际增量、非 v1 必需,将来需合规取证再加 append-only 新表(对存量零迁移);**降级标记 `downgraded_features`(G13)折入 `usage_record`**(per-call,降级率一句查询);**定义 `ErrorCode(StrEnum)`** 业务错误码目录(RFC 9457 problem+json,无新表)。**第〇~3 批锁定,第 4/5 批为零新表增量、不回改本文已定部分。** **v0.5.5 修正枚举 `label` 归属**——调研 RuoYi/jeecg(Java=DB 字典表) vs Django/DRF(Python=代码枚举 `TextChoices`,label 跟 code 走)后,因**服务端导出**需在后端拿到枚举列 label,将 label 定义源从「前端 i18n 维护」上提为「后端 `locales/{lang}/enums.json` 单源 + codegen 同步前端」;运行时 API 仍只发 code(前端 `valueEnum` 渲染),仍否决 DB 字典表(详见架构稿 6.12.1)。
> 关系：本文是《统一 AI 网关后台-架构决策》（下称**架构稿**，v1.5）第八章「开放问题 4：核心数据模型」的落地。架构稿定方向，本文定 `models.py`（SQLAlchemy ORM）+ `schemas.py`（Pydantic）的具体字段。
> 方式：**逐层讨论、逐表敲定**。先定贯穿全局的横切约定（第〇章），再按依赖顺序逐张表落。

---

## 〇、横切约定（先对齐，影响每一张表）

> 这些是「写每张表前必须先定」的全局选择，定下后所有表统一遵守。本章每条都给**推荐值 + 理由 + 待你确认**。

### 0.1 命名规范

| 项 | 约定 | 说明 |
|---|---|---|
| 表名 | `snake_case`、**单数** | SQLAlchemy 社区惯例。`account` 而非 `accounts` |
| 列名 | `snake_case` | `created_at`、`logical_model_id` |
| 外键列 | `<目标表>_id` | `channel_id` 指向 `upstream_channel.id` |
| 主键 | 统一叫 `id` | —— |

### 0.2 主键类型 —— **已定：BigInteger + 雪花算法**

主键统一 `BigInteger`，ID 由**雪花算法（Snowflake）应用层生成**（非 DB 自增）。

- ID 64 位落进 `BigInteger`；`autoincrement=False`，由应用在 insert 前赋值。
- **收益**：ID 应用层预生成（insert 前即知 id，利于一次事务内跨表引用）；大致按时间有序（B-tree 局部性优于随机 UUID）。
- **代价（必须解决）：worker-id 协调**。雪花 ID 含一段 worker-id（机器号），要求每个生成进程持唯一 worker-id，否则同毫秒可能撞 ID。本平台是 N 个无状态副本（架构稿 G7），`--scale=N` 起的副本环境相同，无法配置写死。

**worker-id 分配方案：启动时从 Redis 租用。**

```
副本启动 → Redis SPOP 一个空闲 worker-id（池 0~1023） → 持有期 TTL 心跳续租
副本退出 → 归还 worker-id 回池
```

复用现成 Redis（架构稿「Redis 做跨副本协调」），不加新基建。雪花库需选能处理**时钟回拨**的实现（回拨时等待或报错，不静默重复）。

**故障策略（已定，绝不静默降级）：**
- **租不到 worker-id → fail-fast**：副本启动时拿不到 worker-id 直接**启动失败退出**（健康检查不通过，编排器不放流量），绝不 fallback 到随机/固定 worker-id（那会导致跨副本 ID 撞车，污染主键）。
- **续租失败超安全窗口 → 摘自身**：心跳续租连续失败超过 TTL 安全窗口，副本**主动停止接受新写入 / 摘出健康**，避免 worker-id 被 Redis 判过期回收后、本副本仍用旧 id 生成（与抢到该 id 的新副本撞车）。
- **生产 Redis 必须 HA**：worker-id 租约是写路径硬依赖，Redis 单点不可接受，生产部署主从/哨兵或集群。

> **备查**：本平台单一共享 Postgres，若用 `BIGSERIAL` 自增则 DB 序列天然保证 N 副本不冲突、零协调成本——雪花的「分布式唯一」卖点在单 DB 下用不上，选雪花是为了上面两个收益。
>
> 例外：`api_key` 的「key 本身」不是主键——主键仍是雪花 id，key 明文只展示一次、DB 存哈希（见该表）。

### 0.3 公共列 —— **已定：统一 BaseEntity + 流水表轻量基类**

**配置/业务表**继承统一基类 `BaseEntity`（六个公共字段）：

```python
class BaseEntity(Base):
    __abstract__ = True
    id:         Mapped[int]      = mapped_column(BigInteger, primary_key=True, autoincrement=False)  # 雪花,应用赋值
    is_deleted: Mapped[bool]     = mapped_column(default=False)   # 软删除标记,PG 原生 boolean,is_ 前缀=布尔(Python 规范)
    created_by: Mapped[int|None] = mapped_column(BigInteger, nullable=True)   # 操作者 sys_user.id,软引用,系统行为 null
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_by: Mapped[int|None] = mapped_column(BigInteger, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())
```

**流水表（append-only：用量 / 审计）用轻量基类 `LogEntity`**——它们只增不改、系统生成，套不进上面六字段：

```python
class LogEntity(Base):
    __abstract__ = True
    id:         Mapped[int]      = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    # 无 is_deleted（从不删）、无 updated_*（从不改）、无 created_by（操作者是发起调用的 principal,单独建字段语义更准）
```

**约定说明：**
- 字段命名采用 **Python/SQLAlchemy 惯例**：`created_at` / `updated_at` / `created_by` / `updated_by`（已定）。
- `created_by`/`updated_by` **不做硬外键，只存 `sys_user.id`（软引用）**：避免账户删除连带破坏历史；系统自动行为存 `null` 表「系统」。
- 时间统一 **`timestamptz` 存 UTC**，`server_default=func.now()` 由 DB 生成（不靠应用时钟）。

### 0.4 软删除 —— **已定：统一 `is_deleted` + 部分唯一索引**

- **配置/业务表统一带 `is_deleted`**（在 `BaseEntity` 里），禁用/删除走逻辑标记，不物理删，保留可追溯。
- **流水表（用量/审计）不带 `is_deleted`**，append-only 只增不改。
- **`status`/`enabled` 与 `is_deleted` 并存不冲突**：`is_deleted` 是「记录是否逻辑删除」，`status` 是「业务运行态」（如 `channel_key.status ∈ active/disabled`，DB 只存配置态）。一个 key 可以 `is_deleted=false` 且 `status=disabled`（未删但人工停用）。

**软删除的唯一约束坑（所有「带唯一列 + is_deleted」的表都要处理）：**

软删一个 `name='zhang'` 的行后（仍在表中，`is_deleted=true`），再建同名行会撞唯一约束。解法用 Postgres **部分唯一索引**——只对未删行生效：

```python
__table_args__ = (
    Index("uq_user_username_active", "username", unique=True, postgresql_where=text("is_deleted = false")),
)
```

每张带此情况的表落地时单独标注。

### 0.5 枚举怎么存（✅ 已定）

| 选项 | 做法 | 取舍 |
|---|---|---|
| **Python `Enum` + DB 存字符串**（推荐） | `status: Mapped[str]`，应用层用 `StrEnum` 约束取值 | 改枚举值不用迁移 DB；灵活 |
| PG 原生 `ENUM` 类型 | DB 层 `CREATE TYPE` | 强约束，但加枚举值要 `ALTER TYPE` 迁移，痛 |
| 外键到字典表 | 单独 lookup 表 | 最重，适合可由运营增删的「类目」 |

**已定：Python `StrEnum` + DB 存 `String`。** 值集稳定、改动需发版，无需 DB 强约束。

#### 全部枚举集中定义（enums.py）

> **约定 1（命名）**：成员名 = 存库字符串值。`StrEnum` 下 `Status.active.value == "active"`，ORM 列存 `.value`，Pydantic schema 直接用枚举类型做字段类型校验。当成员名无法等于字符串值时（如 `global` 是关键字、`self` 易混淆），用显式 `= "..."` 指定值并加注释。
> **约定 2（注释，强制）**：**每个枚举成员必须带行内注释说明业务语义**——给后端开发者读代码用。code 如 `active`/`disabled` 尚可自解释，但 `dept_and_child_or_self`/`custom`/`cooldown` 这类不写注释，几周后后端自己都得猜。注释是**开发者文档**（读代码用），与面向终端用户的 `label`（展示译文）是两回事——**两者都在后端，但分属不同位置**：注释在 `enums.py` 成员旁，`label` 在 `locales/{lang}/enums.json`。
> **`label` 归属（v0.5.5 修正）**：`label` 由后端 `locales/{lang}/enums.json` **单源定义**，经 codegen 同步前端 `valueEnum`，并供**服务端导出**（Excel/CSV 枚举列 code→label）消费。运行时 **API 响应仍只发 code**（前端 `valueEnum` 渲染 label，不变）——变的是 label 的**定义源从前端 i18n 上提到后端**（因导出需在服务端拿到 label；遵 Python/Django `TextChoices` 惯例，否决 RuoYi 式 DB 字典表）。详见架构稿 6.12.1 反字典表声明。

```python
from enum import StrEnum

# —— 通用状态（多表共用，避免每表重定义）——
class ActiveStatus(StrEnum):        # 配置表通用启停态:sys_role/sys_menu/logical_model/model_deployment/quota 等
    active = "active"               # 启用
    disabled = "disabled"           # 停用(人工禁用,非删除)

class UsageStatus(StrEnum):         # usage_record 单次调用的最终结果
    success = "success"             # 调用成功
    error = "error"                 # 调用出错(上游报错/网关异常)
    timeout = "timeout"             # 调用超时

# —— 第 1 批 ——
class UserStatus(StrEnum):
    active = "active"               # 在职可用
    disabled = "disabled"           # 离职/停用,禁止登录与调用

class OAuthSource(StrEnum):         # 第三方身份来源
    wecom = "wecom"                 # 企业微信(当前唯一接入源)
    # 预留：dingtalk(钉钉) / feishu(飞书) ...

class ApiKeyStatus(StrEnum):
    active = "active"               # 有效,可用于鉴权
    disabled = "disabled"           # 已吊销,鉴权拒绝

# —— 第 1.5 批 RBAC ——
class MenuType(StrEnum):            # 权限资源节点类型(接口/菜单/按钮合一于 sys_menu)
    catalog = "catalog"             # 目录:纯分组节点,无路由,仅组织树形结构
    menu = "menu"                   # 菜单:可路由的页面
    button = "button"               # 按钮/操作点:细到单个接口权限(perms 码)

class DataScope(StrEnum):           # 数据权限范围,对齐 RuoYi 6 档(挂在 sys_role.data_scope)
    all_data               = "all"                     # 全部数据(不限部门,通常管理员)
    custom                 = "custom"                  # 自定义部门集(具体部门由 sys_role_dept 指定)
    dept_only              = "dept"                    # 仅本部门(用户所在部门)
    dept_and_child         = "dept_and_child"          # 本部门及其所有子部门
    self_only              = "self"                    # 仅本人创建的数据(成员名加 _only 后缀避免与 Python self 习惯混淆)
    dept_and_child_or_self = "dept_and_child_or_self"  # 本部门及以下 或 本人(两者并集)

# —— 第 2 批 号池与模型三层 ——
class ChannelStatus(StrEnum):       # 上游渠道启停态
    active = "active"               # 启用,参与 Router 构建
    disabled = "disabled"           # 停用,不参与路由

class ChannelKeyStatus(StrEnum):    # 上游 key 的 DB 配置态(仅人工配置态;cooldown 是 Redis/Router 运行态,不入库不属本枚举)
    active = "active"               # 可用,参与号池轮询
    disabled = "disabled"           # 人工停用(如 key 泄露/欠费),不参与轮询

class GrantScope(StrEnum):          # 模型授权的作用对象(user_model_grant.scope)
    user = "user"                   # 授权给单个用户(账户级)
    department = "department"       # 授权给整个部门(部门下全员继承)

# —— 第 3 批 配额与计量 ——
class QuotaScope(StrEnum):          # 配额作用对象(quota.scope)
    user = "user"                   # 个人配额
    department = "department"       # 部门总配额
    global_ = "global"              # 平台全局兜底配额(成员名加 _ 尾缀:global 是 Python 关键字;存库值仍 "global")

class QuotaPeriod(StrEnum):         # 配额周期
    daily = "daily"                 # 每日重置
    monthly = "monthly"             # 每月重置
    total = "total"                 # 累计不重置(如一次性总额度)

class QuotaMetric(StrEnum):         # 配额计量维度
    tokens = "tokens"               # 按 token 数限额
    requests = "requests"           # 按请求次数限额
    cost = "cost"                   # 按内部成本点限额(对应 usage_record.cost)

# —— 第 5 批 业务错误码目录（API 错误响应契约，非数据库表）——
# 走 RFC 9457 problem+json:后端只发 code（本枚举值）+ params（插值变量 dict）,
# 前端按 code 查 i18n 文案、用 params 插值（架构稿 G16/6.12.3）。后端永不拼中文错误文案。
# 命名:<域>.<具体错误>,点分两段,与 6.12.5 ⑥ i18n key 两层命名空间同构。
class ErrorCode(StrEnum):           # 业务错误码(problem+json 的 type/code);成员值即下发给前端的 code
    # 鉴权与账户
    auth_invalid_token      = "auth.invalid_token"        # JWT/sk-key 无效或过期
    auth_account_disabled   = "auth.account_disabled"     # 账户已停用(离职/封禁)
    auth_forbidden          = "auth.forbidden"            # 已认证但无权访问该后台资源(RBAC 拒绝)
    # 模型授权与路由
    model_not_granted       = "model.not_granted"         # 用户/部门未被授予该逻辑模型(user_model_grant 缺)
    model_not_found         = "model.not_found"           # 逻辑模型不存在或已停用
    model_no_channel        = "model.no_available_channel" # 该逻辑模型当前无可用承载渠道(全停用/全冷却)
    # 配额
    quota_exceeded          = "quota.exceeded"            # 触达配额上限(params 带 scope/metric/period/limit)
    # 上游
    upstream_error          = "upstream.error"            # 上游厂商返回错误
    upstream_timeout        = "upstream.timeout"          # 上游调用超时
    upstream_rate_limited   = "upstream.rate_limited"     # 上游限流(号池全员被限)
    # 请求本身
    request_invalid         = "request.invalid"           # 请求参数不合法(校验失败,params 带字段级详情)
    # 兜底
    internal_error          = "internal.error"            # 平台内部未预期错误(对应 500)

```

> 注：`QuotaScope.global_` 必须带尾下划线——`global` 是 Python 关键字，不能直接做成员名；存库值仍是 `"global"`。`DataScope` 各成员名与值刻意不同（语义更清晰），其余枚举成员名 = 值。
> 注：以上注释面向**后端开发者**（读代码、联调）。若需让 Swagger/OpenAPI 接口文档也带中文说明，可给成员挂 `description` 属性（`__new__` 手法）注入 FastAPI 的 OpenAPI `description` 字段——**这是文档说明而非返回数据**，前端不拿它当 label。当前默认走行内注释，不上 `description` 样板（YAGNI）。

### 0.6 敏感字段加密（api key / 上游凭据）

- **`channel_key.api_key_encrypted`（上游厂商 key）**：需要**原文**去调上游，所以不能只存哈希 → **对称加密存**（如 Fernet / app 层 KMS），运行时解密。创建/更新入参字段叫 `api_key`（明文），落库列叫 `api_key_encrypted`（密文）。
- **`api_key`（平台签发给用户的 sk-key）**：平台只需**验证**不需还原 → **存哈希**（如 sha256），明文仅创建时展示一次。

> 两者加密策略相反，因为用途相反：一个要拿去用（可逆加密），一个只要比对（单向哈希）。

### 0.7 关联关系 —— **已定：不建物理外键，应用层维护**

**全平台不使用 DB 外键约束（`FOREIGN KEY`）**，所有表间关联只存「目标表的 id 值」，引用完整性由**应用层（service 代码）维护**。

| 项 | 约定 |
|---|---|
| 关联列 | 仍叫 `<目标表>_id`（如 `channel_id`、`logical_model_id`），存对方雪花 id |
| DB 层 | **不加 `ForeignKey(...)` 约束**；ORM 用普通 `mapped_column(BigInteger)`，不用 `relationship()` 的 FK 推导 |
| 引用校验 | 写入前由 service 校验目标存在；删除/禁用时由 service 处理关联（如级联软删、置空、拒绝） |
| 索引 | 关联列**仍建普通索引**（`index=True`），保证 join/过滤性能——不建外键 ≠ 不建索引 |

**理由（与软删除一致）：** ① 软删除模式下，物理 FK 会和 `is_deleted` 冲突（删父行时 FK 阻止，但我们是逻辑删不是物理删）；② N 副本 + 批量异步写场景，FK 约束增加写入耦合与死锁面；③ 关联逻辑（级联怎么处理）本就因表而异，集中在 service 层更可控、可测。这是高并发系统的常见取舍（LiteLLM、多数大体量 Web 系统同样不依赖 DB FK）。

> **代价（须知）：** 放弃 DB 兜底的引用完整性，脏引用（指向已删行的 id）的防护全落在应用层——service 必须自律校验。这也是 `created_by`/`updated_by` 选「软引用」的同款理由（0.3）。

### 0.8 ORM ↔ Pydantic 分离（架构稿 6.2 铁律，复述以便落地）

- `models.py`：SQLAlchemy，落库。
- `schemas.py`：Pydantic，API 进出。每实体按用途拆 `XxxCreate` / `XxxUpdate` / `XxxRead`。
- 敏感字段（key 明文 / 哈希 / 上游 api_key 密文）**绝不进 `XxxRead`**。

---

## 表清单与依赖顺序（落表路线图）

按「被依赖者先建」排序，逐张往下落（`→` = 逻辑引用，存对方 id，**非 DB 外键**，见 0.7）：

```
第 1 批（基础主体与身份）
  sys_user                用户基础档案（HR 同步，principal 真相源）
  sys_department          部门树（邻接表）
  sys_user_oauth          第三方登录身份（企微等）→ sys_user
  api_key                 平台签发的 sk-key 凭据 → sys_user

第 1.5 批（管理后台 RBAC，标准企业级四权限）
  sys_role                角色（含 data_scope 数据权限范围）
  sys_menu                菜单/按钮/接口权限统一表（menu_type 区分）
  sys_user_role           用户↔角色（多对多）→ sys_user + sys_role
  sys_role_menu           角色↔权限（多对多）→ sys_role + sys_menu
  sys_role_dept           角色↔部门（仅 data_scope=custom 时）→ sys_role + sys_department

第 2 批（号池与模型三层，架构稿四之三）
  upstream_channel        上游渠道（厂商+base_url+协议）
  channel_key             号池：渠道下的一组上游 key → upstream_channel
  logical_model           逻辑模型（员工看到的统一名）
  model_deployment        逻辑模型→承载渠道（多对多核心）→ logical_model + upstream_channel
  user_model_grant        模型分配：用户/部门能用哪些逻辑模型 → sys_user + logical_model

第 3 批（配额与计量）
  usage_record            单次调用用量流水（per-call ledger）→ sys_user + logical_model
  quota                   多层配额规则（user/department/global × 模型 × 周期 × 维度）

第 4 批（MCP 服务器，架构稿 G11/四之五）
  （无新表）          平台自身即标准 MCP 服务器,工具是平台内代码;鉴权复用 sys_user/api_key(第1批),
                     工具内 LLM 调用直调自有 LLM 网关复用其配额/记账;MCP 调用在平台侧不留痕(下游第三方是业务真相源)。
                     不建 mcp_server/mcp_grant/工具目录/第三方连接表(详见架构稿四之五「对数据模型的影响:零新表」)。

第 5 批（错误码契约，无新表）
  （无新表）          call_audit 调用审计表**砍掉**——usage_record(第3批)已覆盖审计主要价值(谁/哪把key/何时/
                     什么模型/结果/耗时);源IP/正文/MCP工具调用是合规边际增量,非 v1 必需。将来真需合规取证,
                     再加 append-only 的 call_audit 新表(对存量零迁移)。
  ErrorCode(StrEnum) 业务错误码目录(架构稿 G16/6.12.3,见 0.5 枚举节):业务错误走 RFC 9457 problem+json,
                     后端发 code+params,前端 i18n 按 code 查表插值。是 API 错误契约(enums.py),不是表。
  # downgraded_features(G13 降级标记)已折进第 3 批 usage_record(per-call,降级率一句查询)。
```

> **两种权限不要混淆（核心边界）**：
> - **RBAC（第 1.5 批）= 管理后台操作权限**——「谁能管平台、能操作哪些功能模块」，仅少数后台用户（平台工程师/财务/leader）有。
> - **grant（`user_model_grant`）= AI 资源消费权限**——「全体员工能用哪些模型」，粒度细、与部门/账户强绑。MCP 工具的业务授权交下游第三方（不建 grant 表，G11/四之五）。
> - 二者正交，各管各的。**绝不把「能用 claude-sonnet」做成角色**。

---

## 横切约定已全部锁定（第〇章小结）

| 约定 | 结论 |
|---|---|
| 主键 | `BigInteger` + **雪花算法**（应用层生成，Redis 租 worker-id） |
| 公共列 | 配置表 `BaseEntity`（id/is_deleted/created_by/created_at/updated_by/updated_at）；流水表 `LogEntity`（id/created_at）；关联表 `LinkEntity`（id/created_at，物理删） |
| 字段命名 | Python 风格 `created_at`/`updated_at`/`created_by`/`updated_by` |
| 软删除 | 统一 `is_deleted` + 部分唯一索引；流水表 append-only |
| 枚举 | Python `StrEnum` + DB 存字符串 |
| 敏感字段 | 上游 key 可逆加密（`channel_key.api_key_encrypted`）；平台 sk-key **sha256**（高熵随机）；用户 `password` 列存 **argon2/bcrypt** 哈希（低熵人造）；三者绝不进 `XxxRead` |
| 关联 | **无物理外键**，存 id + 普通索引，应用层维护引用完整性 |
| 表名 | 主体表 `sys_user`（避 PG 保留字，`sys_` 前缀标识平台级基础表，类名仍 `User`）—— **已定** |
| worker-id 故障 | 租不到 → fail-fast 退出；续租失败超窗口 → 摘自身停写；**绝不静默降级随机 id**；生产 Redis 必须 HA |

---

## 第 1 批 · 基础主体与身份（四张表）

> 设计原则：**用户主数据 ≠ 登录身份 ≠ 组织 ≠ 调用凭据**，各有真相源/职责。
> - `sys_user` —— 平台权威用户档案，真相源是 **HR 系统**（定时同步）。
> - `sys_user_oauth` —— 第三方登录身份快照（企微等），真相源是 **第三方**。一个用户每 source 一个身份。
> - `sys_department` —— 组织部门树，模型分配的 scope 之一（架构稿四之三）。
> - `api_key` —— 平台签发给用户的 sk-key 调用凭据，存哈希。
> 服务账号（`oa-system` 等）暂不做（本轮决定），将来需要时再单独设计，不污染 `sys_user`。

### 表 1.1：`sys_user`（用户基础档案，HR 同步）

```python
class User(BaseEntity):
    __tablename__ = "sys_user"

    # 工号：HR 刚换过新工号,但企微 UserID 仍是旧工号 → 拆两列各司其职
    employee_no:        Mapped[str]      = mapped_column(String(64), index=True)  # 新工号,HR 权威键(NOT NULL)
    legacy_employee_no: Mapped[str|None] = mapped_column(String(64), nullable=True, index=True)  # 旧工号,= 企微 UserID,仅老员工有

    username:    Mapped[str]      = mapped_column(String(64))   # 登录键(账密登录用),NOT NULL,业务唯一
    real_name:   Mapped[str|None] = mapped_column(String(64), nullable=True)   # 真实姓名(HR)
    email:       Mapped[str|None] = mapped_column(String(255), nullable=True, index=True)  # 与 oauth.email 对齐 255
    mobile:      Mapped[str|None] = mapped_column(String(32), nullable=True, index=True)

    # 账密登录:仅后台管理员设密码(SSO-only 用户此列为 null);列名叫 password,但存的是 argon2/bcrypt 哈希,绝不明文
    password: Mapped[str|None] = mapped_column(String(255), nullable=True)  # 哈希值,绝不进任何 XxxRead

    department_id: Mapped[int|None] = mapped_column(BigInteger, nullable=True, index=True)  # → sys_department,逻辑引用无 FK

    # 在职状态:离职要停登录但保留审计,与 is_deleted 分工
    status: Mapped[str] = mapped_column(String(16), default="active", index=True)  # UserStatus: active | disabled

    # 语言偏好(架构稿 G16/6.12):后端外发消息(邮件/IM)译文用 + 登录后前端默认语言;BCP 47 格式如 zh-CN/en-US
    preferred_locale: Mapped[str] = mapped_column(String(16), nullable=False, server_default="zh-CN")

    remark: Mapped[str|None] = mapped_column(String(255), nullable=True)

    __table_args__ = (
        Index("uq_user_username_active", "username", unique=True, postgresql_where=text("is_deleted = false")),
        Index("uq_user_employee_no_active", "employee_no", unique=True, postgresql_where=text("is_deleted = false")),
        Index("uq_user_legacy_no_active", "legacy_employee_no", unique=True,
              postgresql_where=text("legacy_employee_no IS NOT NULL AND is_deleted = false")),
    )
```

要点：
- **`sys_user` 只放基础档案**，不含任何企微/第三方字段（移到 1.3）。
- **工号拆两列(关键)**：HR 刚换新工号体系,但企微 UserID 仍沿用旧工号。
  - `employee_no`（新工号）= **HR 权威键 + 账密登录键**，NOT NULL（每个真实用户都有；break-glass admin 给一个合成工号）。HR 同步按它 upsert。
  - `legacy_employee_no`（旧工号）= **企微 UserID 匹配键**，可空（仅换号前的老员工有；换号后入职的新人没有，其企微 UserID 直接 = 新工号）。
- **`username` 是登录键**(账密登录用)，NOT NULL、业务唯一。HR 同步时为每个用户生成(可派生自工号或用工号本身)。
- **账密登录走 `username` + `password`**。普通员工纯企微 SSO,不用 username 登录但仍有此字段(唯一标识)。
- **`password` 列仅后台管理员有**(平台工程师/财务/leader 设密码做 break-glass/SSO 兜底)；普通员工此列 null。**列名叫 `password` 但存的是 argon2/bcrypt 哈希,绝不明文**,**绝不进 `UserRead`**(0.8 铁律)。
- `status`（active/disabled）保留：离职/停用走它（停登录、留审计）；`is_deleted` 留误建/合并的逻辑删。这是「`status` 按表定」（0.4）下对本表的判断。
- **`preferred_locale`（语言偏好，架构稿 G16）**：BCP 47 格式（`zh-CN`/`en-US`），NOT NULL 默认 `zh-CN`。两处用途：① **后端外发消息**（邮件/企微推送/通知）按**收件人**此列渲染本地化正文（外发无前端那层，必须后端译）；② 登录后作为**前端默认语言**的种子（前端可再覆盖并回写）。注意：运行时 API 响应不读这列——展示文本（业务错误/菜单）由前端 i18n 拥有，枚举 label 虽后端单源但运行时也只发 code（前端 `valueEnum` 渲染），后端均只发 code/key（详见架构稿 6.12 三七开原则）。

### 表 1.2：`sys_department`（部门树）

```python
class Department(BaseEntity):
    __tablename__ = "sys_department"

    name:        Mapped[str]      = mapped_column(String(128))
    parent_id:   Mapped[int|None] = mapped_column(BigInteger, nullable=True, index=True)  # → self,逻辑引用,顶级为 null
    external_id: Mapped[str|None] = mapped_column(String(64), nullable=True, index=True)  # HR/企微部门ID,同步映射用
    sort_order:  Mapped[int]      = mapped_column(default=0)   # 同级排序
    remark:      Mapped[str|None] = mapped_column(String(255), nullable=True)

    __table_args__ = (
        Index("uq_dept_external_active", "external_id", unique=True,
              postgresql_where=text("external_id IS NOT NULL AND is_deleted = false")),
    )
```

要点：
- **邻接表（`parent_id` 自引用）建部门树**——简单够用；2000 人规模无需 closure table/物化路径那种重型树结构。
- `external_id` 存 HR/企微的部门 ID，同步时按它对齐，`user.department_id` 指向本表 `id`（不是 external_id）。
- 模型分配「按部门 scope」即指向本表（第 2 批 `user_model_grant` 用）。

### 表 1.3：`sys_user_oauth`（第三方登录身份，精简自 JustAuth）

```python
class UserOAuth(BaseEntity):
    __tablename__ = "sys_user_oauth"

    user_id:  Mapped[int]      = mapped_column(BigInteger, index=True)   # → sys_user,NOT NULL(方案1 硬匹配命中才落库)
    source:   Mapped[str]      = mapped_column(String(32), index=True)   # OAuthSource: wecom | ...
    uuid:     Mapped[str]      = mapped_column(String(64), index=True)   # 第三方用户ID(企微 UserID)

    # 第三方 profile 快照(企微返回的展示信息,非权威)
    nickname: Mapped[str|None] = mapped_column(String(64), nullable=True)
    avatar:   Mapped[str|None] = mapped_column(String(1000), nullable=True)
    email:    Mapped[str|None] = mapped_column(String(255), nullable=True)
    gender:   Mapped[str|None] = mapped_column(String(16), nullable=True)

    raw: Mapped[dict|None] = mapped_column(JSONB, nullable=True)  # 仅脱敏白名单 profile 字段,绝不存 access_token/user_ticket

    __table_args__ = (
        Index("uq_oauth_source_uuid_active", "source", "uuid", unique=True,
              postgresql_where=text("is_deleted = false")),
        Index("uq_oauth_user_source_active", "user_id", "source", unique=True,
              postgresql_where=text("is_deleted = false")),   # 一个用户每个 source 只绑一个身份
    )
```

> **`raw` 安全边界（已定）**：`raw` JSONB **只存脱敏后的非敏感 profile 字段**（昵称、头像 url、部门名等展示信息）。**绝不存 `access_token` / `refresh_token` / `user_ticket` 等凭据**——业内 OAuth 身份表标准做法是不留 token（token 用完即弃,要续期重新走 OAuth 流程）。若将来确需缓存 token,另立加密列 + 保留期,不塞进明文 JSONB。

**与你贴的 JustAuth `AuthUser` 表的差异（删/改了什么）：**

| 原字段 | 处理 | 原因 |
|---|---|---|
| `blog` / `company` / `location` | **删** | JustAuth 为 GitHub/Gitee 公网 OAuth 设计；企微企业 SSO 不返回，永远 null |
| `username` | **删**（归 `sys_user`） | 登录账号是平台主数据，不是第三方快照 |
| `nickname` / `avatar` / `email` / `gender` | 留 | 企微 profile 快照，可展示 |
| `uuid` + `source` | 留，**组合部分唯一** | `(source, uuid)` 唯一锁一个第三方身份 |
| `remark` | 删（归 `sys_user`） | 备注属用户档案 |
| —— | **加 `raw JSONB`** | 仅存脱敏白名单 profile 字段；**绝不存 access_token** |
| `user_id` | 留，**NOT NULL** | 方案1 硬匹配命中才落库，不存在「待绑定 null」中间态 |

### ✅ 已定：企微登录如何「挂上」HR 用户（join key）—— 方案1（工号硬匹配，含新旧工号过渡）

扫码拿到 `(source=wecom, uuid=企微UserID)` 后，若 `sys_user_oauth` 无此身份，要绑定到某个 `sys_user`。

**采用方案1：企微 UserID == 工号（工号即真相键），硬匹配。**

⚠️ **过渡复杂性**：HR 刚换新工号，但企微 UserID 仍沿用**旧工号**。而**新员工没有旧工号**（`legacy_employee_no` 为 null，其企微 UserID 直接 = 新工号）。所以匹配键不是单一字段——**先按旧工号匹配，没有旧工号/未命中再按新工号匹配**：

```
扫码 → (source=wecom, uuid=企微UserID)
     → ① 查 sys_user WHERE legacy_employee_no = uuid   # 老员工:企微 = 旧工号
          ↑ 仅匹配非空 legacy_employee_no(新员工此列 null,SQL 等值天然不命中 null,无需特判)
     → ② ① 未命中再查 WHERE employee_no = uuid          # 新人(无旧工号):企微 = 新工号
     → 命中:写 sys_user_oauth(wecom, uuid) → user_id,完成绑定(此处才落 user_id)
     → 两步都未命中:拒绝登录(该工号未在 HR 同步入库),不自动建号
```

- **新员工无旧工号 → 步骤①自然落空**：SQL `WHERE legacy_employee_no = uuid` 对 null 行返回 false（SQL 三值逻辑，null 不等于任何值），无需额外判空，直接走步骤②。
- **`user_id` 仅在命中时写入**：未命中绝不建 `sys_user_oauth` 行（呼应 1.3 表 `user_id NOT NULL`——不存在「待绑定 null」中间态）。
- **零模糊匹配逻辑**：不靠 email/手机这类可变、可重复的字段，直接工号硬匹配，最稳。
- 两个工号列在 1.1 均 `index=True` + 部分唯一索引，匹配走索引。

> ⚠️ **唯一需警惕的碰撞**：若**某新员工的新工号**恰好等于**某老员工的旧工号**（两套编号空间重叠），步骤①会先把该 uuid 绑到老员工身上（错绑）。
> - **是否存在风险取决于你们新旧工号编号规则**：若新工号有明显区隔（如加前缀/不同位数/不同起始段），永不重叠 → 无风险。
> - 若可能重叠，需在 HR 同步阶段校验「新工号 ∉ 全体旧工号集合」，或调整匹配优先级。**请你确认新旧工号是否可能撞号**；若会撞，这条匹配规则要再加防护。
> - 将来企微 UserID 统一迁到新工号后，可废弃步骤①、清空 `legacy_employee_no`，匹配退化为单键，碰撞风险消失。

- 备选（仅记录，本平台不用）：email/mobile 模糊匹配；管理员手动绑。本企业工号体系完备，方案1 直接覆盖。

### 表 1.4：`api_key`（平台签发的 sk-key 凭据）

> 员工/程序调用网关用的密钥（OpenAI 风格 `sk-...`）。一个用户可签发多把（不同用途/环境）。
> 与上游厂商 key（第 2 批 `channel_key`）**方向相反**：这是平台**发给**调用方的，平台只验不还原 → **存哈希**（0.6 已定）。

```python
class ApiKey(BaseEntity):
    __tablename__ = "api_key"

    user_id:   Mapped[int]      = mapped_column(BigInteger, index=True)   # → sys_user,key 归属人
    name:      Mapped[str]      = mapped_column(String(64))               # 用途标签,如 "本地开发"/"oa-定时任务"

    key_hash:  Mapped[str]      = mapped_column(String(64), index=True)   # sk-key 的 sha256(明文仅创建时返一次),按它查验
    key_prefix:Mapped[str]      = mapped_column(String(16))               # 明文前缀如 "sk-a1b2",仅供列表展示辨识(非敏感)

    status:    Mapped[str]      = mapped_column(String(16), default="active", index=True)  # ApiKeyStatus: active | disabled
    expires_at:   Mapped[datetime|None] = mapped_column(nullable=True)    # 过期时间,null=永不过期
    last_used_at: Mapped[datetime|None] = mapped_column(nullable=True)    # 最近使用(异步更新,非每次同步写,避免热点)

    remark: Mapped[str|None] = mapped_column(String(255), nullable=True)

    __table_args__ = (
        Index("uq_apikey_hash", "key_hash", unique=True),   # 哈希全局唯一(无 is_deleted 条件:哈希撞=同 key,删了也不该复用)
    )
```

要点：
- **存哈希不存明文**（与 `channel_key.api_key_encrypted` 可逆加密相反，0.6）：网关只需「验证这把 key 有效且属于谁」，不需还原。明文 `sk-...` **仅创建响应里返一次**，用户不存就再不可见(只能吊销重发)。
- **`key_hash` 用 sha256 即可**（不用 bcrypt）：key 本身是高熵随机串（非用户低熵密码），无需抗暴力慢哈希；sha256 可建唯一索引 + 等值查验，每次请求快速命中。（对比 `sys_user.password` 用 argon2/bcrypt——那是低熵人造密码，必须慢哈希。）
- **`key_prefix`** 存明文前几位（`sk-a1b2`），列表页给用户辨识「这是哪把」，不泄露完整 key。
- **`status` + `expires_at`** 双控：手动禁用 / 到期自动失效；鉴权时校验 `status=active AND (expires_at IS NULL OR expires_at > now)`。
- **`last_used_at` 异步更新**：每请求同步写会造成单行热点，改为采样/批量异步回写（架构稿审计/计量管线顺带更新）。
- 配额/计量**不在本表**（第 3 批 `usage_record`/`quota`）；本表只管「key 的身份与生命周期」。

### key 的作用域（scope）—— ✅ 已定：本版不做

`api_key` 当前只绑 `user_id`——这把 key 的权限**完全继承归属用户**（能用的模型、配额都按 user 算）。

**决策（已定）：本版不做 per-key scope。** key = 用户身份的代理，权限随用户，简单清晰。第 2 批 `user_model_grant` 已定稿（按 user/department 授权），per-key 收窄无现实需求 → **YAGNI**。将来若真出现「同一用户不同 key 限不同模型」的需求，再新增 `api_key_model_grant` / `api_key_scope` 关联表，不影响现有结构。

### schemas.py（出入参，四表）

```python
# sys_user
class UserCreate(BaseModel):
    employee_no: str                       # 新工号,必填(HR 权威键)
    legacy_employee_no: str | None = None  # 旧工号,老员工才有
    username: str                          # 登录键,必填
    real_name: str | None = None
    email: str | None = None
    mobile: str | None = None
    department_id: int | None = None
    password: str | None = None            # 明文仅入参,service 哈希后存 password 列;SSO-only 用户不传
    remark: str | None = None

class UserUpdate(BaseModel):          # 全可选
    real_name: str | None = None
    email: str | None = None
    mobile: str | None = None
    department_id: int | None = None
    status: UserStatus | None = None
    preferred_locale: str | None = None   # 语言偏好,用户可自助切换(BCP 47),前端切语言后回写
    remark: str | None = None
    # 改密走独立端点,不混在通用 update 里

class UserRead(BaseModel):
    id: int
    employee_no: str
    legacy_employee_no: str | None
    username: str
    real_name: str | None
    email: str | None
    mobile: str | None
    department_id: int | None
    status: UserStatus
    preferred_locale: str                 # 前端登录后据此设默认语言
    created_at: datetime
    # password 绝不出现(0.8 铁律)
    model_config = ConfigDict(from_attributes=True)

# sys_department
class DepartmentRead(BaseModel):
    id: int
    name: str
    parent_id: int | None
    external_id: str | None
    sort_order: int
    model_config = ConfigDict(from_attributes=True)

# sys_user_oauth —— 通常不直接对外,内部绑定用;如需展示则只露非敏感
class UserOAuthRead(BaseModel):
    id: int
    user_id: int
    source: str
    nickname: str | None
    avatar: str | None
    model_config = ConfigDict(from_attributes=True)

# api_key
class ApiKeyCreate(BaseModel):
    name: str
    expires_at: datetime | None = None
    remark: str | None = None
    # user_id 不在入参:由当前登录态/被操作用户决定,防越权签发

class ApiKeyCreateResult(BaseModel):    # 创建专用,唯一一次返明文 key
    id: int
    name: str
    api_key: str            # 明文 sk-...,仅此一次可见
    key_prefix: str
    expires_at: datetime | None

class ApiKeyRead(BaseModel):            # 列表/详情,绝不含明文或哈希
    id: int
    user_id: int
    name: str
    key_prefix: str
    status: str
    expires_at: datetime | None
    last_used_at: datetime | None
    created_at: datetime
    # key_hash / 明文 绝不出现(0.8 铁律)
    model_config = ConfigDict(from_attributes=True)
```

---

## 第 1.5 批 · 管理后台 RBAC（标准企业级，四种权限）

> 仅服务于「**谁能管理这个平台**」。普通员工不进后台、无角色，其 AI 资源权限全由 grant 表决定（见表清单边界说明）。
> 模型对标若依（RuoYi）：**接口/菜单/按钮三权限合一于 `sys_menu`**（`menu_type` 区分，`perms` 串复用于前端按钮显隐 + 后端 API 鉴权）；**数据权限正交**，挂 `sys_role.data_scope`。
>
> **本批已定稿**（对照 RuoYi-Vue-Plus + BladeX 两套企业级 RBAC 后取舍，集各家所长）。

### 设计取舍：RuoYi vs BladeX（已对照，结论锁定）

对照两套主流企业 RBAC schema 后的逐项决策，**每条都是刻意选择，非遗漏**：

| 维度 | RuoYi | BladeX | 本平台采用 | 理由 |
|---|---|---|---|---|
| 菜单/按钮/接口权限 | **合一** `sys_menu`+`perms` | 拆 `blade_menu` + `blade_scope_api` | **学 RuoYi 合一** | 一个 `perms` 码前后端复用，避免权限定义漂移 |
| 数据权限 | `data_scope` 6 档枚举 | `blade_scope_data` 可配置规则表 | **学 RuoYi 枚举** | BladeX 配置表是 MyBatis-Plus 拦截器专属（运行时配置驱动拼 SQL），Python/SQLAlchemy 复刻 = 大量机器 + SQL 注入雷区 + YAGNI（后台用户仅十几人，6 档够用） |
| 用户↔部门 | 单 `dept_id` | 多对多 `blade_user_dept` | **单部门**（决策 2026-06-15） | 经确认企微侧无大量多部门，单字段 99% 场景够用，部门数据权限计算更简单 |
| 角色层级 | 扁平 | 角色树 `parent_id` | **扁平**（决策 2026-06-15） | 后台用户十几人，多角色绑定已足；角色树要递归算权限，过度设计 |
| 用户↔角色 | `sys_user_role` 关联表 | ⚠️ `blade_user.role_id` 存逗号 CSV 串 | **学 RuoYi 关联表** | BladeX CSV 串违反第一范式、无法 join/索引，是反模式 |
| 关联表主键 | 纯复合主键 | 代理 id `blade_role_menu.id` | **学 BladeX 代理 id**（= 本平台 `LinkEntity`） | 代理雪花 id + `UNIQUE` 防重，比纯复合主键多留 `created_at`（授权时间可审计） |

> **被 BladeX 反向验证的两处旧决策**：① `blade_role_menu` 用代理 id 而非复合主键 → 背书我们的 `LinkEntity`；② `blade_user_oauth` 同样无 `blog/company/location` → 背书我们删 oauth 冗余快照字段（见 1.3 差异表）。

### 四种权限如何落到表（先理清，再看字段）

| 权限种类 | 落在哪 | 机制 |
|---|---|---|
| **菜单权限** | `sys_menu`（`menu_type=dir/menu`） | 控制前端路由/侧栏显隐 |
| **按钮权限** | `sys_menu`（`menu_type=button` + `perms`） | 控制前端按钮显隐 |
| **接口权限** | 同上 `perms` 串 | 后端 API 用**同一个** `perms`（如 `pool:key:add`）鉴权——前后端复用一个码 |
| **数据权限** | `sys_role.data_scope`（+ `sys_role_dept`） | 「能看多大范围」：全部/自定义/本部门/本部门及以下/仅本人/本部门及以下或本人（对齐 RuoYi 6 档） |

### `DataScope` 枚举（定义见 0.5 集中枚举节，此处复述语义）

`DataScope` 6 档（`all` / `custom` / `dept` / `dept_and_child` / `self` / `dept_and_child_or_self`），完整 `StrEnum` 定义在 [0.5 全部枚举集中定义](#05-枚举怎么存-已定)。

> ⚠️ `sys_role.data_scope` 的 ORM 默认是字符串 `"self"`，对应枚举成员 `DataScope.self_only`（`.value == "self"`）；`RoleCreate` 的 Pydantic 默认 `DataScope.self_only` 与之一致。**成员名 `self_only` 与存储值 `"self"` 刻意不同名**（`self` 虽非 Python 关键字，但避免与习惯用法混淆），落地时务必让 `StrEnum` 的 value 取 `"self"`。

### 多角色下数据权限如何合并（扁平角色核心用法，必须定义）

一个后台用户可挂多个角色，各角色 `data_scope` 可能不同。**有效数据范围 = 各角色取并集，最宽优先**：

```
① 任一角色 data_scope = all          → 有效范围 = 全部(直接放最宽,短路)
② 否则,各角色范围求并集:
     custom 角色          → 并入其 sys_role_dept 部门集
     dept                 → 并入用户本部门
     dept_and_child       → 并入用户本部门及子树
     self                 → 并入"仅本人"
     dept_and_child_or_self → 并入(本部门及子树) ∪ (本人)
③ 边界 fail-closed:用户无部门(department_id=null)却命中部门类 scope → 该 scope 贡献空集(不放行全部),仅 self 类仍生效
```

- **最宽优先**符合 RBAC 直觉（多角色是能力叠加，不是取交集削权）。
- **fail-closed 是安全底线**：查不到部门绝不退化成「看全部」，只退化成「看自己」或空。
- `custom` 的 `sys_role_dept` 一致性由 service 保证：设 `data_scope=custom` 必须给 `dept_ids`；从 custom 切到其他档时**清空该角色的 `sys_role_dept`**（防残留脏部门集被将来误用）。

### 表 1.5.1：`sys_role`（角色）

```python
class Role(BaseEntity):
    __tablename__ = "sys_role"

    name:       Mapped[str]  = mapped_column(String(64))               # 角色名,如 "平台管理员"(对 ry role_name)
    code:       Mapped[str]  = mapped_column(String(64), index=True)   # 角色标识,如 "platform_admin"(对 ry role_key)
    sort_order: Mapped[int]  = mapped_column(default=0)                 # 对 ry role_sort
    status:     Mapped[str]  = mapped_column(String(16), default="active")  # active | disabled

    # 数据权限范围（正交维度）—— 对齐 RuoYi 6 档
    data_scope: Mapped[str]  = mapped_column(String(16), default="self")
    # DataScope: all(全部) | custom(自定义部门,关 sys_role_dept) | dept(本部门)
    #          | dept_and_child(本部门及以下) | self(仅本人) | dept_and_child_or_self(本部门及以下或本人)

    # 前端树勾选联动开关（纯 UI 行为,对 ry menu_check_strictly / dept_check_strictly）
    menu_check_strictly: Mapped[bool] = mapped_column(default=True)     # 菜单树父子勾选是否联动
    dept_check_strictly: Mapped[bool] = mapped_column(default=True)     # 部门树父子勾选是否联动

    remark: Mapped[str|None] = mapped_column(String(255), nullable=True)

    __table_args__ = (
        Index("uq_role_code_active", "code", unique=True, postgresql_where=text("is_deleted = false")),
    )
```

### 表 1.5.2：`sys_menu`（菜单/按钮/接口权限统一表）

```python
class Menu(BaseEntity):
    __tablename__ = "sys_menu"

    name:      Mapped[str]      = mapped_column(String(64))               # i18n key(如 menu.system.user),前端 formatMessage 译;非中文展示文本(G16)
    parent_id: Mapped[int|None] = mapped_column(BigInteger, nullable=True, index=True)  # → self,顶级 null
    menu_type: Mapped[str]      = mapped_column(String(8))                # MenuType: dir | menu | button(对 ry M/C/F)
    perms:     Mapped[str|None] = mapped_column(String(128), nullable=True, index=True)  # 权限码,如 pool:key:add(button/接口用)
    path:      Mapped[str|None] = mapped_column(String(255), nullable=True)  # 前端路由(menu 用)
    component: Mapped[str|None] = mapped_column(String(255), nullable=True)  # 前端组件路径(menu 用)
    query_param:Mapped[str|None]= mapped_column(String(255), nullable=True)  # 路由参数(对 ry query_param)
    is_frame:  Mapped[bool]     = mapped_column(default=False)            # 是否外链(对 ry is_frame)
    is_cache:  Mapped[bool]     = mapped_column(default=True)             # 是否 keep-alive 缓存(对 ry is_cache)
    icon:      Mapped[str|None] = mapped_column(String(64), nullable=True)
    sort_order:Mapped[int]      = mapped_column(default=0)                # 对 ry order_num
    visible:   Mapped[bool]     = mapped_column(default=True)             # 隐藏菜单(仍鉴权,不显示;对 ry visible)
    status:    Mapped[str]      = mapped_column(String(16), default="active")  # active | disabled
    remark:    Mapped[str|None] = mapped_column(String(255), nullable=True)   # 开发可读备注,说明此菜单业务含义(给后端开发者看 DB 用,非面向终端用户;name 是 i18n key 不可读)

    # 无唯一名约束:同名按钮可挂不同菜单下;perms 唯一性由应用层保证(同一码可被多处引用,故不设 DB 唯一)
```

> **`name` 存 i18n key 而非中文（G16/6.12）**：本列存 `menu.system.user` 这类**稳定标识符**，前端 `menuDataRender` 经 `intl.formatMessage({ id: name })` 译成当前语言。这是 RuoYi-Vue3 / Ant Design Pro / soybean-admin 动态菜单 i18n 的共同做法。后端不存、不发中文菜单名——展示文本由前端 i18n 资源拥有。`remark` 列补回「DB 里也能看懂这菜单是啥」的开发可读性（i18n key 对人不友好）。

> `perms` 是 RBAC 的灵魂：前端「这个按钮显不显」查它，后端「这个 API 放不放行」也查它——**一个码两处用**，避免前后端权限定义漂移。

### 表 1.5.3 / 1.5.4 / 1.5.5：三张关联表

> 均继承 `LinkEntity`（雪花 id + created_at，物理删，已定）。每张加 `UNIQUE` 防重复授权——**等价于 RuoYi 参考库的复合主键** `primary key (user_id, role_id)`，但我们多保留了 id 与 created_at（授权时间可审计，参考库丢了这信息）。

```python
# 用户↔角色
class UserRole(LinkEntity):
    __tablename__ = "sys_user_role"
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)   # → sys_user
    role_id: Mapped[int] = mapped_column(BigInteger, index=True)   # → sys_role
    __table_args__ = (UniqueConstraint("user_id", "role_id", name="uq_user_role"),)

# 角色↔权限
class RoleMenu(LinkEntity):
    __tablename__ = "sys_role_menu"
    role_id: Mapped[int] = mapped_column(BigInteger, index=True)   # → sys_role
    menu_id: Mapped[int] = mapped_column(BigInteger, index=True)   # → sys_menu
    __table_args__ = (UniqueConstraint("role_id", "menu_id", name="uq_role_menu"),)

# 角色↔部门（仅 data_scope=custom 时填）
class RoleDept(LinkEntity):
    __tablename__ = "sys_role_dept"
    role_id: Mapped[int] = mapped_column(BigInteger, index=True)   # → sys_role
    dept_id: Mapped[int] = mapped_column(BigInteger, index=True)   # → sys_department
    __table_args__ = (UniqueConstraint("role_id", "dept_id", name="uq_role_dept"),)
```

### `LinkEntity` 基类（已定，物理删）

关联表（多对多中间表）和业务表性质不同——纯连接、改动即增删、无需软删/审计追溯。新增第三个轻量基类：

```python
class LinkEntity(Base):
    __abstract__ = True
    id:         Mapped[int]      = mapped_column(BigInteger, primary_key=True, autoincrement=False)  # 雪花
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    # 无 is_deleted（关联是物理增删,不软删）、无 updated_*（不更新,只增删）
    # 无 created_by:本轮决定不记录"谁授的权"(后台用户仅十几人,授权操作低频,问责成本>收益)
```

**理由**：给 `sys_user_role` 套 `is_deleted` 反而麻烦——「取消某用户的角色」应是物理删一行，不是标记软删（软删后重新授权又撞唯一、查询全要带 `WHERE is_deleted`）。关联表物理删是业内常规，也与 RuoYi 参考库一致（其关联表无 `del_flag`）。

> **关于授权问责（已决策不做）**：Oracle 评审建议给 `LinkEntity` 加 `created_by` 或另立 `sys_admin_audit_log` 记「谁把角色授给谁/谁撤销」。**本轮明确不做**——后台用户仅十几人、授权变更低频，问责审计的成本大于收益。`created_at` 留着（知道何时授权），「谁授的」暂不追。**将来若合规要求后台操作审计，再统一加 `sys_admin_audit_log`（同理 `call_audit` 调用审计也是届时再加）**，届时是加表不是改这几张关联表的结构，迁移代价小。

> 与参考库差异：RuoYi 关联表是**纯复合主键**（无 id、无 created_at）；我们的 `LinkEntity` = 复合主键防重的「带雪花 id + created_at + UNIQUE 约束」增强版。UNIQUE 约束承担参考库复合主键的防重职责。

> **命名例外说明（`dept_id` vs `department_id`）**：全局约定（0.1）关联列叫 `<目标表>_id`，目标表是 `sys_department` → 严格应为 `department_id`（`sys_user.department_id` 即遵此）。但 RBAC 数据权限相关的 `sys_role_dept.dept_id` 与 schema 的 `dept_ids` **刻意用简称 `dept`**——这是沿用 RuoYi 数据权限体系的术语（`dataScope`/`deptId` 是其固定词汇），保持与参考库、与 `DataScope` 枚举语义一致。两套命名并存是有意为之：主数据建模用全称 `department_id`，RBAC 数据权限沿用 RuoYi 简称 `dept`。

### schemas.py（RBAC 出入参，节选）

```python
class RoleCreate(BaseModel):
    name: str
    code: str
    data_scope: DataScope = DataScope.self_only
    sort_order: int = 0
    remark: str | None = None
    menu_ids: list[int] = Field(default_factory=list)   # 创建时直接绑权限
    dept_ids: list[int] = Field(default_factory=list)   # data_scope=custom 时的部门集

class RoleRead(BaseModel):
    id: int
    name: str
    code: str
    data_scope: DataScope
    status: str
    sort_order: int
    created_at: datetime
    model_config = ConfigDict(from_attributes=True)

class MenuRead(BaseModel):          # 树形返回,前端构菜单
    id: int
    name: str                       # i18n key(如 menu.system.user),前端 formatMessage 译;非中文(G16)
    parent_id: int | None
    menu_type: MenuType
    perms: str | None
    path: str | None
    icon: str | None
    visible: bool
    model_config = ConfigDict(from_attributes=True)
```

---

## 第 2 批 · 号池与模型三层（架构稿四之三 / G12）

> 这是整个网关的**心脏**。落地架构稿「逻辑模型方案 B」：员工看到统一模型名，背后由多个上游承载。
> **核心命题**：DB 里配的这几张表，运行时如何**笛卡尔展开成一个 `litellm.Router` 的 deployment 列表**喂给 Router。轮换/加权/熔断/冷却由 Router 白送，本平台只建配置面 + UI。

### 三层 → litellm.Router deployment 的坍缩（先看全景，再逐表）

```
① upstream_channel  渠道 = 一个「厂商 + api_base + 协议」接入点
        │ 1:N
        ▼
② channel_key       号池 = 渠道下一组上游凭据(加密存,轮换/限额/熔断)
        ╲
         ╲  ③ logical_model  员工看到的统一名(claude-sonnet)
          ╲        │ N:M (model_deployment 承载关系)
           ╲       ▼
            ╲ model_deployment  逻辑模型↔渠道(upstream_model/weight/priority)
             ╲
              ▼
   【Router 配置生成器】把 (logical_model → model_deployment → channel → channel_key)
   笛卡尔展开成 deployment = (model, api_base, api_key) 列表
        ▼
   litellm.Router(model_list=[...])  ← 轮换/加权/fallback/熔断全自动
```

**坍缩规则**（架构稿已定，复述以指导建表）：
- `deployment` 三元组 = `(model, api_base, api_key)`。
- **多上游** = 同一 `logical_model` 经多条 `model_deployment` 挂到**不同 api_base 的渠道** → 多个 deployment。
- **多 key** = 同一渠道下 `channel_key` 有多把 → 同 api_base、不同 key 的多个 deployment。
- 一个 `logical_model` 的最终 deployment 数 = Σ(每条承载 deployment 的渠道下的活跃 key 数)。

> **能力感知路由（G13，B+D）对 schema 的影响已定**：`model_deployment` **无需** `capabilities` 字段——运行时用 `litellm.supports_*(upstream_model, provider)` 查;降级标记 `downgraded_features` 落在第 3 批 `usage_record`（per-call,降级率一句查询;原计划放审计表,因 `call_audit` 砍掉而折入流水）。本批不碰能力字段。

### 本批落表顺序（被依赖者先）

```
表 2.1  upstream_channel   渠道(厂商+api_base+协议)        ← 先建,被 channel_key/model_deployment 依赖
表 2.2  channel_key         号池(渠道下的 key,加密)         → upstream_channel
表 2.3  logical_model       逻辑模型(统一名+计价+上下文)
表 2.4  model_deployment    承载关系(逻辑模型↔渠道,N:M核心) → logical_model + upstream_channel
表 2.5  user_model_grant    模型分配(谁能用哪些逻辑模型)    → sys_user/sys_department + logical_model
```

> 逐表讨论，本轮先定 **2.1 `upstream_channel`**，敲定后再下一张。

### 表 2.1：`upstream_channel`（上游渠道）

```python
class UpstreamChannel(BaseEntity):
    __tablename__ = "upstream_channel"

    name:     Mapped[str]      = mapped_column(String(64))   # 渠道名,如 anthropic-official / bedrock-claude / deepseek
    provider: Mapped[str]      = mapped_column(String(32), index=True)  # 厂商标识: anthropic|gemini|deepseek|glm|qwen|mimo...
    protocol: Mapped[str]      = mapped_column(String(16), index=True)  # 客户端→上游的协议: anthropic|openai|gemini

    api_base: Mapped[str|None] = mapped_column(String(255), nullable=True)  # 上游 base_url,官方厂商可空(litellm 内置默认)

    # provider 专有非密配置(region/api_version 等),非敏感才放这;密钥走 channel_key
    extra_config: Mapped[dict|None] = mapped_column(JSONB, nullable=True)

    status: Mapped[str] = mapped_column(String(16), default="active", index=True)  # ChannelStatus: active | disabled

    remark: Mapped[str|None] = mapped_column(String(255), nullable=True)

    __table_args__ = (
        Index("uq_channel_name_active", "name", unique=True, postgresql_where=text("is_deleted = false")),
    )
```

要点与设计取舍：
- **`provider` vs `protocol` 是两个独立轴**（架构稿四之二的核心）：
  - `provider` = 厂商身份（决定 litellm 入口、计价、能力查询的 model 前缀）。
  - `protocol` = 客户端打到这个上游用什么线协议（决定 litellm 调用入口：`acompletion`/`anthropic.messages.acreate`/`google_genai`）。
  - 二者常一致（anthropic 厂商 + anthropic 协议），但 **DeepSeek/GLM/Qwen/MiMo 是 `provider=各家` + `protocol=openai`**（OpenAI 兼容）。所以必须拆两列，不能合并。
- **`provider`/`protocol` 用 `String` 存（StrEnum 约束）不用 PG ENUM**：新增厂商（如接入新的 OpenAI 兼容模型）只发版加枚举值，不迁移 DB（0.5 已定）。
- **`api_base` 可空**：Anthropic/Gemini 官方走 litellm 内置默认 base_url，不用填；自建/兼容端点（DeepSeek 等）必填。
- **`extra_config JSONB` 只放非敏感配置**（如 Bedrock 的 `region`、Azure 的 `api_version`）；**密钥绝不放这**（走 2.2 `channel_key` 加密列）。
- **`status` 渠道级开关**：禁用渠道 → 其下所有 key 和承载关系一并不参与 Router 构建（service 层在生成 deployment 时过滤 `status=active`）。

### ✅ 本表两个决策（已定）

**决策1：只支持单 key 厂商（选 A）。**
本企业上游全是单 key(Claude/Gemini/DeepSeek/GLM/Qwen/MiMo,一个字符串密钥认证)。故 2.2 `channel_key` 的凭据就一个加密 `api_key` 字段，简单直接。**Bedrock/Vertex 等多段凭据厂商当前不接**——将来真接入时再扩展(加 `credential_type` + 结构化凭据)，现在不为假想需求买单（YAGNI）。

**决策2：litellm model 前缀不入库（选 X）。**
litellm 靠 model 前缀分派（`anthropic/...`、`deepseek/...`、`gemini/...`），但这前缀是 `provider` 的**派生属性**。运行时由代码里一个 `PROVIDER_LITELLM_PREFIX` 映射字典推出，**不在表里冗余存**，表保持干净。

> 至此 **2.1 `upstream_channel` 定稿**。下一张 2.2 `channel_key`。

### 表 2.2：`channel_key`（号池 —— 渠道下的上游凭据）

> 一个渠道下挂一组上游 key（号池），Router 在它们之间轮换/加权/熔断。
> 这把 key 是**上游厂商发给我们的**（要拿去调上游），所以**可逆加密存**（0.6 已定，与 `sys_user.password`/`api_key.key_hash` 的单向哈希方向相反）。

```python
class ChannelKey(BaseEntity):
    __tablename__ = "channel_key"

    channel_id: Mapped[int] = mapped_column(BigInteger, index=True)   # → upstream_channel
    alias:      Mapped[str] = mapped_column(String(64))               # 人类可读标签,如 "anthropic-主号"/"deepseek-备号-2"

    # 上游厂商 key:可逆加密存(运行时解密去调上游),绝不明文落库,绝不进任何 XxxRead
    api_key_encrypted: Mapped[str] = mapped_column(Text)              # Fernet/KMS 密文
    key_hint:          Mapped[str] = mapped_column(String(16))        # 明文尾几位如 "...a1b2",仅供运维辨识,非敏感

    # 配置态:与 is_deleted 分工(0.4)。DB 只存人工配置态;cooldown 是 Router 熔断后的运行态,活在 Redis,不入库(2.2 决策)
    status: Mapped[str] = mapped_column(String(16), default="active", index=True)
    # ChannelKeyStatus: active(可用) | disabled(人工停用) —— cooldown 仅 Redis 侧取值,DB 不存

    # 限额(可选,喂给 litellm.Router 的 rpm/tpm 上限;null=不限)
    rpm_limit: Mapped[int|None] = mapped_column(nullable=True)
    tpm_limit: Mapped[int|None] = mapped_column(nullable=True)

    # 加权/主备(同渠道多 key 间的 Router 路由参数)
    weight:   Mapped[int] = mapped_column(default=1)                  # 加权轮询权重
    priority: Mapped[int] = mapped_column(default=0)                  # 主备:小者优先(0=主)

    last_used_at: Mapped[datetime|None] = mapped_column(nullable=True)  # 僵尸 key 发现(异步更新)

    remark: Mapped[str|None] = mapped_column(String(255), nullable=True)

    __table_args__ = (
        Index("ix_channel_key_channel_status", "channel_id", "status"),   # 构 Router 时按渠道捞活跃 key
    )
```

要点与设计取舍：

**① key 加密落地（核心）**
- **`api_key_encrypted` 存密文,`Text` 类型**（密文比原文长，不限长更稳）。加密用 **Fernet（对称）**，密钥从环境/KMS 注入，**绝不入库、绝不进 git**。
- **`key_hint` 存明文尾 4 位**（`...a1b2`），列表页给运维辨识「这是哪把 key」，不泄露完整 key。（呼应 `api_key.key_prefix` 的同款思路，但 key 是前缀辨识、上游 key 是尾缀辨识——上游 key 前缀往往是固定厂商标识无区分度。）
- **解密时机**：仅在「构建 Router deployment 列表」时解密成内存中的 `(model, api_base, api_key)`，**解密结果不落库、不记日志**。
- 两个加密方向对照（全平台密钥三态）：

  | 字段 | 用途 | 策略 |
  |---|---|---|
  | `channel_key.api_key_encrypted` | 拿去调上游(需还原) | **可逆加密** Fernet |
  | `api_key.key_hash` | 验证平台 sk-key(只比对) | **单向** sha256 |
  | `sys_user.password` | 验证管理员密码(只比对) | **单向慢哈希** argon2/bcrypt |

**② status 三态 + 与 Redis 的分工**
- `active`/`disabled` 是**配置态**(落 DB)：`disabled` 是运维人工停用某把 key。
- `cooldown` 是**运行态**：Router 探测到某 key 连续失败 → 熔断进冷却。**这个状态主要活在 Router 内存/Redis**（架构稿「号池健康/冷却落 Redis，DB 存配置」）。
- ⚠️ **DB 的 `status` 列要不要实时反映 cooldown?** 这是本表待你定的点（见下）。

**③ weight/priority 喂 Router**
- `weight` = 加权轮询（litellm Router `weight`）；`priority` = 主备（小者优先，litellm 用 fallback 顺序表达）。
- 这两个值连同解密后的 key，构成 deployment 的 `litellm_params`。

**④ rpm/tpm 限额**
- 可选，null = 不限。填了就喂 litellm.Router 的 per-deployment rpm/tpm，Router 自动按它限流/切换。

### 本表的决策点（✅ 已定，见下方 2.2 决策）

**问题：`cooldown` 状态要不要写回 DB 的 `status` 列?**
熔断冷却是运行时态，架构稿定了「落 Redis」。但 DB 这个 `status` 列列了 `cooldown` 值——存在两种做法:
- **选 A（推荐）**：DB `status` 只存配置态 `active`/`disabled`；`cooldown` **纯运行时**活在 Redis/Router 内存，**不写回 DB**（避免高频写 DB、避免 DB 与 Redis 状态打架）。DB 永远是「人工配置的意图」，Redis 是「此刻的实际健康」。管理后台健康看板读 Redis 展示 cooldown。
- **选 B**：cooldown 也实时写回 DB `status`，单一数据源。但高频写 + 与 Redis 一致性维护麻烦。
- 我倾向 **A**——配置态与运行态分离，DB 不被高频健康事件污染。若选 A，`status` 列枚举其实只需 `active`/`disabled`，`cooldown` 仅作 Redis 侧取值（文档注释标清）。

你拍这个（A/B），我锁 2.2 进 2.3 `logical_model`（逻辑模型：统一名 + 计价 + 上下文窗口）。

### ✅ 2.2 决策（已定）

**`cooldown` 不写回 DB（选 A）。** DB `status` 列只存配置态 `active`/`disabled`；`cooldown` 是纯运行时态，活在 Redis/Router 内存，不写回 DB。配置意图（DB）与实时健康（Redis）分离，DB 不被高频健康事件污染。管理后台健康看板读 Redis 展示 cooldown。`status` 列注释标清「`cooldown` 仅 Redis 侧取值，DB 不存」。

> 至此 **2.2 `channel_key` 定稿**。下一张 2.3 `logical_model`。

### 表 2.3：`logical_model`（逻辑模型 —— 员工看到的统一名）

> 方案 B 的「员工视角」：员工只认 `claude-sonnet` 这个统一名，不关心背后由哪个渠道/哪把 key 承载。
> 本表存「这个统一名对外的元信息」（展示、计价、上下文）；它**由哪些渠道承载**是下一张 `model_deployment` 的事。

```python
class LogicalModel(BaseEntity):
    __tablename__ = "logical_model"

    name:         Mapped[str] = mapped_column(String(64))   # 统一模型名(员工 API 传的 model 值),如 "claude-sonnet"
    display_name: Mapped[str] = mapped_column(String(64))   # 前端展示名,如 "Claude Sonnet (推荐)"

    # 分组/排序(给员工的模型选择列表用)
    category:   Mapped[str|None] = mapped_column(String(32), nullable=True, index=True)  # 分组,如 "通用"/"代码"/"长文"
    sort_order: Mapped[int]      = mapped_column(default=0)

    # 上下文窗口:可空。null=运行时由 litellm.get_max_tokens(upstream_model) 查;填了=显式覆盖展示
    context_length: Mapped[int|None] = mapped_column(nullable=True)

    # 计价系数(用于配额/账单换算,非真实美元成本)。null=不单独计价
    # 输入/输出分开:LLM 计价普遍 输入≠输出 单价
    price_input:  Mapped[Decimal|None] = mapped_column(Numeric(12, 6), nullable=True)  # 每百万 token 输入价(内部计价单位)
    price_output: Mapped[Decimal|None] = mapped_column(Numeric(12, 6), nullable=True)  # 每百万 token 输出价

    status: Mapped[str] = mapped_column(String(16), default="active", index=True)  # active | disabled

    remark: Mapped[str|None] = mapped_column(String(255), nullable=True)

    __table_args__ = (
        Index("uq_logical_model_name_active", "name", unique=True, postgresql_where=text("is_deleted = false")),
    )
```

要点与设计取舍：
- **`name` 是员工 API 请求里传的 `model` 值**（如 `{"model": "claude-sonnet"}`），全局唯一（部分唯一索引）。这是整个方案 B 的对外契约。
- **`display_name` / `category` / `sort_order`** 纯为前端模型选择器服务（分组、排序、友好名）。
- **`context_length` 可空、运行时兜底**：填了用填的（覆盖展示），不填运行时 `litellm.get_max_tokens()` 查。因为一个逻辑模型可能由多个上游承载、窗口可能不同，这里存的是「对外宣称的窗口」。
- **计价 `price_input`/`price_output` 分开存**：LLM 输入/输出单价不同，必须拆。用 `Numeric(12,6)` 存**内部计价单位**（不是真实美元——公司内部配额换算用，比如「1 配额点 = X token」），按每百万 token 计。null = 该模型不单独计价（如内部免费模型）。
- **本表不存「谁能用」**（那是 2.5 `user_model_grant`）、**不存「谁承载」**（那是 2.4 `model_deployment`）。职责单一：逻辑模型自身的对外元信息。

### 本表的决策点（✅ 已定，见下方 2.3 决策）

**问题：计价系数现在就要，还是先留字段空着？**
配额/计量是第 3 批的事（`usage_record`/`quota`）。本表的 `price_input`/`price_output` 是给那批做换算用的。
- **选 A（推荐）**：字段现在就建好（schema 稳定），值可以先留 null，等第 3 批配额逻辑定了再回填具体计价。建表不必等计价规则。
- **选 B**：计价完全挪到第 3 批，本表现在连字段都不加，等配额设计时再 `ALTER TABLE`。
- 我倾向 **A**——字段先占位（加列零成本），避免第 3 批再改本表结构。计价**规则**留第 3 批，计价**字段**现在建。

你拍这个（A/B），我锁 2.3 进 2.4 `model_deployment`（承载关系，N:M 核心——这张是「笛卡尔展开」的关键，要聊 upstream_model/weight/priority 怎么映射到 Router）。

### ✅ 2.3 决策（已定）

**计价字段现在就建（选 A）。** `price_input`/`price_output` schema 现在占位（加列零成本），值先留 null；计价**规则**留第 3 批配额设计时定、回填，但**字段**现在建好，避免第 3 批回头 `ALTER` 本表。

> 至此 **2.3 `logical_model` 定稿**。下一张 2.4 `model_deployment`（本批核心）。

### 表 2.4：`model_deployment`（承载关系 —— 逻辑模型↔渠道，N:M 核心）

> **方案 B 的心脏**：一个逻辑模型（`claude-sonnet`）由哪些渠道、用各渠道的什么真实模型名承载。
> 它是 N:M 中间表，但**带业务属性**（upstream_model/weight/priority）→ 用 `BaseEntity` 不用 `LinkEntity`（LinkEntity 是纯连接，这张有自己的配置字段要管/审计）。

```python
class ModelDeployment(BaseEntity):
    __tablename__ = "model_deployment"

    logical_model_id: Mapped[int] = mapped_column(BigInteger, index=True)  # → logical_model
    channel_id:       Mapped[int] = mapped_column(BigInteger, index=True)  # → upstream_channel

    # 该渠道下这个逻辑模型对应的真实上游模型名(笛卡尔展开时拼进 deployment 的 model)
    upstream_model: Mapped[str] = mapped_column(String(128))  # 如 claude-sonnet-4-6 / anthropic.claude-3-5-... / deepseek-chat

    # 承载级路由参数(渠道之间怎么选)——与 channel_key 的 key 级参数是两层,见下方说明
    weight:   Mapped[int] = mapped_column(default=1)   # 渠道间加权
    priority: Mapped[int] = mapped_column(default=0)   # 渠道间主备(小者优先,0=主)

    status: Mapped[str] = mapped_column(String(16), default="active", index=True)  # active | disabled

    remark: Mapped[str|None] = mapped_column(String(255), nullable=True)

    __table_args__ = (
        # 同一逻辑模型在同一渠道下只配一条承载(upstream_model 由渠道+逻辑模型唯一确定)
        # 软删表:用部分唯一索引,软删行(is_deleted=true)不占唯一槽,可重建同组合
        Index("uq_deployment_logical_model_channel_active", "logical_model_id", "channel_id",
              unique=True, postgresql_where=text("is_deleted = false")),
        Index("ix_deployment_logical_status", "logical_model_id", "status"),  # 构 Router 时按逻辑模型捞活跃承载
    )
```

要点与设计取舍：
- **`upstream_model` 是关键**：同一逻辑模型 `claude-sonnet` 在不同渠道下真实名不同——Anthropic 官方渠道是 `claude-sonnet-4-6`，Bedrock 渠道是 `anthropic.claude-3-5-sonnet-...`。这一列把「统一名」翻译成「各渠道的真实名」。
- **N:M 但用 `BaseEntity`**：它有 weight/priority/status/upstream_model 等自己的业务字段要管理、要审计（谁改了权重），不是纯关联，故继承 `BaseEntity`（含软删/审计列），不是 `LinkEntity`。
- **部分唯一索引（软删表必须）**：一个逻辑模型在一个渠道下只配一条承载（真实模型名由这两者唯一确定）。因继承 `BaseEntity`（软删），用 `Index(..., unique=True, postgresql_where="is_deleted = false")` 而非全量 `UniqueConstraint`——否则软删一行后无法重建同组合（软删行仍占唯一槽）。两列均非空，无需 NULL guard。

### 笛卡尔展开：DB → litellm.Router deployment 列表（本批命题的答案）

构建 Router 时，service 层这样展开（架构稿四之三的坍缩规则落地）：

```
for lm in logical_model where status=active:                       # 每个逻辑模型
  for dep in model_deployment where logical_model_id=lm.id and status=active:   # 它的每条活跃承载
    ch = upstream_channel[dep.channel_id]                          # 承载渠道
    if ch.status != active: continue                               # 渠道级开关过滤
    for key in channel_key where channel_id=ch.id and status=active:  # 该渠道每把活跃 key
      deployments.append({
        "model_name": lm.name,                                     # 对外统一名(Router 按它分组)
        "litellm_params": {
          "model":    PROVIDER_LITELLM_PREFIX[ch.provider] + dep.upstream_model,  # 如 "anthropic/claude-sonnet-4-6"
          "api_base": ch.api_base,                                 # 官方可空(litellm 默认)
          "api_key":  decrypt(key.api_key_encrypted),              # 仅此刻解密,不落库
          "rpm":      key.rpm_limit,  "tpm": key.tpm_limit,
          "weight":   dep.weight * key.weight,                     # 两层权重相乘(见下)
        },
        # priority 用于 fallback 顺序(litellm Router 的 fallbacks/cooldown)
      })
litellm.Router(model_list=deployments)   # 轮换/加权/fallback/熔断全自动
```

**一个逻辑模型最终的 deployment 数 = Σ(每条活跃承载所在渠道的活跃 key 数)** —— 这就是「笛卡尔展开」。

### 本表的决策点（✅ 已定，见下方 2.4 决策）：两层 weight/priority 如何合并？

`weight`/`priority` 在**两层**都有——`model_deployment`（渠道间）和 `channel_key`（同渠道多 key 间）。展开成扁平 deployment 列表时要合成一个值：

- **weight 合并**：`最终 weight = dep.weight × key.weight`（相乘）。
  - 直觉：先按渠道权重分流量，渠道内再按 key 权重分。相乘自然实现两级分配。
  - 我推荐**相乘**。
- **priority 合并**：这个更微妙。两层 priority 语义是「渠道主备」和「key 主备」。
  - **选 A（推荐）**：priority 只在**渠道层（model_deployment）**有意义，用于 litellm fallback 顺序（主渠道全挂才切备渠道）；`channel_key` 的 priority **降级为同渠道内的次级排序**（同优先渠道内，先用低 priority 的 key）。即：渠道 priority 为主序、key priority 为从序。
  - **选 B**：两层 priority 也相加/分层编码，统一成一个全局序。复杂。
  - 我倾向 **A**——渠道主备是主要场景，key 间一般是平行轮换（weight 够用），key 的 priority 仅作可选的同渠道排序。

**顺带确认**：`channel_key` 那张我已放了 `weight`/`priority`，与本表的同名字段是上述「两层」关系。你认可「weight 相乘、priority 渠道为主序」吗？

你拍这个（weight 相乘 ✓/✗、priority 选 A/B），我锁 2.4 进本批最后一张 2.5 `user_model_grant`（模型分配：谁能用哪些逻辑模型 + 默认模型 + 账户/部门 scope）。

### ✅ 2.4 决策（已定）

**weight 相乘、priority 渠道为主序（选 A）。**
- **weight 合并 = `dep.weight × key.weight`**（相乘）：先按渠道权重分流量，渠道内再按 key 权重分，两级自然叠加。
- **priority**：主序在渠道层（`model_deployment.priority`，喂 litellm Router fallback——主渠道全挂才切备渠道）；`channel_key.priority` 降为**同渠道内 key 的次级排序**（同优先渠道内先用低 priority 的 key）。渠道主备是主场景，key 间默认平行轮换（weight 够用）。

> 至此 **2.4 `model_deployment` 定稿**。下一张 2.5 `user_model_grant`（本批最后一张）。

### 表 2.5：`user_model_grant`（模型分配 —— 谁能用哪些逻辑模型）

> **AI 资源消费权限（B 轴）的落地**——和 RBAC（A 轴，后台操作权限）正交（见表清单边界说明）。
> 解决架构稿「哪个账户/部门能打到哪些逻辑模型 + 默认模型」。架构稿草案叫 `account_model_grant`，因 `account`→`sys_user` 改名，定为 `user_model_grant`。

```python
class UserModelGrant(BaseEntity):
    __tablename__ = "user_model_grant"

    # 多态 scope:这条授权发给「某用户」还是「某部门」(部门级=部门下全员继承)
    scope:    Mapped[str] = mapped_column(String(16), index=True)  # GrantScope: user | department
    scope_id: Mapped[int] = mapped_column(BigInteger, index=True)  # scope=user→sys_user.id; scope=department→sys_department.id

    logical_model_id: Mapped[int]  = mapped_column(BigInteger, index=True)  # → logical_model
    is_default:       Mapped[bool] = mapped_column(default=False)           # 该 scope 的默认模型(员工没指定 model 时用)

    remark: Mapped[str|None] = mapped_column(String(255), nullable=True)

    __table_args__ = (
        # 同一主体不重复授予同一模型。软删表→部分唯一索引(软删行不占槽,可重新授予)
        Index("uq_grant_scope_logical_model_active", "scope", "scope_id", "logical_model_id",
              unique=True, postgresql_where=text("is_deleted = false")),
        # 每个主体最多一个默认模型(部分唯一索引,只约束 is_default=true 的行)
        Index("uq_grant_one_default", "scope", "scope_id", unique=True,
              postgresql_where=text("is_default = true AND is_deleted = false")),
    )
```

要点与设计取舍：
- **带 `is_default` 业务属性 → 用 `BaseEntity` 不用 `LinkEntity`**（同 `model_deployment` 的判断：非纯连接，且模型分配是重要配置，要软删/审计）。
- **多态 scope（`scope` + `scope_id`）**：一行授权既能发给用户也能发给部门。部门级授权 = 该部门下全员继承（裁决时展开）。
- **`is_default` 部分唯一索引**：每个主体（`scope`+`scope_id`）最多一个默认模型，DB 层用「只约束 `is_default=true` 行」的部分唯一索引保证。
- **多态 scope 在「无物理外键」下零额外代价**：`scope_id` 本就靠应用层维护引用（0.7），多态只是 service 校验时按 `scope` 决定查哪张表。

### 裁决算法：员工请求 `model=X`，能不能用？（架构稿四之三落地）

```
用户张三请求 logical_model = "claude-sonnet":
  有效授权集 = user_model_grant where (scope=user, scope_id=张三.id)        # 账户级
             ∪ user_model_grant where (scope=department, scope_id=张三.department_id)  # 部门级继承
  ① X ∈ 有效授权集的 logical_model 集 ？  → 是则放行,否则 403
  ② 员工没传 model(用默认):
       默认模型 = 账户级 is_default 优先;无则取部门级 is_default
       (账户级覆盖部门级 —— 架构稿已定)
```

- **取并集 + 账户级优先**：用户能用的 = 自己被授的 ∪ 所在部门被授的；默认模型账户级覆盖部门级。
- **部门级授权批量省事**：给「技术部」授 `claude-sonnet`，全部门自动可用，不用逐人配。账户级用于个别覆盖（如给某人额外开一个模型）。
- **与配额正交**：本表只管「能不能用」（access），用多少是第 3 批 `quota`/`usage_record` 的事。

### schemas.py（第 2 批，节选）

```python
class ChannelKeyCreate(BaseModel):
    channel_id: int
    alias: str
    api_key: str                       # 明文仅入参,service 加密后存 api_key_encrypted
    rpm_limit: int | None = None
    tpm_limit: int | None = None
    weight: int = 1
    priority: int = 0
    remark: str | None = None

class ChannelKeyRead(BaseModel):       # 绝不含密文/明文,只露 hint
    id: int
    channel_id: int
    alias: str
    key_hint: str
    status: str
    rpm_limit: int | None
    tpm_limit: int | None
    weight: int
    priority: int
    last_used_at: datetime | None
    created_at: datetime
    # api_key_encrypted 绝不出现(0.8 铁律)
    model_config = ConfigDict(from_attributes=True)

class LogicalModelRead(BaseModel):
    id: int
    name: str
    display_name: str
    category: str | None
    context_length: int | None
    status: str
    model_config = ConfigDict(from_attributes=True)

class UserModelGrantCreate(BaseModel):
    scope: GrantScope                  # user | department
    scope_id: int
    logical_model_id: int
    is_default: bool = False
```

---

## 第 2 批已定稿（小结）

| 表 | 职责 | 关键决策 |
|---|---|---|
| `upstream_channel` | 渠道(厂商+api_base+协议) | provider/protocol 拆两列；只支持单 key；前缀不入库 |
| `channel_key` | 号池(渠道下的 key) | Fernet 可逆加密 + key_hint；cooldown 不写回 DB（纯 Redis） |
| `logical_model` | 员工看到的统一名 | 计价字段现建值留 null；context_length 运行时兜底 |
| `model_deployment` | 承载关系(逻辑模型↔渠道,N:M) | upstream_model 翻译真实名；weight 相乘、priority 渠道为主序 |
| `user_model_grant` | 模型分配(AI 消费权限,B 轴) | 多态 scope；账户级覆盖部门级；is_default 部分唯一 |

**核心成果**：「DB 配置 → litellm.Router deployment 列表」的笛卡尔展开伪代码已落地（2.4 节）。第 2 批 5 张表全部定稿。

> **遗留开放点（不阻塞）**：`api_key` 的 per-key scope——第 1 批标注「待第 2 批模型分配定了再回看」。现在 2.5 定了，可决定：是否让单把 `api_key` 限定只能用其归属用户授权集的子集？建议仍按「key 权限 = 完全继承用户」（最简），per-key 收窄留到有真实需求时再加。

---

## 第 3 批 · 配额与计量（架构稿四之三 / 第八章 4）

> 解决「谁用了多少、还能用多少」。架构稿已定关键分工：**Redis 做实时配额计数（热路径快速增减+判断），Postgres 存用量流水+配额配置（持久、可审计、可对账）**。
> 这一批两张表性质完全不同：`usage_record` 是 **append-only 流水**（用 `LogEntity`），`quota` 是**配置+状态**（用 `BaseEntity`）。

### Redis ↔ DB 分工（先理清，这是本批最关键的认知）

```
请求热路径(每次调用):
  ① Redis INCR 实时计数(user:model:日 的已用量)        ← 快,热路径只碰 Redis
  ② 判断 Redis 计数 vs quota 配额上限 → 放行/拒绝(429)
  ③ 调用完成后,异步落一条 usage_record 到 Postgres      ← 流水持久化,不阻塞响应

对账/展示(低频):
  - 用量报表、账单 → 查 Postgres usage_record(聚合)
  - 配额管理(设/改上限) → 改 Postgres quota,同步刷新 Redis 计数器上限
```

**为什么 Redis 计实时、DB 存流水?**
- 配额判断在**每次请求热路径**,必须微秒级——Redis INCR 原子且快;若每次查 DB 聚合 `SUM(tokens)` 会拖垮热路径。
- 但 Redis 易失,不能作账单真相源——**每次调用异步落一条 DB 流水**,DB 是对账/审计的权威记录。
- 两者关系:Redis = 此刻的「跑表」(可重建);DB = 完整的「账本」(权威)。Redis 计数器可从 DB 流水重算重建(如 Redis 重启)。

### 本批落表顺序

```
表 3.1  usage_record   用量流水(append-only,每次调用一条)   ← LogEntity
表 3.2  quota          配额配置+周期状态(谁/什么模型/周期/上限) ← BaseEntity
```

> 逐表讨论，本轮先定 **3.1 `usage_record`**。

### 表 3.1：`usage_record`（用量流水 —— 每次调用落一条）

> append-only 流水,只增不改不删 → 继承 `LogEntity`（id + created_at,无软删/无 updated/无 created_by，0.3 已定）。
> 这是账单/用量报表/降级率统计的权威数据源。

```python
class UsageRecord(LogEntity):
    __tablename__ = "usage_record"

    # —— 主体(算在谁头上)——
    user_id:    Mapped[int]      = mapped_column(BigInteger, index=True)   # → sys_user,principal
    api_key_id: Mapped[int|None] = mapped_column(BigInteger, nullable=True, index=True)  # → api_key,哪把 key 发的(SSO 直连可空)

    # —— 模型与承载(用了什么)——
    logical_model_id: Mapped[int]      = mapped_column(BigInteger, index=True)  # → logical_model,员工请求的统一名
    channel_id:       Mapped[int|None] = mapped_column(BigInteger, nullable=True, index=True)  # → 实际落到的渠道(事后分析)
    upstream_model:   Mapped[str|None] = mapped_column(String(128), nullable=True)  # 实际打的上游真实模型名

    # —— 计量(用了多少)——
    prompt_tokens:     Mapped[int] = mapped_column(default=0)   # 输入 token
    completion_tokens: Mapped[int] = mapped_column(default=0)   # 输出 token
    total_tokens:      Mapped[int] = mapped_column(default=0)   # 合计(冗余,便于聚合)
    cost:        Mapped[Decimal|None] = mapped_column(Numeric(14, 6), nullable=True)  # 按 logical_model 计价系数算的内部成本

    # —— 结果与可观测 ——
    status:      Mapped[str]      = mapped_column(String(16), index=True)  # success | error | timeout
    latency_ms:  Mapped[int|None] = mapped_column(nullable=True)           # 端到端耗时
    request_id:  Mapped[str|None] = mapped_column(String(64), nullable=True, index=True)  # 调用关联 id（贯穿网关链路;无 call_audit 表,本列仅用于跨日志/Redis 排查）
    downgraded_features: Mapped[list|None] = mapped_column(JSONB, nullable=True)  # 本次被丢弃/降级的特性列表(G13);null/空=未降级;降级率统计用

    __table_args__ = (
        # 报表主路径:按 用户+时间 聚合;按 模型+时间 聚合
        Index("ix_usage_user_created", "user_id", "created_at"),
        Index("ix_usage_logical_model_created", "logical_model_id", "created_at"),
    )
```

要点与设计取舍：
- **`LogEntity` append-only**:流水只增,永不改/删,无软删列。对账/审计的权威记录。
- **主体 = `user_id`**(principal,算账对象);`api_key_id` 记哪把 key 发的(SSO 直连无 key 则 null)。
- **记「逻辑模型 + 实际渠道/上游模型」两层**:`logical_model_id` 是员工请求的统一名(配额按它算),`channel_id`/`upstream_model` 是实际落到哪——事后分析「这个逻辑模型实际走了哪些渠道」用。
- **token 三字段**:`prompt`/`completion` 分开(计价输入输出不同价),`total` 冗余存便于聚合(避免每次 `prompt+completion`)。
- **`cost` 按 2.3 计价系数算**:落库时即算好内部成本。**`price_input`/`price_output` 是「每百万 token 单价」,故公式必须除以 1,000,000**:`cost = (prompt_tokens × price_input + completion_tokens × price_output) / 1_000_000`,用 `Decimal` 运算。冻结当时计价(避免日后改价导致历史账变动)。
- **`request_id` 调用关联 id**:贯穿网关一次请求的链路（access/error 日志、Redis 计数 key 等用它串排查）。**本平台不建 `call_audit` 审计表**（见第 5 批），故 `request_id` 不再指向某张审计表，仅作跨日志/Redis 的排查关联键。
- **`downgraded_features` 降级标记（G13）**:`JSONB` 列存本次请求被丢弃/降级的特性列表（如 `["tools", "vision"]`）；`null`/空数组 = 未降级。原计划放审计表，因 `call_audit` 砍掉、降级又是 **per-call** 的事,折进本流水最自然——降级率统计 = `COUNT(*) FILTER (WHERE downgraded_features IS NOT NULL AND downgraded_features <> '[]') / COUNT(*)`，一句查询搞定。
- **索引按报表主路径建**:`(user_id, created_at)` 和 `(logical_model_id, created_at)` 覆盖「某人某段时间用量」「某模型某段时间用量」两大查询。

### 3.1 两个决策（✅ 已定）

**问题1：流水表要不要按时间分区(partition)?**
2000 人、峰值 500 并发,`usage_record` 会**快速膨胀**(每次调用一行,日增可能几十万行)。Postgres 单表过亿行后聚合查询会慢。
- **选 A（采纳）**：先**不分区**,建好 `(user_id, created_at)`/`(logical_model_id, created_at)` 索引。配合「定期归档冷数据」(如 6 个月前的流水转走)。简单,够用一两年。
- **选 B**：上 PG **声明式分区**(按月 `PARTITION BY RANGE (created_at)`),自动滚动。一开始就抗亿级,但运维复杂度(分区维护/迁移)上升。

**问题2：`cost` 落库时冻结 vs 不存 cost 只存 token?**
- **选 A（采纳）**：落库时按当时计价算好 `cost` 存下(冻结历史)。改价不影响历史账单,对账稳定。
- **选 B**：只存 token,`cost` 报表时实时按当前 `logical_model` 计价算。省一列,但改价会让历史账「跟着变」,对账不稳。

**分区选 A（先不分区）、cost 选 A（冻结存）。**
- **不分区**:建好 `(user_id, created_at)`/`(model, created_at)` 索引,配合定期归档冷数据,够用一两年。分区可后加(声明式分区不需开局即上),留意增长真到亿级瓶颈再上 B。
- **cost 冻结**:落库时按当时 `logical_model` 计价系数算好 `cost` 存下,改价不影响历史账单,对账稳定。

> ⚠️ 量级预期未明确——若后续发现调用量很快逼近亿级(日增数十万行 × 一两年),提前评估上 PG 声明式按月分区。当前按 A 落地。
> 至此 **3.1 `usage_record` 定稿**。下一张 3.2 `quota`（本批最后一张）。

### 表 3.2：`quota`（配额配置 + 周期上限）

> 「谁、对什么模型、在什么周期、最多能用多少」。这是配置（运维设定）+ 上限定义；**实时已用量在 Redis**，本表不存「已用」（那会高频写 DB，违背 3.1 的分工）。
> 配置性质 → 继承 `BaseEntity`（要软删、要审计谁改的配额）。

```python
class Quota(BaseEntity):
    __tablename__ = "quota"

    # —— 配额作用对象(多态 scope,与 user_model_grant 同款)——
    scope:    Mapped[str] = mapped_column(String(16), index=True)  # QuotaScope: user | department | global
    scope_id: Mapped[int|None] = mapped_column(BigInteger, nullable=True, index=True)  # global 时为 null

    # —— 限哪个模型(null=不分模型,该 scope 的总配额)——
    logical_model_id: Mapped[int|None] = mapped_column(BigInteger, nullable=True, index=True)  # → logical_model

    # —— 周期 + 计量维度 ——
    period: Mapped[str] = mapped_column(String(16))   # QuotaPeriod: daily | monthly | total(累计不重置)
    metric: Mapped[str] = mapped_column(String(16), default="tokens")  # QuotaMetric: tokens | requests | cost

    limit_value: Mapped[Decimal] = mapped_column(Numeric(14, 6))   # 上限数值。Decimal 与 usage_record.cost 同精度;tokens/requests 取整数值,cost 取小数

    # —— 超限行为 ——
    enforce: Mapped[bool] = mapped_column(default=True)   # true=超限拒绝(429);false=仅告警不拦(软配额)

    status: Mapped[str] = mapped_column(String(16), default="active", index=True)  # ActiveStatus: active | disabled
    remark: Mapped[str|None] = mapped_column(String(255), nullable=True)

    __table_args__ = (
        # 同一(对象,模型,周期,维度)只配一条 active 配额。
        # 三重考量:① 软删表→必须 is_deleted=false 过滤(软删行不占槽);
        #          ② scope_id(global 时)、logical_model_id(总配额时)可为 NULL,
        #             PG 默认 UNIQUE 放过多个 NULL→重复配额漏网,故用 NULLS NOT DISTINCT(PG15+)把 NULL 视作相等;
        # ③ 要求 PostgreSQL 15+。
        Index("uq_quota_scope_model_period_metric_active",
              "scope", "scope_id", "logical_model_id", "period", "metric",
              unique=True,
              postgresql_nulls_not_distinct=True,
              postgresql_where=text("is_deleted = false")),
        # 业务一致性:global 必须无 scope_id;user/department 必须有 scope_id
        CheckConstraint(
            "(scope = 'global' AND scope_id IS NULL) OR (scope <> 'global' AND scope_id IS NOT NULL)",
            name="ck_quota_scope_id_presence"),
    )
```

要点与设计取舍：
- **本表只存「上限定义」,不存「已用量」**:已用在 Redis(3.1 分工)。本表是「规则」,Redis 是「跑表」,`usage_record` 是「账本」——三者各司其职。
- **多态 scope 三档**:`user`(个人配额)/`department`(部门总配额)/`global`(平台兜底)。`global` 时 `scope_id` 为 null。
- **`logical_model_id` 可空**:填了=限定某模型的配额(如「claude-sonnet 每天 100万 token」);null=该 scope 的**总配额**(不分模型,所有模型合计)。
- **`period` 三档**:`daily`/`monthly` 周期重置;`total` 累计不重置(如一次性额度)。
- **`metric` 三选**:按 `tokens`/`requests`/`cost` 计量——可只限 token、只限请求数、或限内部成本点。
- **`limit_value` 用 `Numeric(14,6)`**:与 `usage_record.cost` 同精度,cost 维度配额可直接比对;tokens/requests 维度填整数值即可(Decimal 兼容)。
- **`enforce` 软硬配额**:硬配额超限拒绝(429);软配额仅告警(给 leader 发提醒,不拦)。
- **唯一性用 `NULLS NOT DISTINCT` 部分唯一索引(PG15+)**:`global`/总配额会出现 `scope_id`/`logical_model_id` 为 NULL,PG 默认 UNIQUE 放过多个 NULL→重复 active 配额漏网。`NULLS NOT DISTINCT` 把 NULL 视作相等,配合 `is_deleted=false` 过滤(软删行不占槽)。另加 `CHECK`:`global ⇒ scope_id IS NULL`、非 global ⇒ `scope_id IS NOT NULL`。

### 配额裁决:多档 scope 如何叠加判断?（与 grant 裁决呼应）

```
请求时,收集所有命中的配额规则(全部满足才放行,任一超限即拒):
  ├─ user 级:    (scope=user, scope_id=张三, model=X 或 null)
  ├─ department 级:(scope=department, scope_id=张三部门, model=X 或 null)
  └─ global 级:  (scope=global, model=X 或 null)
  对每条规则:Redis 取该(scope,scope_id,model,metric,period,bucket)的已用计数 vs limit_value
  → 任一 enforce=true 的规则超限 → 拒绝(429,返回最先触顶的那条信息)
  → enforce=false 的规则超限 → 记告警,不拦
```

- **AND 语义(与 grant 的 OR 相反)**:grant 是「能用哪些」取并集放宽;quota 是「多重上限」全部满足才放行——任一档触顶即拦。
- **逐档独立计数,Redis key 必须含 metric + 周期 bucket**:key 形如
  `quota:{scope}:{scope_id|global}:{logical_model_id|all}:{metric}:{period}:{bucket}`
  （`bucket` 是周期窗口标识:daily→`20260615`、monthly→`202606`、total→`all`）。
  一次请求完成时,对命中的每条规则,按其 `metric` 分别累加 tokens / requests / cost(`INCRBY` 整数维度,cost 维度用定点整数或 `INCRBYFLOAT`),并对周期 key 设过期(daily/monthly 自然过期,total 不过期)。
- **设计意图**:个人有个人的天花板、部门有部门的总盘子、平台有全局兜底,三层任一爆了都拦——防单点滥用拖垮全局。

### schemas.py（第 3 批，节选）

```python
class QuotaCreate(BaseModel):
    scope: QuotaScope                      # user | department | global
    scope_id: int | None = None            # global 时不传
    logical_model_id: int | None = None    # null=该 scope 总配额
    period: QuotaPeriod                    # daily | monthly | total
    metric: QuotaMetric = QuotaMetric.tokens
    limit_value: Decimal                   # 与 usage_record.cost 同精度;tokens/requests 维度传整数值
    enforce: bool = True

class QuotaRead(BaseModel):
    id: int
    scope: QuotaScope
    scope_id: int | None
    logical_model_id: int | None
    period: QuotaPeriod
    metric: QuotaMetric
    limit_value: Decimal
    enforce: bool
    status: ActiveStatus
    model_config = ConfigDict(from_attributes=True)

# 用量展示通常是聚合结果,不直接映射 usage_record 单行
class UsageSummary(BaseModel):
    user_id: int
    logical_model_id: int | None
    period_start: datetime
    total_tokens: int
    total_cost: Decimal | None
    request_count: int
```

---

## 第 3 批已定稿（小结）

| 表 | 性质 | 关键决策 |
|---|---|---|
| `usage_record` | append-only 流水(`LogEntity`) | 不分区+索引+归档；cost 冻结存(公式 ÷1,000,000)；记逻辑模型+实际渠道两层；request_id 串审计 |
| `quota` | 配置+上限(`BaseEntity`) | 只存上限不存已用(已用在 Redis)；多态 scope 三档(user/dept/global)；period/metric 多维；enforce 软硬配额；`NULLS NOT DISTINCT` 部分唯一索引(PG15+)+ CHECK；limit_value 用 Decimal |

**核心成果**:Redis(实时计数,key 含 metric+周期 bucket)↔ DB(`usage_record` 账本 + `quota` 规则)三者分工明确。配额裁决 AND 语义(多档全满足才放行)与 grant 的 OR 语义(并集放宽)正交互补。
