# 统一 AI 基础设施平台 — 架构决策（讨论纪要）

> 版本：v1.5（架构决策定稿；G1–G16 已锁定，下游设计细节见第八章）
> 日期：2026-06-16
> 范围：**公司统一内部 AI 基础设施平台**，一个项目。统一身份（企微 JWT + sk-key）+ 统一管理后台（用户/账单/统计），提供两类 AI 能力：**① LLM 网关**（协议矩阵/路由/配额/记账）、**② MCP 服务器**（平台自身即标准 MCP 服务器，工具是平台内代码，鉴权复用 JWT/sk-key，业务授权交下游）
> 关系：《Hermes 企业桌面端集中管控部署》（下称 **A 版**）是本平台的**客户端/消费者**之一；Hermes 既调本平台的 LLM 网关，也连本平台的 MCP 服务器
> 状态：架构决策（定位 / build-buy / 协议矩阵 / 号池模型分配 / 协议特性匹配 / 架构部署 / 前后端栈 / 国际化 / MCP 服务器）已全部厘清并锁定（G1–G16，含第四之五 MCP 设计）；剩余为下游实现细节（核心数据模型 schema / repo 模块边界），见第八章
> 修订：v1.3 补 MCP 协议概念厘清（服务端三 primitive Tools/Resources/Prompts × 谁控制 + 客户端能力 Sampling/Elicitation/Roots，明确本平台实现 Tools 必做/Resources 选做/Prompts 暂不做/Elicitation 必做/Sampling+Roots 不做）；纠正两处：① sampling 是客户端能力非服务端 primitive，工具要 LLM 直调自有网关而非走 sampling 协议；② 删除「2026-07-28 规范改无状态」的依据，改为「服务端选无状态模式（不下发 Mcp-Session-Id）」并加实现期 SDK 验证提醒。v1.5 收口审计/错误码：**不建调用审计表 `call_audit`**——`usage_record` 已覆盖审计主要价值，源 IP/正文/MCP 调用是合规边际增量、非 v1 必需，将来需合规取证再加 append-only 新表；**降级标记 `downgraded_features`（G13）改落 `usage_record`**（per-call 流水）；**MCP 调用平台侧不留痕**（下游第三方是业务真相源）；`ErrorCode(StrEnum)` 错误码目录于数据模型第 5 批定义（无新表）
> 修订：v1.4 按用户「做最标准的 MCP server」定调——三 primitive **全实现**（不再 Tools-only 起步）；落地官方 `mcp` Python SDK `FastMCP`，补标准实现骨架（三装饰器 + `stateless_http=True` + 挂载进 FastAPI）；无状态由 SDK 文档化参数 `stateless_http=True` 确认（撤销 v1.3 的「需验证」caveat）；鉴权定为标准 `Authorization: Bearer` + 自定义 `TokenVerifier` 承载 JWT/sk-key，不上 OAuth 2.1 discovery（维持 G5），保留一处 SDK 是否强制 `AuthSettings` 的实现期验证点

---

## 一、定位：公司统一内部 AI 基础设施

本项目是**公司内部 AI 基础设施的统一平台**，通过一套身份、一套管理后台，纳管全公司所有 AI 能力的接入与调用：

> **平台 = 公司所有 AI 访问的统一入口。** 客户端（Hermes / Claude Code / OA 系统 / 内部脚本 / 其他团队应用）平级，用同一个企微 JWT 或个人 sk-key 接入。平台统一管两类能力：**LLM 调用**（路由到多家厂商）与 **MCP 调用**（路由到各 MCP 服务）。

### 心智模型

```
Hermes / Claude Code / OA系统 / 脚本 / 其他应用
        │ 同一套身份（JWT / sk-key）
        ▼
┌──── 统一 AI 基础设施平台（一个项目）────────┐
│  认证（企微 JWT + sk-key）                  │
│  管理后台（用户 / 账单 / 统计）             │
│  ├─ LLM 网关     → 多家模型厂商             │
│  └─ MCP 服务器   → 平台自研工具（代码）     │
│        └─ 工具内部 → 各第三方系统 REST API  │
│  共享：PostgreSQL + Redis                   │
└─────────────────────────────────────────────┘
```

### LLM 与 MCP 是一对孪生的「AI 能力接入」

| 平台统一管理 | LLM 这一侧 | MCP 这一侧 |
|---|---|---|
| 接入对象 | 上游模型厂商（Claude / GLM / ...） | **平台自研工具**（代码，内部调第三方 API） |
| 客户端怎么用 | 同一个 JWT / sk-key | 同一个 JWT / sk-key |
| 平台做什么 | 路由 / 配额 / 记账 / 审计 | **自身即标准 MCP 服务器** / 鉴权 / 工具发现 / 调用审计 |
| 管理后台 | 模型分配 / 用量报表 | 工具调用统计 |

### 「管 MCP」的定位澄清（平台自身就是 MCP 服务器）

- **平台不是「纳管外部 MCP 服务」，而是自身实现一个标准 MCP 服务器**（Streamable HTTP）。客户端连这一个端点，用协议自带的 `tools/list` 发现工具。
- **工具是平台内的代码**（如 OA 工具 `oa_submit_flow`），由本项目自研、持续扩展；工具**内部**对接某第三方系统（OA / HR / ...）的 **REST API**——第三方是工具调用的普通 API，不是 MCP 服务。
- 业务级权限（「张三能不能提这个流程」）由**下游第三方系统**判断（平台透传用户身份），平台不复制第三方的权限模型。详见第四之五。

### 与 A 版（Hermes 桌面端管控）的关系

- A 版是本平台的**客户端/消费者**：Hermes provider 插件指向本平台的 LLM 网关；Hermes 的 `mcp.json` 指向本平台的 MCP 服务器（一个 Streamable HTTP 端点）。
- A 版自己只管 Hermes 端侧（fork 改造 / 桌面壳 / 分发 / SOUL.md / skills）；**LLM 网关与 MCP 服务器都由本平台提供**。

---

## 二、已确认的架构决策

> 编号用 **G**（Gateway）系列，与 A 版的 D 系列区分。

| # | 决策点 | 结论 |
|---|---|---|
| G1 | 项目定位 | **公司统一内部 AI 基础设施平台，一个项目**。纳管 LLM + MCP 两类接入；Hermes/Claude Code/OA 等均为平级客户端 |
| G2 | build / buy | **自建**：控制面与前端全部自研；**不引入任何独立开源网关服务**（不要 one-api / 不要 LiteLLM Proxy） |
| G3 | 上游适配方式 | **`import litellm` 当库**用于「上游协议格式翻译 + 韧性（Router）」；**绝不 fork / vendor 其源码** |
| G4 | 技术栈 | **Python / FastAPI**（生态成熟，异步流式契合 500 并发场景，MCP SDK 生态好） |
| G5 | 身份模型 | 主体只有一种：**用户主体（`sys_user.id` / `user_id`）**；凭据分层：后台管理面只接受 JWT，LLM Gateway 与 MCP 额外接受 **`sk-xxxx` 静态 key**。JWT 与 sk-key 都归因到同一个 `user_id` / 配额桶，但 sk-key 不等同后台登录态 |
| G6 | 两层归因 | **暂不做**（服务账号代调用时，只算调用方用户的账，不追溯背后员工） |
| G7 | 部署形态 | **模块化单体**：一个 repo，按领域分包。**默认起步 = N 个全功能副本 + nginx 做 HA/扩容**（同一产物复制多份）；**角色拆分**（auth+admin / llm-gateway / mcp-gateway）为后续触发式演进选项，切换只改启动方式不动代码。共享 Postgres + Redis |
| G8 | 编排基础设施 | **Docker Compose**（2000 员工 / 峰值 500 并发，**不上 k8s**；容器将来可无缝迁 k8s） |
| G9 | 客户端协议 | **三协议原生入口（对称）**：OpenAI `/v1/chat/completions`、Anthropic `/v1/messages`、Gemini `/v1beta/models/{model}:generateContent`。任意 SDK（含 Claude Code、Google genai SDK）均可接入 |
| G10 | 协议转换 | **对称 3×3 矩阵 + 同协议直通**：客户端协议 == 上游协议 → 无损直通；异协议 → 转换。由 litellm 库按 `model` 前缀自动决定，**不自写转换器** |
| G11 | MCP 归属 | **平台自身实现一个标准 MCP 服务器**（官方 `mcp` Python SDK 的 `FastMCP`，Streamable HTTP `stateless_http=True`），与 LLM 网关并列的第二类能力。**三 primitive 全实现**（Tools/Resources/Prompts），工具是**平台内的代码**（自研，持续扩展），工具内部对接各第三方系统的 REST API；**不聚合外部 MCP 服务**。鉴权走标准 `Authorization: Bearer` + 自定义 `TokenVerifier` 承载 JWT/sk-key（G5），**不上 OAuth 2.1 discovery**。业务级授权交下游第三方（身份透传），平台不建授权表。详见第四之五 |
| G12 | 号池与模型分配 | **逻辑模型方案（方案 B）+ 三层模型**：模型分配（用户/部门→可用模型）/ 模型目录（逻辑模型→多承载渠道）/ 号池（渠道→多 key）。多上游与多 key 均坍缩为 litellm.Router 的 deployment，轮换/熔断由 Router 白送 |
| G13 | 协议特性匹配 | **B+D 组合**：路由层用 litellm `supports_*()` 能力函数对候选 deployment 做软过滤（优先支持请求特性的渠道，全不支持仍放行），发生降级时回写 `X-Gateway-Downgraded` 响应头 + 记审计。无需给 `model_deployment` 加能力字段 |
| G14 | 技术栈最佳实践 | **对标 LiteLLM Proxy 等业内标准**：全异步（asyncpg + SQLAlchemy 2.0 async + Pydantic v2）；按领域分包；热路径 DB 写异步批量、不绑请求生命周期；无状态副本 + 共享 Redis；暴露 liveness/readiness。详见第六章 |
| G15 | 前后端分离 + 前端栈 | **前后端分离**：FastAPI 纯 JSON API + 独立 SPA。前端栈：**React 19 + TS + Vite 7 + Ant Design 5 + Ant Design Pro（ProTable/ProForm/ProLayout）+ Zustand（客户端态）+ TanStack Query（服务端态）+ Axios**。详见 6.10 |
| G16 | 国际化（i18n） | **展示与逻辑分离，按文本产生位置三七开**：纯展示文本（业务错误/菜单/字段 label）后端只发 code/key，**前端 i18n 拥有译文**；**枚举 label 例外——后端单源（`locales/{lang}/enums.json`）+ codegen 同步前端**（因服务端导出也要消费 label，遵 Python/Django `TextChoices` 惯例，否决 RuoYi 式 DB 字典表）。后端另译两类例外——Pydantic 校验错误（exception handler 译）+ 外发消息（邮件/IM，按收件人 `preferred_locale` 译）。业务错误走 **RFC 9457 `problem+json`**（`code`+`params`，前端插值）；翻译栈用 **JSON/YAML 自研 `I18n` 类**（双语够用，Babel 留演进）。**不建字典表、不出 dict 接口**。详见 6.12 |

---

## 三、网关 build / buy 边界（G2 + G3）

**结论：控制面全自建，litellm 仅作为「翻译 + 韧性」的库，存在感就两个调用点。**

```python
# 你的 FastAPI 网关进程（100% 自研代码）
# —— 客户端协议有两个原生入口，各自调对应的 litellm 库函数 ——

# 入口 1：OpenAI 协议（Hermes + 任意 OpenAI SDK 客户端）
async def handle_openai(request, principal):       # POST /v1/chat/completions
    verify_credential(principal)                    # ← 自建：JWT / sk-key 鉴权
    check_quota(principal, redis)                   # ← 自建：Redis 配额
    model, _ = resolve_model(principal)             # ← 自建：分配表裁决
    resp = await router.acompletion(                # ← litellm：按 model 前缀自动直通/转换
        model=model, messages=..., stream=True
    )
    async for chunk in resp:                        # ← 自建：流式透传 + 尾包记账
        record_usage_if_final(chunk)
        yield to_openai_sse(chunk)

# 入口 2：Anthropic 协议（Claude Code 等）
async def handle_anthropic(request, principal):     # POST /v1/messages
    verify_credential(principal); check_quota(...); model, _ = resolve_model(...)
    resp = await litellm.anthropic.messages.acreate(# ← litellm：anthropic/ 上游则无损直通
        model=model, messages=..., stream=True
    )
    async for chunk in resp:                        # ← 自建：Anthropic SSE 透传 + 记账
        record_usage_if_final(chunk)
        yield chunk                                 # 已是 Anthropic SSE 事件
```

### 自建 vs 引用库 总账

| 层 | 自建 | 引用库 |
|---|---|---|
| LLM 网关 | 客户端协议入口 / 鉴权 / 路由 / 配额 / 记账 / 可观测 | litellm（`acompletion` + `anthropic.messages.acreate` 翻译 + `Router` 韧性） |
| MCP 服务器 | 自身即标准 MCP 服务器 / 工具(代码) / 鉴权 / `tools/list` 发现 / 调用审计 | mcp SDK（协议层，作 **MCP 服务端**暴露 Streamable HTTP 端点） |
| 认证服务 | 企微 OAuth / JWT 签发 / refresh rotation / sk-key 子系统 | pyjwt 等标准 JWT 库 |
| 管理后台 | 后端 CRUD / 报表 + **前端全部** | Ant Design Pro 等 UI 框架 |

**模式一致**：凡「协议 / 格式 / 标准」有现成轮子的 → 引用库；凡「业务 / 身份体系 / 界面」→ 全自建。无 fork、无多语言、不寄生在别人的控制面上。

> **为什么不「拉源码整合」**：litellm 最值钱的部分（厂商适配器、计价表）恰是变化最频繁的部分。copy 源码 = 永久冻结的 fork，每次厂商出新模型 / 改接口都要手动 diff merge，等同于 one-api 改 Go 源码的维护地狱。`pip` 跟版本才能持续吃到社区维护。

> **开源网关的前端对本项目零复用价值**：任何开源网关都自带它自己的一套管理 UI，覆盖不了本项目要的全部管理模块（号池/模型目录/模型分配/用户/配额/MCP 工具统计），风格 / 权限 / 登录也是两套。管理后台前端无论如何都得自研，所以「前端」不构成 build/buy 的选型差异。

---

## 四、上游厂商矩阵（G3）

| 上游 | litellm 接法 | 翻译需求 |
|---|---|---|
| Claude | `anthropic/` 原生 | 需要（OpenAI ↔ Messages 格式） |
| Gemini | `gemini/` 原生 | 需要 |
| DeepSeek | `deepseek/` 原生 | OpenAI 兼容，轻 |
| GLM（智谱） | `openai/` + base_url | 几乎无（OpenAI 兼容透传） |
| Qwen（DashScope） | `dashscope/` 或 `openai/`+base_url | 轻 |
| MiMo（小米） | `openai/` + base_url | 几乎无 |

- 加厂商 = 管理后台里加一条「上游接入」配置，**不动网关代码**。
- 厂商越多，`litellm.Router` 的 fallback（如 GLM 故障切 Qwen）越有价值 —— A 版开放问题「多上游容灾」基本白送。

---

## 四之二、协议转换矩阵（G9 + G10）—— 对称 3×3 + 同协议直通

> 本节修正早期纪要里「客户端永远是 OpenAI 格式」的错误表述。该说法**仅对 Hermes 这一个客户端成立**；统一平台（G1）必须支持多种客户端协议原生入口，否则 Claude Code（Anthropic）、Google genai SDK（Gemini）这类客户端无法接入。

### 核心原则：转换方向由「上游协议」决定，不由「客户端入口」决定

客户端打哪个端点只决定「请求进来是什么格式」；**真正决定转不转、怎么转的，是请求被路由到的上游是什么协议**。同协议直通（无损），异协议转换。**直通是 per-上游 的配置开关，不是全局开关**（来自 new-api 源码实证：`QuantumNous/new-api`，转换由 channel-type 的 Adaptor 决定，passthrough 是 channel setting）。

### 三个客户端入口 → 三个 litellm 库函数

| 客户端协议 | 端点 | 调用的 litellm 库函数 |
|---|---|---|
| OpenAI | `POST /v1/chat/completions` | `litellm.acompletion()` / `Router.acompletion()` |
| Anthropic | `POST /v1/messages` | `litellm.anthropic.messages.acreate()` |
| Gemini | `POST /v1beta/models/{model}:generateContent` | `litellm.google_genai.agenerate_content()` / `agenerate_content_stream()` |

> 三者对称，均为 litellm 库原生提供（Gemini 由 PR #12046 加入，2025/6）。**三个入口都不用自写转换器。**

### 对称 3×3 矩阵（客户端协议 × 上游协议）

| 客户端协议 ↓ \ 上游 → | Anthropic（Claude） | OpenAI 兼容（GLM/DeepSeek/Qwen/MiMo） | Gemini |
|---|---|---|---|
| **Anthropic** `/v1/messages` | **直通·无损** | 转换 | 转换 |
| **OpenAI** `/v1/chat/completions` | 转换 | **直通** | 转换 |
| **Gemini** `/v1beta/.../generateContent` | 转换 | 转换 | **直通·无损** |

对角线 = 同协议直通（无损）；非对角 = litellm 内部转换。**所有格子由 litellm 库按 `model` 前缀自动填**，网关只管开端点 + 路由 + 配额 + 记账。

### 接 Claude Code 的方式（实证）

Claude Code 侧设两个环境变量即接入：

```
ANTHROPIC_BASE_URL  = https://你的网关/v1
ANTHROPIC_AUTH_TOKEN = <员工 sk-key 或 JWT>
```

该员工被分配到 Claude 上游时 → **全程无损直通**（tool_use / prompt caching / 流式事件原样透传）。

### 三个必须记住的约束

1. **直通是 per-上游配置**：管理后台「上游接入」配置里要有「是否 Anthropic 原生 / 是否直通」标记。
2. **跨协议转换有损**（litellm 实证）：Anthropic 的 `cache_control` 转到非 Anthropic 上游会**丢失**；`thinking` 被降级映射成 OpenAI `reasoning_effort`（按 budget_tokens 分档）。→ **影响模型分配策略**：重度用 Claude Code prompt caching 的员工被分配到 GLM/Qwen 会体验降级，需做「客户端协议特性 × 上游能力」匹配。
3. **litellm 直通路径异常类型 bug（[#20507](https://github.com/BerriAI/litellm/issues/20507)，2026/2）**：passthrough 路径异常会退化成 `BaseLLMException` 而非 `RateLimitError`，**破坏 Router 自动重试/fallback**。容灾需在外层自兜异常分类，或盯上游修复。
4. **Gemini `system_instruction` 路由坑（[#12671](https://github.com/BerriAI/litellm/issues/12671)）**：经端点透传时 `system_instruction` / `safety_settings` 可能需放 `extra_body` 才能到达库函数；直接调库函数无此问题。网关侧在调 `agenerate_content()` 前归一化顶层字段即可规避。

---

## 四之三、号池与模型分配（G12）—— 三层模型

> 解决文档此前的缺口：公司在各厂商的 API key 如何统一管理（号池），以及管理后台如何灵活控制「哪个用户/部门能打到哪些模型」（模型分配）。采用**逻辑模型方案（方案 B）**：员工看到统一模型名，背后可由多个上游承载。

### 三层职责（不可压成一层）

```
① 模型分配层 —— 用户/部门 能用哪些逻辑模型 + 默认模型
        │ （对员工的访问控制）
        ▼
② 模型目录层 —— 逻辑模型（如 "claude-sonnet"）由哪些上游渠道承载
        │ （一个逻辑模型 → N 个承载渠道，多上游容灾/分流）
        ▼
③ 号池层 —— 每个上游渠道挂一组 API key（轮换/限额/健康/熔断）
```

**一次请求走三层**：用户张三发 `claude-sonnet` →
- 层①裁决：`claude-sonnet` 在张三分配集内 ✓
- 层②路由：该逻辑模型由「Anthropic 官方 + Bedrock」2 渠道承载，选一个
- 层③出号：从该渠道 key 池轮换一个健康、未超限的 key 发出

### 与 litellm.Router 的坍缩关系（关键）

litellm.Router 的核心单元是 `deployment = (model, api_base, api_key)`。本方案的「多上游」与「多 key」是**同一机制的两个轴**，都坍缩成 deployment：

- 多上游：同名逻辑模型挂多个**不同 api_base** 的 deployment
- 多 key：同名逻辑模型挂多个**相同 api_base、不同 key** 的 deployment

→ **自建一个「Router 配置生成器」**，把 DB 里（逻辑模型 → 承载渠道 → 号池 key）笛卡尔展开成 deployment 列表喂给 Router。**轮换、加权路由、失败熔断冷却由 Router 白送**，本项目只建管理面 + UI。

### 核心表（草案）

```
upstream_channel        渠道 = 一个「厂商 + api_base + 协议」的接入点
  id, name(如 anthropic-official / bedrock-claude)
  provider, api_base, protocol(anthropic|openai|gemini), enabled

channel_key             号池 = 渠道下的一组凭据
  id, channel_id→upstream_channel
  api_key(加密存), alias, status(active/disabled/cooldown)
  rate_limit(rpm/tpm 可选), last_used_at, health(健康探测结果)

logical_model           员工看到的统一模型名
  id, name(如 claude-sonnet), display_name, enabled
  context_length, pricing(计价系数)

model_deployment        逻辑模型 → 承载渠道（多对多，方案 B 的核心）
  id, logical_model_id→logical_model
  channel_id→upstream_channel
  upstream_model(该渠道的真实模型名，如 claude-sonnet-4-6 / anthropic.claude-...)
  weight(加权路由), priority(主备), enabled

user_model_grant        模型分配 = 用户/部门 能用哪些逻辑模型
  id, scope(user|department), scope_id
  logical_model_id→logical_model
  is_default(该 scope 的默认模型)
  注：用户级覆盖部门级；裁决时取并集，user 优先
```

> 配额/用量表按 `user_id × logical_model × 日` 记（完整 schema 见第八章 4）。号池的健康/冷却状态运行时落 Redis，DB 存配置与审计。

### 管理后台对应模块

- **号池管理**：渠道 CRUD、key 录入/轮换/启停、连通性测试、健康看板、`last_used` 僵尸 key 发现
- **模型目录**：逻辑模型 CRUD、承载渠道绑定（权重/主备）、计价配置
- **模型分配**：按部门批量分配 + 按用户覆盖、设默认模型、即时生效（Router 配置短 TTL 重载或热更新）

---

## 四之四、协议特性 × 上游能力匹配（G13，**已决策：B+D**）

> **决策（G13）：采用 B+D 组合** —— 路由层用 litellm 能力函数做软过滤（请求带特性时优先选同协议/支持该特性的承载渠道），无合规渠道而发生降级时回写响应头标注（D 作为 B 的透明化兜底）。本节保留问题分析与调研过程作为决策依据。它横跨第四之二（协议矩阵）与第四之三（模型分配）。

### 问题：跨协议转换会静默丢特性

第四之二实证：当**客户端协议 ≠ 上游协议**时，litellm 做格式转换，部分协议特有能力会**有损**：

| 特性 | 来源协议 | 转到非同协议上游时 |
|---|---|---|
| `cache_control`（提示缓存） | Anthropic | **丢失**（OpenAI/Gemini 无对应字段） |
| `thinking` / `budget_tokens` | Anthropic | 降级映射为 `reasoning_effort`（按档位近似） |
| Gemini 结构化输出 `responseSchema` | Gemini | 老模型需 flatten，可能降级 |
| 各家专有 `tool` 细节 | 各协议 | 跨协议转换可能丢精度 |

### 风险场景

逻辑模型方案（B）下，员工看到的是统一名 `claude-sonnet`，但它背后可能由「Anthropic 官方 + 某 OpenAI 兼容渠道」多渠道承载（容灾/分流）。于是会出现：

> Claude Code 用户依赖 prompt caching 发请求 → Router 这次恰好把他路由到**非 Anthropic 协议的承载渠道** → `cache_control` 被静默丢弃 → 用户感知「缓存突然失效、变慢变贵」，**且无任何报错**。

这是「静默降级」，最难排查——同一个逻辑模型，时好时坏，取决于这次落到哪个承载渠道。

### 可选方案（择一，待决）

| 方案 | 做法 | 取舍 |
|---|---|---|
| **A. 不处理（最简）** | 接受跨协议有损，文档告知用户 | 零工程量；但有静默降级体验问题 |
| **B. 渠道打「协议能力」标签 + 路由约束** | `model_deployment` 加协议/能力字段；当请求带 `cache_control` 等特性时，路由器只选「同协议且支持该特性」的承载渠道 | 体验好、无静默降级；需在路由层加特性感知逻辑，且可能缩小可用渠道（影响容灾） |
| **C. 逻辑模型按协议纯化** | 规定「带原生特性诉求的逻辑模型」只绑定同协议渠道（如 `claude-sonnet` 只绑 Anthropic 协议渠道） | 简单可控；牺牲了跨协议承载的容灾/分流灵活性 |
| **D. 响应头标注降级** | 转换发生时回写响应头（如 `X-Gateway-Downgraded: cache_control`），客户端/用户可感知 | 透明；但需客户端配合看头，治标 |

### 同类项目怎么做（实证调研，2025）

> 调研了三个主流统一网关，看它们如何建模「能力元数据」以及是否做「能力感知路由」。结论：**业界普遍建好了能力元数据，但路由层留了空白。**

| 项目 | 有无能力元数据 | 路由时是否用 | 关键证据 |
|---|---|---|---|
| **LiteLLM**（本项目当库用） | ✅ 一整套 `supports_*` 布尔标志 | ❌ **不用** | Router 只按 model 名 + 上下文窗口 + RPM 过滤；`router.py` 的 `_pre_call_checks()` 里有 `[TODO]` 明说能力过滤未实现。能力数据只用于文档/计价，路由对它视而不见 |
| **one-api / new-api** | ❌ 根本没有 | ❌ 纯名字+优先级 | Channel 只有 `(type, base_url, key, models 逗号串, priority, weight)`；Ability 表只是 `(group, model, channel_id)→(enabled, priority)`。零能力字段 |
| **OpenRouter** | ✅ 最成熟，每模型 `supported_parameters` 数组 + `architecture.input_modalities` | ⚠️ 只给**客户端**筛选 | 支持 `GET /models?supported_parameters=tools` 过滤 + 请求级 `require_parameters` 标志（要求模型支持请求里所有参数才选它）；但服务端路由仍不透明 |

**降级透明度（方案 D）：三个项目无一实现。** litellm 的 `drop_params=True` 是静默丢弃，`drop_params=False` 是直接报错，都不回写「丢了什么」给客户端。

**两个对决策很重要的细节：**

1. **能力元数据 litellm 白送，方案 B 成本被高估了。** litellm 已暴露现成函数：`litellm.supports_prompt_caching(model, provider)`、`supports_reasoning(...)`、`supports_response_schema(...)`、`supports_vision(...)` 等（源 `model_prices_and_context_window.json`）。→ **方案 B 不必自建 `capabilities` 字段，运行时查 litellm 即可**，工程量远小于文档原先的估计。

2. **行业的「兜底」其实就是方案 A 的机制。** litellm `drop_params` 在选完上游后静默丢不支持的参数——这正是「主流默认行为 ≈ 方案 A」。换句话说：什么都不做，跨协议落到不支持的上游时 litellm 会自动丢参数不报错（A）；想避免静默降级，就得在路由层抢在前面过滤（B），而 OpenRouter 的 `require_parameters` 证明这条路有人走通了。

### 决策依据与方案取舍（B+D 选定）

- **纯 A 否决**：本平台是「逻辑模型多渠道承载」，静默降级排查成本高，体验差。
- **B 成本被高估、故采纳**：能力查询走 litellm 现成函数，不需自建能力表；路由层加一个「请求带特性 → 过滤候选 deployment」的钩子即可。这是 OpenRouter `require_parameters` 的同款思路。
- **D 作为 B 的兜底透明化**：路由层用 litellm 能力函数做软过滤（优先同协议/支持特性的渠道），实在没有合规渠道而仍发生降级时，回写响应头标注（如 `X-Gateway-Downgraded: cache_control`），让客户端/用户可感知，避免「时好时坏查不出」。
- **C 未选**（备查）：按协议纯化逻辑模型可零路由改造，但牺牲跨协议容灾；B 已用 litellm 函数把改造成本压到很低，无需退到 C。

### B+D 的落地要点

1. **软过滤，不硬切**：请求携带特性（如 `cache_control` / `thinking`）时，路由层用 `litellm.supports_*(upstream_model, provider)` 把候选 deployment 分两档——「支持」优先，「不支持」降级备选。**全部都不支持时仍放行**（宁可降级也不拒绝服务），但触发 D。
2. **降级标注（D）**：发生「落到不支持特性的渠道」时，响应头回写 `X-Gateway-Downgraded: <feature>`；同时记入调用审计（便于事后统计降级率、发现号池能力缺口）。
3. **容灾不受损**：软过滤只调整**优先级**，不缩小候选集，因此多渠道容灾能力保留。

### 对 schema 的影响（已定）

- **`model_deployment` 无需加 `capabilities` 字段** —— 运行时用 `litellm.supports_*(upstream_model, provider)` 查。
- **降级标记字段 `downgraded_features`**（记录本次请求被丢弃/降级的特性列表）落在 `usage_record`（用量流水，per-call），用于降级率统计。原计划放调用审计表，因 `call_audit` 不建（第八章 4），折入流水最自然。

---

## 四之五、MCP 服务器设计（G11）—— 平台自身即一个标准 MCP 服务器

> 关闭第八章「开放问题 7：MCP 接入细节」。经多轮厘清，本平台对 MCP 的定位**不是「纳管/聚合外部 MCP 服务」，而是平台自身实现一个标准 MCP 服务器**——工具是平台内的自研代码，内部对接各第三方系统的 REST API。这与「LLM 网关聚合多家上游」是**不同形态**：MCP 这侧没有「上游 MCP」可聚合，平台自己**就是**那个 MCP 服务器。

### 核心定位：平台 = MCP 服务器，工具 = 代码，第三方 = 工具内部调的 API

```
客户端（Hermes / Claude Code / 脚本）
    │ Authorization: Bearer <JWT / sk-key>（同 LLM 网关，G5）
    ▼
平台 MCP 服务器（官方 SDK FastMCP，Streamable HTTP 单端点，stateless）
    ├─ Tools（动作）  oa_submit_flow   ──内部调──▶ OA 系统 REST API
    │                query_attendance ──内部调──▶ HR 系统 REST API
    ├─ Resources（只读数据）org://department/{id}/members ...
    └─ Prompts（命名工作流模板）提交 PTO 申请 ...
       （全是平台自研代码，持续扩展）
```

- **工具是代码**（类比 Hermes 自身的 `tools/registry.register` + handler，亦即 `hermes mcp serve` 暴露的那种 MCP 服务器）。新增「对接一个第三方系统」= 写一个新工具，base_url / 路径 / 请求结构天然随代码走。
- **第三方系统是工具内部调用的普通 REST API**，不是 MCP 服务。第三方凭据（如 OA 给平台的 API key）是**平台级配置**，密钥进 `.env`/密钥库，非 per-用户 token。
- **不聚合外部 MCP**——故无「上游 MCP 注册」「工具名跨服务命名空间冲突」「平台→下游 MCP 的 OAuth/token 交换」这些问题（聚合代理模式才有，本平台不采用）。

### MCP 协议概念厘清：服务端三 primitive vs 客户端能力

> 厘清依据：MCP 官方规范（modelcontextprotocol.io，稳定版 2025-11-25）+ Python/TS 官方 SDK。**这是「我们实现哪些」的决策基础**，避免把服务端能力和客户端能力混为一谈。

**服务端三 primitive**（按「谁控制」区分，这是 MCP 的核心设计）：

| primitive | 谁控制 | 干什么 | 协议方法 | 服务端是否执行逻辑 |
|---|---|---|---|---|
| **Tools** | **模型**（LLM 自主决定调用） | 动作 / 有副作用：调 API、写库、跑流程 | `tools/list` → `tools/call` | **是**（返回结果回喂 LLM） |
| **Resources** | **应用**（宿主决定何时读、怎么用） | 只读数据，URI 寻址：文档、schema、知识库 | `resources/list` `resources/read` `resources/templates/list` `resources/subscribe` | 否（只读内容，宿主喂给 LLM） |
| **Prompts** | **用户**（显式选用，如 slash 命令） | 可复用提示模板：把指令+资源+工具打包成命名工作流 | `prompts/list` `prompts/get` | 否（返回消息模板，客户端渲染进对话） |

**客户端能力**（由**客户端/宿主**提供，**不是**服务端 primitive，切勿混淆）：

- **Sampling**（`sampling/createMessage`）：服务端反向请求**客户端**跑一次 LLM 生成（嵌套 LLM 调用）。**本平台不用**——见下「工具要 LLM 时的正确做法」。
- **Elicitation**（`elicitation/create`）：服务端在工具执行中途向**用户**要补充输入（缺参/二次确认）。**本平台需要**——审批流的 human-in-the-loop 靠它。
- **Roots**（`roots/list`）：客户端把文件系统/工作区边界暴露给服务端。**本平台不需要**（只做 API 包装，不碰文件系统）。

**本平台实现哪些（决策：三 primitive 全实现，做最标准的 MCP 服务器）**：

| 能力 | 实现 | 说明 |
|---|---|---|
| **Tools** | ✅ **实现** | OA/HR 动作（提流程、审批、查考勤、建工单）——MCP 这侧的核心价值，`@mcp.tool()` 注册 |
| **Resources** | ✅ **实现** | 暴露只读数据（组织架构、HR 政策文档、API schema 等），`@mcp.resource("uri://...")` 注册；URI 模板支持参数化 |
| **Prompts** | ✅ **实现** | 命名工作流模板（如「提交 PTO 申请」「新人 onboarding」），`@mcp.prompt()` 注册，返回消息模板供客户端渲染 |
| **Elicitation** | ✅ **配合（客户端能力）** | 审批/确认/缺参场景的 human-in-the-loop；工具在执行中途经 `Context` 向用户要补充输入 |
| **Sampling / Roots** | ❌ **不实现** | sampling 用自有网关直调替代（见下）；roots 与本场景（API 包装、不碰文件系统）无关 |

> **为何三 primitive 全实现**：用户明确要求「最标准的 MCP server」。官方参考服务器（Everything server）即三块齐全；官方 Python SDK（`FastMCP`）三个装饰器对等一等公民，实现成本低。三块齐全后客户端（Hermes / Claude Code / 任意 MCP host）拿到的是一个**完整、无能力缺口**的标准服务器，而非「只有 tools 的半成品」。具体每块放什么内容随业务迭代填充，但**能力面板从第一版就完整**。

### 传输与无状态（对齐 6.7）

- **客户端入口 = Streamable HTTP**（MCP 当前/推荐的远程传输；旧的 HTTP+SSE 自 2025-03-26 已 deprecated，不采用）。
- **无状态模式 = 官方 SDK 一等公民**：`FastMCP("...", stateless_http=True, json_response=True)`——官方 README 明确标注此组合为「**recommended**」生产配置。无状态模式下服务端不维护跨请求会话、不依赖 `Mcp-Session-Id`，每个请求自包含，**任意副本可接任意请求**，天然契合「N 个无状态副本 + nginx 无粘滞」（6.7），**无需 Redis session / 粘滞路由**。
  > 这点已落实，非待验证项——`stateless_http` 是 SDK 文档化的标准参数（先前「需验证 SDK 是否支持无状态」「2026-07-28 规范改无状态」两处说法均作废）。
- 本平台工具调用用法（一问一答、无跨请求业务态）本身契合无状态。多步工作流（提交→审批→财务）用「工具返回不透明 handle、后续调用带 handle」实现，状态在下游第三方，不在 MCP 连接里。

### 鉴权（标准 Bearer + 自定义 TokenVerifier，承载 JWT/sk-key，维持 G5）

- **传输层用 MCP 标准 `Authorization: Bearer <token>`**，token 就是我们自己的**企微 JWT** 或**个人 sk-key**——与 LLM 网关**完全同一套凭据层**（G5）。
- **接 SDK 一等公民鉴权钩子 `TokenVerifier`** 自验，**不上 OAuth 2.1 discovery**（不实现授权服务器、不发 protected-resource metadata）：
  ```python
  from mcp.server.auth.provider import AccessToken, TokenVerifier

  class HermesTokenVerifier(TokenVerifier):
      async def verify_token(self, token: str) -> AccessToken | None:
          # 复用 LLM 网关同一套校验：先试 JWT 验签, 再试 sk-key 查 api_key 表
          principal = verify_jwt_or_skkey(token)   # 失败返回 None → SDK 自动 401
          if principal is None:
              return None
          return AccessToken(token=token, client_id=str(principal.user_id),
                             subject=principal.username, scopes=[])
  ```
- **⚠ 实现期一点须验证**：官方示例里 `token_verifier=` 常与 `auth=AuthSettings(...)`（RFC 9728 protected-resource metadata + AS discovery）成对出现。我们**只要 `TokenVerifier` 做校验，不要 OAuth discovery**。落地时验证：(a) SDK 是否允许只给 `token_verifier` 而不给 `auth=AuthSettings`；(b) 若 SDK 强制 `AuthSettings`，则把 issuer/resource 指向平台自身（仍是「标准 Bearer + 自验」，不跑真正的 OAuth 流程）；(c) 退路是用自定义 ASGI 中间件验 Bearer。三者都维持 G5，不引入第二套凭据。
- **工具内取用户身份**：`from mcp.server.auth.middleware.auth_context import get_access_token`；`get_access_token().subject` 即认证用户名，用于透传给下游第三方（见下）。

### 业务授权交下游第三方（平台不建授权表）

> **决策：平台不做 per-user 的工具授权**（不建 `mcp_grant`）。理由：下游第三方系统（OA/HR）本身就是**业务权限的真相源**，平台复制其权限模型会造成「两个真相源」漂移。平台把识别出的用户身份透传给第三方，由第三方判断「这个用户能不能做这个操作」并执行。

- **身份透传 = 「可信子系统」模式**：平台验完 JWT/sk-key 后，调第三方 API 时携带用户身份（如 `X-Forwarded-User` 头 / 第三方约定的身份字段）。第三方对该身份的信任，建立在它**先信任平台这个调用方**（平台持有的服务凭据 + 内网/mTLS）之上——故 `X-Forwarded-User` 不可被外部伪造（是平台基于已验证的 JWT 注入的）。
- **⚠ 两个前提（须知，非阻塞）**：
  1. 第三方必须**能识别并信任**平台断言的用户身份（trusted subsystem）。
  2. 「下游判断权限」只对「下游真的做 per-user 鉴权」的工具成立。将来若新增「调一个不按用户鉴权的共享服务账号 API、却能改数据/花钱」的工具，那类工具需在工具内部自行加判断或单独加一层——**现在不预建授权表**，真出现时再处理。

### 工具发现与可见性

- **工具发现走协议自带 `tools/list`**——客户端连上即可拿到全部工具 schema。工具定义是代码，**不在 DB 建 `mcp_tool` 目录表**（与协议重复）。
- **可见性（默认全可见 + 可选按身份过滤）**：默认所有工具对所有用户可见。规范允许 `tools/list` **按请求的鉴权身份返回不同工具集**——故若将来要做「HR 工具只对 HR 可见」，可在 `tools/list` handler 按 principal 过滤（**代码/配置层判断，仍不建授权表**），而非 DB grant。代价（context 体积 / 看到注定被下游拒的工具）当前**可接受**；要「不发版急停某工具」时，按平台规矩在 `config.yaml` 放工具启用清单（reload 生效）。

### 工具要 LLM 时的正确做法：直调自有网关（不用 sampling）

- MCP 工具内部若需要 LLM 生成能力，**直接 HTTP 调平台自己的 LLM 网关**（内部调用，同用户身份、配额、记账）——**不走 MCP 的 sampling 协议**。
- 原因：sampling 是「服务端反向请求**客户端**跑 LLM」的机制（给没有自有模型的服务端用），而本平台**自身就是 LLM 网关**，直调更简单、链路更短、计费归属清晰。故服务端**不声明 sampling 能力**。

### 标准 SDK 实现骨架（官方 `mcp` Python SDK，FastMCP）

> 依据官方 SDK v1.12.4（`github.com/modelcontextprotocol/python-sdk`）。**用高层 `FastMCP` API**——三 primitive 用三个对等装饰器注册，能力由 SDK 按已注册 handler **自动声明**（无需手写 capabilities）。

```python
from mcp.server.fastmcp import FastMCP, Context
from mcp.server.fastmcp.prompts import base
from mcp.server.auth.middleware.auth_context import get_access_token

# 无状态 + JSON 响应 = 官方推荐的生产配置（无粘滞负载均衡）
mcp = FastMCP("Hermes AI Platform", stateless_http=True, json_response=True,
              token_verifier=HermesTokenVerifier())   # 见上「鉴权」

# ── Tool：动作，内部调第三方 REST API ──
@mcp.tool()
async def oa_submit_flow(flow_type: str, payload: dict) -> dict:
    """提交 OA 审批流程。"""
    user = get_access_token().subject              # 认证用户名
    return await call_oa_api(flow_type, payload,
                             headers={"X-Forwarded-User": user})   # 身份透传

# ── Resource：只读数据，URI（可参数化）──
@mcp.resource("org://department/{dept_id}/members")
async def dept_members(dept_id: str) -> str:
    return await fetch_org_members(dept_id)

# ── Prompt：命名工作流模板 ──
@mcp.prompt(title="提交 PTO 申请")
def pto_request(reason: str) -> list[base.Message]:
    return [base.UserMessage(f"帮我提交一个 PTO 申请，事由：{reason}")]
```

**挂载到现有 FastAPI 网关**（与 LLM 网关同进程/同 repo，按 G7 角色拆分时再分容器）：

```python
import contextlib
from starlette.routing import Mount

@contextlib.asynccontextmanager
async def lifespan(app):
    async with mcp.session_manager.run():   # 启动 MCP 会话管理器
        yield

# 在主 app 工厂里把 MCP 的 ASGI app 挂到子路径
app.router.routes.append(Mount("/mcp", app=mcp.streamable_http_app()))
# app 的 lifespan 需合并 mcp.session_manager.run()
```

要点：单端点（`/mcp`）暴露 Streamable HTTP；`stateless_http=True` 让任意副本可接任意请求；`get_access_token()` 在任意 handler 内取认证身份。

### 对数据模型的影响：**零新表**

MCP 模块对核心数据模型增量为 **0**，全部复用现有设施：

| 能力 | 复用 |
|---|---|
| 鉴权 | 第 1 批 `sys_user` + `api_key`（JWT/sk-key，同 LLM 网关 G5） |
| 调用审计 | **平台侧不留痕**——MCP 工具调用无 token/cost，不进 `usage_record`；下游第三方系统（OA/HR 等）是业务行为的真相源（「张三提了什么流程」记在下游，不在本平台） |
| 工具内 LLM 调用计费 | 工具直调自有 LLM 网关，复用网关的配额/记账（`usage_record` / `quota`），无独立机制 |

故数据模型设计文档**第 4 批无新表**（原计划的 `mcp_server`/`mcp_grant`/工具目录/第三方连接表全部不建）——符合平台「窄腰：能力在边缘（工具代码），核心不膨胀」哲学。

---

## 五、身份与凭据模型（G5 + G6）

### 两个正交的轴

- **主体（principal）= 配额 / 账单算在谁头上**：只有一种 —— **`sys_user.id`（代码/API 命名 `user_id`）**。
- **凭据（credential）= 拿什么证明自己是这个用户主体**：后台 JWT 与程序调用 sk-key 分层。

| 凭据 | 主体 | 签发方式 | 用途 |
|---|---|---|---|
| 平台/企微 JWT | `user_id` | 账密/扫码登录，短期 access + refresh | 后台管理面、Hermes 桌面端、Gateway/MCP |
| `sk-xxxx` 静态 key | `user_id` | 自助门户生成 | **仅 Gateway/MCP 程序化调用**：脚本 / curl / OA 系统；不用于后台管理系统 |

**两类凭据都解析成同一个 `user_id`**，落到同一个配额桶；但后台管理面只接受 JWT。Gateway/MCP 鉴权层分流：

```
Authorization: Bearer xxx
  ├─ 是 JWT（验签通过） → user_id principal
  └─ 是 sk-xxxx → DB 哈希查表 → user_id principal（仅 gateway/mcp）
之后：统一 principal → 一个配额桶 → 路由 / 记账（只写一遍）
```

### 用户主体的两种来源

- **真人用户**：员工，企微扫码登录，可在自助门户生成自己的 `sk-key`。
- **服务用户**：管理员创建的非真人用户（如 `oa-system`、`batch-jobs`），无人扫码，仅持有 `sk-key` 和配额。OA 系统、批处理等系统级调用方挂在服务用户名下。

> 一张 `sys_user` 表搞定所有 principal，**不需要独立的「应用注册」子系统**。这是相对早期设想的简化（去掉了「应用」作为独立主体类型）。

### `sk-xxxx` 安全硬要求

- DB 只存**哈希**，明文创建时**只展示一次**
- 记录 `last_used_at`，便于发现僵尸 key
- 一键吊销、可选过期时间
- （可选）per-key 作用域：限模型 / 设子预算 —— **未决，见第八章**

### G6 的代价（已接受）

放弃「应用代员工调用」的两层归因：OA 系统调网关时，网关只知道是 `oa-system` 在调，**不知道背后哪个员工触发**。报表无法呈现「OA 帮张三发了多少 token」。初版可接受；真要追溯由应用方自己在业务侧记日志。

---

## 六、架构与部署（G7 + G8 + G14）

> 本章定义代码结构、技术栈、部署形态、进程间通信，全部按 Python/FastAPI 业内最佳实践，并对标 LiteLLM Proxy（Python AI 网关事实标准，43k★）。

### 6.1 架构定位：模块化单体

本平台是**模块化单体（modular monolith）**：一个 repo、一个镜像、共享一套存储，内部按领域划分清晰边界。它既不是「大泥球单体」（无内部边界），也不是微服务（分布式部署 + 网络通信）。

| | 大泥球单体 | 模块化单体（本平台） | 微服务 |
|---|---|---|---|
| 代码组织 | 无边界，互相纠缠 | **一个 repo，按领域分包，边界清晰** | 每服务独立 repo |
| 数据库 | 共享 | **共享一个 Postgres** | 每服务独立 DB |
| 部署 | 单进程 | **一镜像，可复制可按角色拆进程** | 独立部署 + 网络调用 |
| 进程间通信 | 无 | **默认共享 DB/Redis，按需逐级加（6.5）** | 必选 RPC + 服务发现 + 服务网格 |
| 适用规模/阶段 | 玩具 | **本平台规模（2000 人 / 峰值 500）的最优解** | 大团队 / 超大规模 / 强隔离需求 |

选它的核心理由：**该规模下微服务的分布式复杂度（服务发现、链路追踪、熔断网格、跨服务事务）是纯负担，换不来收益**；而清晰的内部边界又保证了「将来真要拆服务时低成本」。

### 6.2 项目结构：按领域分包

参照最权威的 FastAPI 结构指南（`zhanymkanov/fastapi-best-practices`）：**按领域分包，不按技术分层**。每个领域包自包含，是一个高内聚的纵切片：

```
src/
  shared/      公共配置（config.py）、DB 引擎与会话（database.py）、通用工具、基础依赖
  auth/        认证（企微 JWT + sk-key 校验、签发）
  admin/       管理后台 API（号池/模型目录/模型分配/用户/配额）
  gateway/     LLM 网关（三协议入口 + litellm.Router 调度 + 记账）
  mcp/         MCP 服务器（标准 MCP 服务端 + 工具代码 + 鉴权复用 + 身份透传 + 审计）
  main.py      app 工厂：按角色（env）挂载对应 router 集合
```

每个领域包内固定文件职责（FastAPI 社区标准切分）：

| 文件 | 职责 |
|---|---|
| `router.py` | HTTP 端点定义。瘦控制器，只做「收参 → 调 service → 返回」，不写业务逻辑 |
| `schemas.py` | **Pydantic 模型** —— 请求/响应的校验与序列化（API 契约层） |
| `models.py` | **SQLAlchemy ORM 模型** —— 持久化层，映射 DB 表 |
| `service.py` | 业务逻辑。纯异步函数，不掺 HTTP / 框架细节，便于测试与复用 |
| `dependencies.py` | FastAPI `Depends()` 注入项（取 DB session、鉴权、取当前用户等） |
| `constants.py` / `exceptions.py` | 该领域的错误码与领域异常（按需） |

**两条 Python 业内铁律（直接决定数据模型怎么写）：**

1. **ORM 模型与 Pydantic 模型严格分离。** `models.py`（SQLAlchemy）管落库，`schemas.py`（Pydantic）管 API 进出。**ORM 实例绝不直接作为请求/响应体**——否则暴露表结构、引发懒加载/循环引用、泄漏敏感字段。一律 ORM ↔ Pydantic 显式转换（`model_validate(orm_obj)`）。
2. **Pydantic 模型按用途拆族，而非一个模型走天下：**
   - 入参：`XxxCreate` / `XxxUpdate`（只含客户端可提交的字段）
   - 出参：`XxxRead` / `XxxResponse`（只含可对外暴露的字段，敏感字段如 key 明文不进出参）

**跨领域引用的边界纪律：** 模块间**只 import 对方的 `schemas`（契约），不 import 对方的 `models`（实现）**。`gateway` 需要 `admin` 的某个 DTO 时 `from admin.schemas import ...`；需要数据时优先读共享 DB 而非反向依赖。需要对外的契约在领域包 `__init__.py` 显式导出。这套约定把「模块化单体」的软边界落到代码层——边界靠纪律 + 评审守，不靠物理拆包。

> 不引入「独立契约包 / api 子包」这类额外层级：Python 靠 `import` 直接引用，没有编译期制品隔离的需求，平行的 `*-api` 包只增加目录跳转、换不来隔离收益。真有某领域要独立成服务时，再把它的 `schemas` 提取为共享包，成本极低（见 6.5 的「按需演进」原则）。

### 6.3 技术栈：全异步 I/O

本平台是 I/O 密集型（每个连接大部分时间在等上游 LLM 吐字），异步是唯一正确选择。实测高并发下异步比同步吞吐高 5–7×，p99 延迟差一个量级。

| 层 | 选型 |
|---|---|
| Web 框架 | **FastAPI**（全 `async def` 路由）+ Uvicorn（ASGI） |
| DB 驱动 | **asyncpg + SQLAlchemy 2.0 async**（`AsyncSession`，每请求一个，依赖注入开关） |
| 校验/序列化 | **Pydantic v2** |
| 缓存/计数/协调 | **Redis**（async 客户端） |
| 上游适配 | **`import litellm`**（库）+ `litellm.Router`（韧性） |

**最佳实践要点：**

- ⚠️ **致命坑**：绝不在 `async def` 里调同步阻塞驱动（如 psycopg2）——会阻塞整个事件循环，比用同步还糟。要异步就全程 asyncpg。CPU 重活/不可避免的同步库调用走 `asyncio.to_thread()` 卸载。
- **热路径 DB 写不绑请求生命周期**：用量/计费/审计写入用异步后台任务**批量**落库（LiteLLM 的 `DBSpendUpdateWriter` 即此模式）。请求只管「转发 + 返回」，记账异步追平，避免每请求一次同步写拖慢首字延迟。
- **worker 回收**：配「请求数上限自动重启」，防长跑进程内存增长。
- **健康检查**：暴露 `/health/liveness` + `/health/readiness`，供 nginx / 编排做存活与就绪探测。

### 6.4 部署形态：默认全功能副本，按需演进

**并发单位是「进程 + async 事件循环」，不是线程**（Python GIL 下多线程扛不了 I/O 并发）。高可用与扩容 = **多起几个进程/容器**，每个进程内由单事件循环用协程并发处理数百连接。

因为是模块化单体（同一镜像含全部模块），部署形态是一道光谱，**沿光谱切换零业务代码改动**：

| 阶段 | 形态 | 启动方式 | 适用 |
|---|---|---|---|
| 开发 | 单进程全功能 | `uvicorn app:app` | 本地调试 |
| **生产默认 ★** | **N 个全功能副本** | **N 份相同启动命令 + nginx 轮询** | **本平台当前选型** |
| 规模演进 | 角色拆分 | 不同启动命令只挂部分 router（admin / gateway / mcp 各配副本数） | 触发下方条件时 |

**默认采用「N 个全功能副本」**：每副本是完整产物（auth+admin+gateway+mcp 全在内），nginx 随意路由；挂一台其余顶上（HA），不够加一台（扩容）。这正是 LiteLLM 的默认姿势——它的 `DISABLE_ADMIN_ENDPOINTS` 角色拆分是**可选优化**，非必须。对 2000 人 / 峰值 500，全功能副本简单且完全够用。

**何时升级到「角色拆分」**（满足任一即触发，绝不预先做）：

1. **爆炸半径隔离**：admin 的故障（OOM、死循环）会拖垮同副本的 gateway 能力。（缓解：多副本已分摊风险）
2. **事件循环争用**：admin 跑重查询（如全公司月用量聚合）卡住同进程的 gateway 流式连接。（缓解：重查询走 `asyncio.to_thread` 或分析库，见第八章）
3. **安全面收窄**：要求 gateway 节点不暴露 admin 接口。（缓解：nginx 按路径路由 + 网络策略）

切换只改启动命令（`uvicorn app:admin_app` / `app:llm_gateway_app` / `app:mcp_gateway_app`），不动业务代码——**所以现在选最简的，不堵死将来**。

### 6.5 进程间通信：默认共享 DB，按需逐级升级

当前已知需求下，各模块（无论同进程还是拆分后）不需要实时同步互调，默认走最省事的「共享 DB」。**但通信能力的门一扇没关**——业务演进时按需补**那一条**通道，无需预先背整套微服务基建：

| 台阶 | 机制 | 何时用 | 新增基建 |
|---|---|---|---|
| **① 共享 DB**（默认） | admin 写配置表，gateway `SELECT` 读 | 配置类、容忍秒级延迟 | 无 |
| **② Redis 发布订阅** | admin 改 key → 发频道；gateway 订阅 → 立即清本地缓存 | 实时单向广播（秒级生效，如 key 吊销） | 无（Redis 已有） |
| **③ 消息队列** | 发事件，异步可靠消费 | 可靠异步 / 削峰 / 事件溯源 | 加 MQ，仍无服务发现 |
| **④ 点对点调用** | gateway 直接 HTTP 调 admin 内部接口 | 实时同步请求-响应 | 只加这一条调用，无需服务发现 |

**绝大多数实时需求止步台阶 ②，零新增基建。** 例：「后台禁用 key，网关 1 秒内停用」→ admin 发 Redis 频道 `key.revoked`，gateway 订阅清缓存。

**关键认知：业务越不确定，越该选模块化单体。** 微服务过早焊死边界（独立 repo+DB+发版，职责跨服务搬家极贵）；模块化单体边界是软的、可移动的（repo 内重构即可）。因边界已按领域划清，将来某模块真要独立成服务，只是**换传输方式（读 DB → 发调用），不是推倒重写**。

### 6.6 运行时拓扑

**默认（全功能副本）：**

```
nginx（反向代理 + 负载均衡 + TLS 终结）
  ├─ 副本 1：全功能（auth + admin + gateway + mcp）
  ├─ 副本 2：全功能（一模一样）
  └─ 副本 N：全功能（按并发增减）
共享后端（已有现成实例，本项目不部署）：
  ├─ PostgreSQL（用户 / key / 模型分配 / 用量 / 审计；MCP 无专属表，复用用户/审计）
  └─ Redis（实时配额计数 + 配置短 TTL 缓存 + 跨副本协调 + 发布订阅）
```

**演进态（触发后角色拆分）：**

```
nginx（按路径路由 + 负载均衡）
  ├─ 容器组 A：auth + admin   ← 低频，1~2 副本做 HA
  ├─ 容器组 B：gateway        ← LLM 热路径，N 副本，按并发横向扩 ★
  └─ 容器组 C：mcp            ← MCP 服务器/工具调用/审计，中频
共享后端：同上
```

### 6.7 无状态副本 + nginx 流式配置

**无状态是横向扩的前提**：副本不在内存存会话态——JWT 验签无状态（只需公钥）、配额计数在 Redis、持久数据在 Postgres。因此**无需会话粘滞**，加副本 = nginx upstream 加一行。

**SSE 流式必配**（LLM 响应是流式，配错则首字延迟爆炸）：

```nginx
proxy_buffering off;        # 不关 → 流式退化为「等全部生成完才一次性吐」
proxy_read_timeout 600s;    # 长连接别被默认 60s 掐断
```

### 6.8 与业内对标

| 维度 | LiteLLM Proxy（事实标准） | 本平台 | 对齐 |
|---|---|---|---|
| Web 框架 | FastAPI + Uvicorn，全异步 | 同（G4） | ✅ |
| DB | PostgreSQL（生产首选） | 共享 PostgreSQL | ✅ |
| 横向扩 | 无状态 worker + 共享 Redis | 同 | ✅ |
| 热路径/控制面拆分 | `DISABLE_ADMIN_ENDPOINTS` 跑网关角色 | 角色拆分演进选项（G7） | ✅ 同款 |
| 部署 | Docker Compose（postgres + redis） | 同（G8） | ✅ |

**旁证**：Langfuse = `web`/`worker` 两容器同代码库；Portkey = 控制面不在热路径、数据面本地缓存配置后独立运行。**「一份代码 → 按角色拆进程 → 共享存储」是这一类系统的公认范式。**

### 6.9 规模判断与编排

2000 员工 / 峰值 500 并发是 I/O bound，asyncio 主场，几个全功能副本即足。**用 Docker Compose，不上 k8s（G8）**——该规模上 k8s 是「为了像微服务」的过度工程。容器无状态，将来真需要可无缝迁 k8s（gateway 配 HPA），但那是规模拐点之后的事。

### 6.10 前后端分离与 Admin 前端技术栈（G15）

**决策（G15）：前后端分离。** FastAPI 只做纯 JSON API 后端；Admin 是独立的 SPA 前端，经 REST 对接。理由：2000 人规模下 Admin 是平台工程师/财务/团队 leader 每天用的产品，前后端独立演进、独立部署优于内嵌静态产物（区别于 LiteLLM 的 FastAPI 挂载方式，对齐 Langfuse/Helicone 的独立前端做法）。

**Admin 不是面向公众站点**（内网中后台，数据密集 CRUD + 仪表盘，无 SEO/SSR 需求），故选 **SPA**，不上 Next.js SSR。

#### 前端技术栈（已定）

| 层 | 选型 | 说明 |
|---|---|---|
| 框架 | **React 19 + TypeScript** | — |
| 构建 | **Vite 7** | 比 AntD Pro 默认的 UmiJS 更轻；二选一时取 Vite |
| UI 组件 | **Ant Design 5 + Ant Design Pro** | 国内企业中后台**事实标准**（蚂蚁出品）。ProComponents 一族：`ProTable`（分页/筛选/排序/工具栏/批量/导出一把梭）、`ProForm`（表单联动/校验/分步）、`ProLayout`（权限菜单布局） |
| 客户端状态 | **Zustand** | UI 态（主题/侧栏/弹窗开关等） |
| **服务端状态** | **TanStack Query (React Query)** | 表格数据/缓存/失效重取，套在 Axios 上。**不让 Zustand 扛 server state**。（AntD Pro 自带 `useRequest`/ahooks 亦可，但 TanStack Query 更通用，二选一） |
| HTTP | **Axios** | — |
| 图标 | **@ant-design/icons** | 与 AntD 配套 |

> 核心规范：**客户端状态（Zustand）与服务端状态（TanStack Query）分治**——这是 React 数据密集中后台的头号实践。

**路线说明（为何选 AntD Pro 而非 shadcn/ui）**：本平台 Admin 是**内部数据密集中后台**（号池/模型目录/配额/用量/审计——清一色密集表格+表单+仪表盘，使用者是平台工程师/财务/团队 leader 的工具型后台，非对外产品）。这正是 Ant Design Pro 的主场：`ProTable`/`ProForm` 把 CRUD 脚手架开箱封装，工程量最小。曾考虑的 `shadcn/ui + TanStack Table` 是「现代可组装」路线，定位是对外 SaaS 产品 / 强前端团队 / 求独特设计，headless 需手工拼 data-table，对几十张表格的内部后台是场景错配，故否决。

### 6.11 待定的最佳实践取舍（记入开放问题）

1. **审计/用量是否上分析库**：若记**每次 LLM 调用**做审计分析，百万行级分析查询会拖垮事务 Postgres（Langfuse/Helicone 都把这类数据分到 ClickHouse）。**需从第一天规划**——确认现成 Postgres 的写入/查询余量，否则审计走单独存储。

### 6.12 国际化（i18n）策略（G16）

> **决策（G16）：展示与逻辑分离，按文本产生位置三七开。** 后端是语言无关的逻辑层，前端是展示语言层——但业内实践不是「后端全程不碰语言」，而是按文本的**产生位置**和**消费者**做三七开。本节给出完整策略，下游 schema 影响（`sys_user.preferred_locale` + `sys_menu` i18n key）落在数据模型设计文档。

#### 6.12.1 核心判定规则

> 文本若由前端展示给浏览器用户，后端只发**稳定标识符**（code/key），前端译；文本若由后端直接送达终端（邮件/IM/短信），或在后端深处产生且前端反查不划算（Pydantic 校验），后端译。

| 文本类别 | 谁翻译 | 后端发什么 | 出处 |
|---|---|---|---|
| 枚举值（状态/类型） | **后端单源 + 同步** | 运行时只发 StrEnum code；label 由后端 `locales/{lang}/enums.json` 定义，codegen 同步前端 | Django `TextChoices`（label 跟 code 走）；服务端导出需消费 label |
| 业务错误 | **前端** | RFC 9457 `code` + 结构化 `params` | RFC 9457；FastAPI 官方讨论（tiangolo：i18n 归前端） |
| Pydantic 422 校验错误 | **后端** | 本地化后的 `msg` | pydantic-i18n / fastapi-validation-i18n |
| 菜单/按钮名 | **前端** | i18n key（`menu.system.user`） | Ant Design Pro + RuoYi-Vue |
| 字段/表单 label | **前端** | （前端静态资源） | 前端 i18n（key 组织见 6.12.5 ⑥） |
| 后端外发消息（邮件/IM/通知） | **后端** | 已渲染的本地化正文 | Flask-Babel `force_locale` |

**反字典表声明**：不建 `sys_dict_type`/`sys_dict_data` 表，不出 `/system/dict` 接口。RuoYi 字典表是 Java 生态为**规避「改枚举要重新发版」**而生的产物（Java 无优雅的 enum + i18n 集成）；Python 侧用 `StrEnum` 定义 code、`locales/{lang}/enums.json` 定义 label，二者皆后端单源，经 OpenAPI schema → 前端 codegen 同步给前端 `valueEnum`。语义与字典表等价但无运行时表、无额外接口。（与数据模型 0.5 节「枚举怎么存」一致。）

> **为何 label 后端单源（而非前端 i18n）**：调研 RuoYi/jeecg-boot（Java=DB 字典表）vs Django/DRF（Python=代码枚举 `TextChoices`，label 跟 code 走、`get_FOO_display()` 取）后定案——本平台是 Python 栈，随 Python 惯例让 label 紧贴 code。**决定性触发点：服务端导出（6.4/6.12.4）**需把枚举列 code→label 写进 Excel/CSV，后端必须能拿到 label；若 label 仅存前端 i18n，导出器无从取值。故枚举 label 升级为后端单源 + codegen 同步，**导出器与前端共享同一份译文，零漂移**。纯展示文本（字段 label/菜单/业务错误）仍归前端 i18n——只有枚举 label 因导出这第二个消费者而上提。

#### 6.12.2 后端：locale 解析与上下文

**请求 locale 分层解析**（优先级从高到低）：

```
显式 query 参数 (?lang=en)              # 调试/强制覆盖
  → Cookie (lang=en)                    # 前端持久化选择
  → Accept-Language 头                  # 浏览器默认
  → 登录用户 sys_user.preferred_locale   # 用户偏好(已认证时)
  → 系统默认 "zh-CN"
```

用 FastAPI middleware 解析一次，写入 `contextvars.ContextVar`，实现**请求级隔离**（协程安全，不串号）：

```python
# shared/i18n/context.py
from contextvars import ContextVar

_current_locale: ContextVar[str] = ContextVar("locale", default="zh-CN")

def get_locale() -> str:
    return _current_locale.get()

def set_locale(locale: str) -> None:
    _current_locale.set(_normalize(locale))   # 归一化 + 白名单回退
```

> FastAPI 社区 i18n 标准做法是 middleware + ContextVar，避免把 `request` 透传到每一层。

**翻译栈选型（已定：JSON/YAML + 自研轻量 `I18n` 类）**：

```
locales/
  zh-CN/
    errors.json       # 业务错误码 → 模板
    messages.json     # 邮件/IM 通知模板
    validation.json   # Pydantic 字段错误
    enums.json        # 枚举 label(后端单源)→ codegen 同步前端 + 服务端导出消费
  en-US/
    errors.json ...
```

```python
class I18n:
    def t(self, key: str, locale: str | None = None, **params) -> str:
        locale = locale or get_locale()
        template = self._catalog[locale].get(key) \
                   or self._catalog[self.default].get(key) \
                   or key                       # 三级回退
        return template.format(**params)        # 命名占位插值
```

**何时升级到 Babel + gettext**：出现复数规则（英语 1 item / 2 items）、日期/货币/数字本地化、或第三种语言时，转 Babel（`.po`/`.mo` + `pybabel` CLI + `force_locale()`）。当前双语不需要，留作演进路径。Babel/gettext 是 Python i18n 工业标准；中等规模 JSON 自研是公认的轻量替代，差异点正是复数/CLDR 本地化。

#### 6.12.3 业务错误：RFC 9457 Problem Details

**统一错误响应契约**——所有业务错误用 `application/problem+json`，前端按 `code` 查表插值：

```json
{
  "type": "https://errors.example.com/quota-exceeded",
  "title": "Quota Exceeded",
  "status": 429,
  "code": "QUOTA_EXCEEDED",
  "detail": "Monthly token quota exceeded",
  "params": { "limit": 1000000, "used": 1000500, "period": "monthly" },
  "trace_id": "..."
}
```

- `type` / `code`：**稳定、不带语言**——前端 i18n 的查表键。
- `title` / `detail`：英文兜底（给开发者看日志/Postman，非面向终端用户）。
- `params`：**结构化参数**，前端插值进本地化模板（`已用 {used}/{limit}，{period} 配额耗尽`）。
- `trace_id`：贯穿网关一次请求链路的关联 id（access/error 日志、Redis 计数 key 用它串排查；本平台不建 `call_audit` 审计表，仅作跨日志排查关联键，详见数据模型第 5 批）。

**错误码目录**：建一份集中的 `ErrorCode(StrEnum)`（数据模型第 5 批已定义，见 0.5 枚举节），每个码含 code 常量、默认 HTTP status、`params` 字段约定（强制行内注释说明每个参数语义）。前后端共享：前端 i18n 资源按这份目录建 key。

```python
class ErrorCode(StrEnum):
    QUOTA_EXCEEDED    = "QUOTA_EXCEEDED"     # 配额耗尽; params: limit/used/period/metric
    UPSTREAM_TIMEOUT  = "UPSTREAM_TIMEOUT"   # 上游超时; params: provider/timeout_ms
    NO_HEALTHY_KEY    = "NO_HEALTHY_KEY"     # 号池无可用 key; params: model/provider
    MODEL_NOT_GRANTED = "MODEL_NOT_GRANTED"  # 用户无该模型授权; params: model
    # ...
```

> RFC 9457（Problem Details for HTTP APIs，取代 RFC 7807）；`fastapi-problem-details` 库可零配置接入。错误码目录化是可观测性/前端协作的共识做法。

**Pydantic 422 校验错误（后端译的例外）**：请求体校验错误由 FastAPI 在路由层抛出，前端按 `loc` 反查不划算——后端 exception handler 翻译后返回：

```python
@app.exception_handler(RequestValidationError)
async def validation_handler(request, exc):
    errors = [{
        "field": ".".join(str(x) for x in e["loc"][1:]),
        "msg": i18n.t(f"validation.{e['type']}", **e.get("ctx", {})),  # 后端译
    } for e in exc.errors()]
    return problem_json(code="VALIDATION_ERROR", status=422, errors=errors)
```

> pydantic-i18n / fastapi-validation-i18n 正是为此而生——把 Pydantic 的 `error['type']` 映射到本地化模板。

#### 6.12.4 后端外发消息（后端译的例外）

邮件、企微/IM 推送、短信通知没有前端那层，后端必须渲染本地化正文。locale 取自**收件人** `sys_user.preferred_locale`（**不是请求发起者**）：

```python
def send_quota_alert(user: User, used: int, limit: int):
    body = i18n.t("messages.quota_alert",
                  locale=user.preferred_locale,   # 显式指定收件人语言
                  used=used, limit=limit)
    notifier.send(user, body)
```

> Flask-Babel `force_locale()` 上下文管理器即为「按收件人而非请求者翻译」场景设计。

**报表导出同属此例外，且导出在服务端做（非前端 ProTable 浏览器导出）。** 理由：`usage_record` 这类统计常是几万~百万行的全量导出，前端 ProTable 仅能导出当前页/已加载数据且大数据量会卡死浏览器；服务端导出能流式吐全量、统一控权限/脱敏、并复用 `I18n` 渲染本地化表头。locale 取**请求发起者**（导出是请求者自己看的，与外发消息取收件人不同）——从请求 `LocaleMiddleware` 注入的 `get_locale()` 取值，这正是 6.12.2 那套 locale ContextVar 在 API 响应体（纯 code）之外的**第一个正当消费者**。前端 ProTable 的「导出」按钮只触发下载，文件由后端生成。

> **【硬约束】CSV/Excel 公式注入转义。** 导出的每个字符串单元格，若首字符是 `=` `+` `-` `@` 或 `\t`(0x09) `\r`(0x0D)，必须前置 `'`（单引号）或制表符强制文本——否则用户把昵称改成 `=cmd|'/c calc'!A1` 之类，管理员用 Excel/WPS 打开导出文件即触发**命令执行/数据外泄**（OWASP CSV Injection）。实现导出时配一个 ~5 行纯函数 + 单元测试，禁止裸写单元格。值不经此转义直接落表 = 阻塞级缺陷。

#### 6.12.5 前端（Ant Design Pro / umi + react-intl）协作约定

**① 枚举 valueEnum 动态构建**——后端发 code，前端 ProTable/ProForm 用 `valueEnum` 映射 code → 本地化 label：

```tsx
// ✅ 在组件内/useMemo 里构建,切语言会重渲染
const statusEnum = useMemo(() => ({
  active:   { text: intl.formatMessage({ id: 'status.active' }),   status: 'Success' },
  disabled: { text: intl.formatMessage({ id: 'status.disabled' }), status: 'Default' },
}), [intl]);
```

> **枚举 label 译文来自 codegen,不手写**：`status.active` 等枚举 label key 由后端 `locales/{lang}/enums.json` 单源定义,经 codegen 生成到前端 locale 文件(见 6.12.1 反字典表声明)。前端只**消费**(`valueEnum` 渲染),不维护枚举译文——与服务端导出共享同一份,零漂移。字段/菜单 label 仍前端手写(见 ⑥)。

**② 动态菜单 i18n**——后端 `sys_menu` 返回 i18n key，前端路由 `name` 经 `formatMessage` 译：

```tsx
menuDataRender: (menus) => menus.map(m => ({
  ...m,
  name: intl.formatMessage({ id: m.name, defaultMessage: m.name }),  // m.name = 'menu.system.user'
}))
```

> soybean-admin / vben-admin / RuoYi-Vue3 动态菜单 i18n 均为此模式。

**③ 错误拦截器统一插值**——umi `request` / Axios 响应拦截器统一处理 `problem+json`，按 `code` 查 i18n 模板、用 `params` 插值：

```tsx
errorHandler: (error) => {
  const { code, params } = error.response?.data ?? {};
  const msg = code
    ? intl.formatMessage({ id: `error.${code}`, defaultMessage: code }, params)
    : intl.formatMessage({ id: 'error.unknown' });
  message.error(msg);
}
```

**④ ⚠️ 反模式（强制规避）**——**禁止在模块顶层 / 组件外调用 `formatMessage()` / `intl.formatMessage()`**：

```tsx
// ❌ label 在模块加载时固化,切语言不重渲染
const columns = [{ title: intl.formatMessage({ id: 'user.name' }) }];

// ✅ 在组件内/hook 里构建
const columns = useMemo(() => [
  { title: intl.formatMessage({ id: 'user.name' }) }
], [intl]);
```

> 业内最高频生产事故：顶层调用导致「切了语言但表头/枚举不变」。

**⑤ locale 传递**——前端选定语言后：① 存 Cookie/localStorage 持久化；② 每个请求带 `Accept-Language`（或 `?lang=`），让后端外发消息/校验错误对齐；③ 登录后同步到 `sys_user.preferred_locale`。

**⑥ i18n key 组织约定（防 key 爆炸）**——字段/表单 label 全在前端，`name`/`status`/`createdAt` 这类字段在几十张表反复出现，若每表每字段都建独立 key 会 key 爆炸 + 译文重复。业内一致解法是**两层命名空间**，既不全共享也不全独立：

| 层 | 放什么 | key 形态 | 共享性 |
|---|---|---|---|
| `common.*` | 通用 UI 操作 + 真正无处不在的字段 | `common.add` / `common.status` / `common.createdAt` / `common.action`（操作列） / `common.remark` | **共享**，一处译文 |
| `pages.{模块}.{字段}` | 领域特定字段 | `pages.user.employeeNo` / `pages.channel.apiBase` | **不共享**，各模块自有 |

**反直觉点（关键）**：直觉是「`name` 到处都有 → 全共享一个 key」（DRY），**但业内明确反对过度共享**。FormatJS 官方：「消息是高度上下文相关的」。屈折语（法/德）里同词按语境译文不同——`name` 作人名 vs 模型名 vs 主机名，法语分别是 `Nom` / `Nom de modèle` / `Nom d'hôte`，共享一个 key 会逼译者只能选一个、其余皆错。故 react-admin 的**阈值规则**：一个 key 只有在 **3+ 个不相关模块**都用到才提升到 `common.*`，否则留模块内。

**对本平台的校准（中文语境可更激进共享）**：① 中文**无性/数/格屈折**，上述屈折语陷阱基本不存在（`name`→恒为「名称」，`status`→恒为「状态」），故通用字段共享比欧语 app 更安全；② 本后台表是**有界的**（约 10 张：`sys_user`/`sys_menu`/`channel_key`/`logical_model`/`quota`…），即便全走 per-module 也不爆炸。

落地规则（前端开发遵此）：
1. **`dataIndex` 直接 = key 末段**——`dataIndex: 'apiBase'` → `id: 'pages.channel.apiBase'`，无脑映射，免思考。
2. **通用列（状态/创建时间/操作/备注）走 `common.*`**——中文语境下安全，放心共享。
3. **拿不准先放模块内**，真到 3+ 模块复用再提升 `common.*`（防 `common` 沦为「谁都不敢清的垃圾抽屉」，react-admin 原话）。
4. **始终带 `defaultMessage`**——译文缺失时运行时兜底，且给译者上下文。

文件结构对齐 Ant Design Pro 官方 locale 切分（`common.ts` / `menu.ts` / `pages/*.ts` / `component.ts`）：

```
src/locales/{lang}/
  common.ts       # 共享 UI 操作 + 无处不在字段(add/edit/delete/status/createdAt/action/remark...)
  menu.ts         # 路由 name → 菜单 label(对应 sys_menu.name 的 i18n key)
  pages/          # 按模块拆领域字段
    user.ts       # pages.user.employeeNo / pages.user.preferredLocale ...
    channel.ts    # pages.channel.apiBase / pages.channel.protocol ...
  component.ts    # 共享 UI chrome(弹窗/抽屉/表头)
  error.ts        # error.{ErrorCode}(对接 RFC 9457 业务错误码,见 6.12.3)
```

> 出处：Ant Design Pro 官方 `src/locales` 结构；vben-admin `common.ts` 提取（commit `efbde0d`）；soybean-admin 顶层 `common` 命名空间；FormatJS issue #2321「messages are contextual」；react-admin 翻译文档「3+ 阈值 + 勿让 common 变垃圾抽屉」。

#### 6.12.6 数据模型影响（仅两处，详见数据模型设计文档）

1. **`sys_user` 加 `preferred_locale`**（`String(16)`，默认 `"zh-CN"`，BCP 47 格式）——后端外发消息译文用 + 登录后前端默认语言。
2. **`sys_menu.name` 语义锁定为 i18n key**（如 `menu.system.user`，前端必译）+ 新增开发可读 `remark` 列（DB 里也能看懂这菜单是啥，给后端开发者）。

#### 6.12.7 决策摘要

| # | 决策 | 选择 |
|---|---|---|
| 原则 | 后端逻辑层 / 前端展示层，按文本产生位置三七开 | ✅ |
| 字典表 | RuoYi `sys_dict` 表 + `/system/dict` 接口 | ❌ 不做（枚举+codegen 替代） |
| 枚举 label | 后端单源 `locales/{lang}/enums.json` + codegen 同步前端（Django `TextChoices` 惯例；服务端导出需消费 label） | ✅ |
| 业务错误 | RFC 9457 `problem+json` + `code`+`params`，前端插值 | ✅ |
| 错误码目录 | `ErrorCode(StrEnum)` 集中定义（数据模型第 5 批，无新表） | ✅ |
| 校验错误 | 后端 exception handler 译（例外） | ✅ |
| 外发消息 | 后端 `I18n` 按收件人 locale 译（例外） | ✅ |
| 报表导出 | 服务端生成（非前端 ProTable），locale 取请求者 `get_locale()`；CSV 公式注入转义为硬约束 | ✅ |
| 翻译栈 | JSON/YAML 自研 `I18n` 类（双语够用，Babel 留演进） | ✅ |
| 菜单存法 | `name` = i18n key + `remark` 开发备注 | ✅ |
| 字段 label key 组织 | 两层命名空间：`common.*`（通用，中文语境激进共享）+ `pages.{模块}.{字段}`（领域），`dataIndex` = key 末段，3+ 复用才提升 common | ✅ |
| 前端反模式 | 禁止组件外 `formatMessage` | ✅ |
| schema 改动 | `sys_user.preferred_locale` + `sys_menu.name` 语义 + `sys_menu.remark` | ✅ |

---

## 七、与 A 版设计文档的关系（待回填）

A 版（Hermes 桌面端管控）作为本平台的客户端，其第三章「后台管理系统」需按以下调整 —— **A 版的后台内容实际被本平台取代**，A 版只保留端侧（**本文定稿后再改 A 版**）：

1. **3.1～3.3 后台服务整体**：A 版自带的认证/网关/MCP 服务设计，全部由**本平台提供**。A 版从「自建后台」改为「消费本平台」：provider 插件指向本平台 LLM 网关，`mcp.json` 指向本平台的 MCP 服务器。
2. **3.4 管理后台**：A 版无需自建；用本平台管理后台。
3. **整体定位**：A 版从「以 Hermes 为中心、自带后台」改为「Hermes 端侧 + 消费统一 AI 基础设施平台」。
4. **MCP 工具实现**：A 版的 OA 工具（`oa_submit_flow` 等）是**本平台 MCP 服务器内的工具代码**（自研），工具内部调 OA 系统 REST API；归属本平台 `mcp/` 模块（G11/第四之五）。

---

## 八、悬而未决点（下一步讨论）

> 早期「感觉怪怪的」三个结（范围过界 / MCP 尺度错配 / 网关双重身份）已厘清并关闭：
> - **范围**：本就该是公司级 AI 基础设施，非副产品 —— 不存在过界。
> - **MCP 尺度**：之前误把 MCP 当「OA 业务实现」，又一度误当「纳管/聚合外部 MCP 服务」；实为**平台自身实现一个标准 MCP 服务器**，工具是平台内代码（G11，第四之五），与 LLM 网关同属平台能力。
> - **双重身份**：平台纯粹是「公司 AI 入口」，Hermes 仅为平级客户端之一，无耦合。
>
> 以下为仍待定的设计细节。

1. **共享 DB 的边界**：三类容器角色共享一个 Postgres，是模块化单体的标准做法；但若将来某组件（如 mcp-gateway）想独立演进 / 独立团队维护，共享 DB 会成为耦合点。现在是否预留 schema 隔离（分 schema / 分库就绪）？

2. **per-key 作用域**：`sk-key` 是否支持限模型 / 设子预算？做了更「完整」，但有工程量。**未决。**

3. **一个 repo 的内部模块边界（已出方向，待细化）**：第六章已定「按领域分包、模块间只 import 对方 `schemas` 不 import `models`、`main.py` 按角色挂载 router」。**仍待细化**：`shared/` 具体放哪些、各领域包的 service 边界、`gateway/router_builder.py` 的职责范围。

4. **核心数据模型（部分落地）**：号池/模型目录/模型分配三层已出 schema 草案（第四之三）。**仍待落地**：用户 / key 凭据表、配额/用量表的完整 schema。降级标记 `downgraded_features`（G13）落在 `usage_record`。**不建调用审计表**（`call_audit`）——`usage_record` 已覆盖审计主要价值，源 IP/正文/MCP 调用是合规边际增量、非 v1 必需，将来需合规取证再加 append-only 新表。MCP 模块**无新表**且**平台侧不留痕**（复用用户/key，下游第三方是业务真相源，G11/第四之五）。错误码目录 `ErrorCode(StrEnum)`（第 5 批，无新表）。

5. **审计/用量是否上分析库（G14 引入）**：若记每次 LLM 调用，百万行级分析查询可能拖垮事务 Postgres。需确认现成 Postgres 写入/查询余量，否则审计走单独存储（ClickHouse 或独立库）。**需从第一天规划，未决。**

6. **litellm 直通异常 bug（#20507）的应对**：依赖 `litellm.Router` 做容灾时，Anthropic 直通路径的异常类型退化会破坏自动重试。是自建异常分类外层，还是接受首版无 Anthropic 直通容灾？**未决。**

> **已关闭**：逻辑模型方案选型（→ 方案 B，G12）；多 key 号池（→ 是，坍缩为 Router deployment，G12）；协议特性匹配（→ B+D 软过滤+降级标注，G13）；技术栈最佳实践（→ 对标 LiteLLM，全异步+按领域分包，G14）；前后端分离 + 前端栈（→ React 19 + Ant Design Pro + TanStack Query，G15）；国际化策略（→ 展示/逻辑三七开，RFC 9457 错误 + JSON 自研翻译栈 + 前端 i18n 拥有展示文本，G16，详见 6.12）；**MCP 接入细节（→ 平台自身即标准 MCP 服务器，工具是平台内代码，鉴权复用 JWT/sk-key，业务授权交下游第三方，零新表，G11，详见第四之五）**。

---

## 九、下一步候选

- **优先**：共享数据模型（第八章 4）—— 用户 / key / 模型分配 / 配额 / 用量 / 审计，把前面所有讨论落成 schema（MCP 模块无新表，复用以上）。
- 之后：repo 内部模块边界细化（第八章 3）→ 认证服务企微对接细节 → 审计存储定夺（第八章 5）。
