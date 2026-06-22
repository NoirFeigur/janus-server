# Hermes 企业桌面端集中管控部署 — 详细设计文档

> 版本：v0.2（评审稿）
> 日期：2026-06-11
> 范围：基于 hermes-agent fork 的企业桌面端分发方案：端侧 Agent + 中心化控制面
> 读者：后台管理系统开发团队、Hermes fork 维护者、运维
> 建设顺序：**后台管理服务先行**（认证/网关/MCP 是 Hermes 端侧改造的前置支撑，详见第八章依赖关系）

---

## 一、背景与目标

把 Hermes Agent 以桌面应用（exe/MSI）形式下发到每位员工电脑，员工一键安装、企微扫码登录即可使用「拉普拉斯 OA 流程助手」。所有资源中心化管控：

- 员工**不需要**填写任何 API 密钥
- 使用哪个大模型由**后台统一分配**，LLM 请求经**公司网关统一中转**
- skills 随客户端版本**统一下发**；MCP 能力由**后台服务端提供**
- 支持**自动更新**（Agent 代码 + Electron 壳双轨）
- Agent 本体运行在**员工本机**（保留本机终端/文件/浏览器操作能力，记忆天然按人隔离）

### 已确认的设计决策

| # | 决策点 | 结论 |
|---|---|---|
| D1 | Token 机制 | 双 token（access + refresh）；LLM 网关与 MCP **共用同一 token 体系** |
| D2 | 端侧权限 | **保留 Hermes 默认配置**（危险命令需用户确认等），不做中心策略下发 |
| D3 | 配置归属 | 基于 `profile_distribution` 划分：公司管 provider/mcp.json/SOUL.md/skills；员工保留偏好/记忆/会话 |
| D4 | 网络边界 | 员工机可访问公网（PyPI/Python 工具链/npm 走**官方源**）；代码仓库走**内网 Git**（开放匿名拉取）；模型厂商调用仅经后台网关 |
| D5 | 审计 | 网关侧做**按人配额 + 流量统计**；**不做**内容级审计 |
| D6 | 壳更新通道 | 内网静态更新服务器（electron-updater generic provider）|

---

## 二、总体架构

```
┌──────────── 员工 PC（每人一套）────────────┐        ┌──────────── 公司后台管理系统（新建）────────────┐
│  Hermes Desktop（Electron 壳）             │        │  ① 认证服务：企微扫码 OAuth → 双 token          │
│   ├─ 首启 bootstrap（内网安装 Python 后端）│        │  ② LLM 网关：OpenAI 兼容中转                    │
│   └─ Hermes Agent（Python，本机进程）      │◄──────►│     · 按用户分配模型 · 配额 · 流量统计          │
│       ├─ 本机工具：terminal/file/browser   │ Bearer │  ③ MCP 服务：OA 流程工具（提交/待办/审批）      │
│       ├─ skills：随代码版本内置            │ token  │  ④ 管理后台：用户/模型分配/配额/用量报表        │
│       ├─ mcp.json → 指向后台 MCP           │        │  ⑤ Electron 更新服务器（静态文件）              │
│       ├─ SOUL.md：OA 助手人设（分发资产）  │        └─────────────────────┬───────────────────────────┘
│       └─ 记忆/会话：本机存储（按人隔离）   │                              │ 仅后台可出公网
└────────────────┬───────────────────────────┘                              ▼
                 │ 安装 / 更新                                       模型厂商 API
                 ▼
   内网 Git 仓库（开放匿名拉取）；PyPI / Python 工具链 / npm 依赖走公网官方源
```

### 职责划分

| 层 | 组件 | 谁建设 |
|---|---|---|
| 控制面 | 认证服务、LLM 网关、MCP 服务、管理后台、更新服务器 | 后台团队（新建）|
| 执行面 | Hermes Agent + Desktop 壳（fork 改造，后续从 main 新切开发分支）| Hermes fork 维护者 |
| 分发面 | 内网 Git 仓库、MSI 下发（依赖项均走公网官方源）| 运维 |

**核心原则**：
1. **OA 系统凭据永远不落员工电脑** —— 端侧只持有用户身份 token，OA 操作由后台 MCC 鉴权后代办
2. **模型分配在服务端强制** —— 客户端声明的模型仅作参考，网关有最终裁决权
3. **公司资产随版本走** —— skills/SOUL.md/mcp.json/provider 配置由更新原子下发，员工个人数据永不触碰

---

## 三、后台管理系统接口契约

### 3.1 认证服务（企微扫码 + 双 token）

#### 登录流程

```
桌面客户端                    认证服务                     企业微信
   │ ① GET /auth/wecom/qr        │                            │
   │──────────────────────────► │ 生成 state，返回二维码URL    │
   │ ② 展示二维码（内嵌webview） │                            │
   │                             │ ◄──── ③ 员工扫码确认 ───── │
   │ ④ 轮询 GET /auth/wecom/poll │                            │
   │──────────────────────────► │ ⑤ code→userid，签发双token  │
   │ ◄── {access, refresh, user} │                            │
   │ ⑥ token 落盘 HERMES_HOME    │                            │
```

#### 接口定义

| 接口 | 方法 | 说明 |
|---|---|---|
| `/auth/wecom/qr` | GET | 返回 `{state, qr_url, expires_in}`。基于企微「扫码授权登录」（corp OAuth）|
| `/auth/wecom/poll?state=` | GET | 长轮询。完成时返回 `{access_token, refresh_token, user: {userid, name, department}}` |
| `/auth/refresh` | POST | body `{refresh_token}` → 新双 token（refresh rotation：旧 refresh 即刻作废）|
| `/auth/logout` | POST | 注销 refresh token |

#### Token 规格（D1）

| 项 | access_token | refresh_token |
|---|---|---|
| 形态 | JWT（含 userid、department、过期时间）| 不透明随机串，服务端存储 |
| 有效期 | 2 小时（建议）| 30 天（建议）|
| 用途 | LLM 网关、MCP 服务**共用**，`Authorization: Bearer <access>` | 仅 `/auth/refresh` |
| 刷新 | 客户端收到 401 时静默刷新重试；刷新失败 → 弹出扫码页 | rotation，防重放 |

> JWT 让网关和 MCP 各自本地校验签名即可（共享公钥），无需每请求回源认证服务。

### 3.2 LLM 网关（OpenAI 兼容中转）

#### 接口

| 接口 | 说明 |
|---|---|
| `POST /v1/chat/completions` | OpenAI 兼容（**必须支持 SSE streaming** —— Hermes 主循环依赖流式）。透传 `tools`/`tool_calls`/`reasoning` 等字段 |
| `GET /v1/models` | 返回**当前用户被分配的**模型列表（管理后台配置）|

#### 模型分配与强制（服务端裁决）

```
请求进入 → JWT 校验 → 取该用户的模型分配表
  ├─ 请求 model 在分配表内 → 路由到对应上游厂商
  ├─ 请求 model 不在分配表内 → 改写为该用户的默认模型（或 403，建议改写+响应头标注）
  └─ 上游凭据（厂商 API key）只存在于网关，永不下发
```

#### 配额与统计（D5）

- **计量维度**：user × model × 日，记录 `prompt_tokens / completion_tokens / 请求数 / 流式时长`（从上游响应 usage 字段取）
- **配额**：日/月 token 上限（按人或按部门），超限返回 `429` + JSON 错误体 `{"error": {"code": "quota_exceeded", "message": "今日额度已用完，明日恢复"}}` —— Hermes 会把错误文本呈现给用户
- **不做内容级审计**：请求/响应 body 不落库；仅元数据（时间、用户、模型、token 数、状态码）
- 管理后台出用量报表（按人/部门/模型/时间）

### 3.3 MCP 服务（OA 能力）

- 协议：**MCP Streamable HTTP**（Hermes 内置 MCP 客户端原生支持），端点如 `https://ai-admin.laplace.local/mcp`
- 认证：同一 access token（`Authorization: Bearer`），MCP 服务从 JWT 取 userid，**以该员工身份**调 OA 系统 —— 权限边界在服务端
- 建议首批工具（与 SOUL.md 职责对应）：

| 工具 | 入参（示意） | 行为 |
|---|---|---|
| `oa_list_flow_types` | - | 可发起的流程类型及表单 schema |
| `oa_submit_flow` | `{type, fields}` | 以当前用户身份发起流程，返回流程号 |
| `oa_list_todos` | `{status?}` | 当前用户待办列表 |
| `oa_get_flow_detail` | `{flow_id}` | 流程详情（仅本人可见范围）|
| `oa_approve` | `{flow_id, action: approve\|reject, comment?}` | 审批操作 |

- MCP 工具的新增/修改**全部在服务端**，客户端无需更新即获得新能力（mcp.json 只记端点）

### 3.4 管理后台功能清单

#### 模块一：用户与身份管理
- 企微通讯录同步（定时 + 手动触发），组织架构树展示
- 用户状态管理：启用 / 禁用 / 离职冻结（禁用即吊销其全部 refresh token，access 随 2h 有效期自然失效）
- 在线会话管理：查看用户当前有效 token 列表，支持单个/全部强制下线
- 用户详情页：所属部门、模型分配、配额使用、最近活跃时间、客户端版本

#### 模块二：模型与配额管理
- 上游厂商接入管理：厂商凭据（API key）录入与轮换、连通性测试、启用/停用
- 模型目录：维护可用模型清单（模型名、上游厂商、上下文长度、计价系数、状态）
- 分配策略：按**部门**批量分配 + 按**用户**个别覆盖；每个分配单元设默认模型
- 配额策略：日/月 token 上限（部门级默认 + 用户级覆盖），超限行为固定为 429
- 策略变更即时生效（网关每请求实时查分配表，或短 TTL 缓存 ≤ 60s）

#### 模块三：用量统计与报表
- 维度：用户 × 模型 × 日（prompt/completion tokens、请求数、失败数、平均/最大时延）
- 视图：部门汇总、人员排行、模型分布、趋势曲线；支持时间区间筛选与 CSV 导出
- 配额预警：用户达 80%/100% 时管理端标记（可选：企微通知管理员）
- 明确不存储请求/响应内容（D5）

#### 模块四：MCP 工具管理
- 工具注册表：工具名、入参 schema、对接的 OA 接口、启用状态、版本说明
- 工具级开关：单个工具可全局停用（如 OA 接口维护期间），MCP 列表接口动态反映
- 调用统计：工具 × 用户 × 日的调用次数与成功率（仅元数据，不存业务内容）

#### 模块五：客户端版本管理
- 版本看板：从网关请求头 `X-Hermes-Version` 被动收集，展示各版本安装占比
- 发布记录：Agent 代码（release 分支 tag）与 Electron 壳（更新服务器上的 latest.yml）的版本台账
- 最低版本策略（可选）：网关可配置拒绝低于某版本的客户端（响应固定错误文案引导更新）

#### 模块六：系统管理
- 管理员账号与角色（至少区分：超级管理员 / 只读报表角色）
- **管理操作审计日志**：谁在何时改了谁的模型分配/配额/状态（管理侧操作要留痕，与 D5 的"不审计员工对话内容"不冲突）
- 认证服务配置：JWT 签名密钥轮换、token 有效期参数
- 企微应用配置：corpid/secret 管理、回调地址

### 3.5 Electron 更新服务器（D6）

- 形态：**纯静态文件服务器**（nginx 即可），electron-builder *generic provider*
- 目录：`https://ai-update.laplace.local/desktop/` 下放 `latest.yml + Hermes-Setup-x.y.z.exe + *.blockmap`
- 发布动作 = 把 `npm run dist:win` 产物拷上去；客户端后台自动检测、差量下载、提示重启安装

---

## 四、Hermes Fork 改造清单

> 开发分支：后续从 `main` 新切专用分支（现有 wecom 网关分支不复用于本项目）。
>
> **前置依赖（后台先行）**：本章所有改造都消费后台服务的接口——provider 插件依赖 LLM 网关（3.2）、扫码登录依赖认证服务（3.1）、oa-workflow skill 依赖 MCP 服务（3.3）。**后台管理服务必须先建到可联调状态**（至少 M1 完成），Hermes 端侧改造才能开工并验证；在此之前 fork 侧只能做不依赖后台的 4.1（仓库地址）。详见第八章的阶段依赖。

### 4.1 `scripts/install.ps1` 仓库地址修改

| 改动 | 现状 → 目标 |
|---|---|
| 仓库地址 | `github.com/NousResearch/hermes-agent.git` → `git.laplace.local/ai/hermes-agent.git`（匿名拉取）|

**仅此一处**。PyPI 依赖、Python/uv/Git/ripgrep 工具链、npm 包均维持公网官方源不动（D4），员工机网络可直达。

构建安装包时 `write-build-stamp.cjs` 自动把**当前 commit** 写入 `install-stamp.json`，首启 bootstrap 会 checkout 该精确 ref —— 版本一致性由此保证，无需额外机制。

### 4.2 公司 model-provider 插件（`plugins/model-providers/laplace/`）

```python
# __init__.py（示意）
from providers import register_provider, ProviderProfile

register_provider(ProviderProfile(
    name="laplace",
    base_url="https://ai-gw.laplace.local/v1",
    api_key_resolver=read_company_access_token,   # 从 HERMES_HOME/company-auth.json 读
    # 模型列表动态来自 GET /v1/models（按用户分配）
))
```

- `api_key_resolver` 每次取**当前有效的 access token**；配 401→refresh→重试 的拦截逻辑（插件内实现或网关 SDK 提供）
- 分发配置把 `model.provider` 锁定为 `laplace`（见 4.4），员工 `/model` 只能在分配集合内切换（网关兜底强制）

### 4.3 桌面端扫码登录（`apps/desktop` onboarding 改造）

- 替换官方首启 onboarding 的「选 provider / 填 API key」步骤为**企微扫码页**（内嵌 webview 展示 `qr_url` + 轮询）
- 登录成功后写 `HERMES_HOME/company-auth.json`：

```json
{ "access_token": "...", "refresh_token": "...", "user": {"userid": "SZ4925", "name": "张三"}, "expires_at": 1781200000 }
```

- 文件 ACL 收紧为仅当前 Windows 用户可读（`icacls`）
- 运行期 401 处理：静默 refresh；refresh 失效 → 桌面端弹扫码窗重新登录（agent 进程不退出）
- 改造量评估：这是 fork 改造中**工作量最大的一项**（Electron 渲染层新页面 + 主进程 IPC + token 生命周期管理）

### 4.4 配置与资产分发（基于 `profile_distribution`，D3）

| 归属 | 内容 | 更新行为 |
|---|---|---|
| **公司管（distribution-owned，更新强制覆盖）** | `SOUL.md`（OA 助手人设）、`skills/`（公司技能）、`mcp.json`（指向后台 MCP）、config 中 `model.provider=laplace` 等公司项 | 随 `hermes update` 原子覆盖 |
| **员工自有（永不触碰）** | 记忆（MEMORY.md/USER.md）、会话历史、界面偏好、个人 skills（`~/.hermes/skills` 用户目录）、company-auth.json | 保留 |

- 端侧权限配置（approvals、command_allowlist 等）**保持 Hermes 默认**（D2），不在分发清单内
- 首装时由 install.ps1 的 distribution stage 落地初始资产

### 4.5 SOUL.md 与公司 skills

- `SOUL.md`：已有的「拉普拉斯 OA 流程助手」人设，进分发资产
- 公司 skills 放仓库 `skills/laplace/` 下（如 `oa-workflow` skill：何时调用哪个 MCC 工具、表单字段引导话术、错误处理）—— 随代码版本演进，更新即下发

### 4.6 自动更新双轨

| 轨道 | 内容 | 机制 | 频率 |
|---|---|---|---|
| Agent 代码 | Python 代码、skills、SOUL.md、配置分发 | `hermes update`（git pull 内网仓库发布分支 + 依赖重建），桌面端已有后台检测/一键更新入口 | 高频（日常迭代）|
| Electron 壳 | 桌面 UI、onboarding、bootstrap 逻辑 | electron-updater → 内网静态服务器（3.5）| 低频 |

发布流程建议：开发在专用开发分支（从 `main` 新切）→ 验证后 merge 到 `release` 分支并打 tag → 客户端 `hermes update` 跟踪 `release` 分支。安装包构建也从 `release` 出（stamp 固定到 release commit）。

### 4.7 构建流水线

```
内网 CI（或手动构建机）：
  1. checkout release 分支
  2. npm install（仓库根）
  3. cd apps/desktop && npm run dist:win        # 产出 NSIS exe + MSI
  4. （可选）企业证书签名：WIN_CSC_LINK / WIN_CSC_KEY_PASSWORD 环境变量，electron-builder 自动签
  5. 产物上传：
     - MSI → 运维下发系统（首装）
     - exe + latest.yml + blockmap → 更新服务器（自动更新）
```

> 不签名时 SmartScreen 会拦截提示；域环境可用 GPO 白名单缓解，但**建议申请企业代码签名证书**。

---

## 五、关键流程时序

### 5.1 首次安装

```
运维下发 MSI → 员工安装（仅 Electron 壳，~100MB）
→ 首启 bootstrap：公网官方源装 Python/Git 工具链 → clone 内网仓库@stamp commit → pip install（公网官方源）
→ 落地分发资产（SOUL.md / skills / mcp.json / provider 配置）
→ 企微扫码登录 → token 落盘
→ 进入聊天界面，OA 助手就绪（全程无需填任何密钥）
```

### 5.2 一次对话的调用链

```
员工提问 → 本机 Agent 组装上下文（SOUL.md + 本机记忆 + skills）
→ POST ai-gw.laplace.local/v1/chat/completions（Bearer access）
→ 网关：JWT 校验 → 配额检查 → 模型裁决 → 转发厂商 → 流式回传 + 记账
→ 模型决定调 OA 工具 → 本机 MCP 客户端 → ai-admin.laplace.local/mcp（同一 Bearer）
→ MCP 服务以该员工身份调 OA → 结果回流 → 模型生成回复
```

### 5.3 Token 过期

```
任一请求 401 → 客户端静默 POST /auth/refresh → 成功：重试原请求（员工无感）
                                            → 失败（refresh 过期/吊销）：弹扫码窗，重新登录
```

---

## 六、安全设计要点

1. **凭据边界**：厂商 API key 仅在网关；OA 系统凭据仅在 MCP 服务；员工机只有个人 token（ACL 收紧 + refresh rotation）
2. **身份即权限**：MCP 每个工具调用都从 JWT 解出 userid，OA 权限完全复用 OA 系统本身的授权体系，端侧无任何越权面
3. **吊销**：管理后台禁用用户 → 认证服务吊销 refresh + JWT 短有效期自然过期 → 2 小时内全面失效
4. **端侧能力**（D2）：保持 Hermes 默认安全配置 —— 危险命令需员工本人确认、secrets redaction 默认开启；agent 以员工本人 Windows 权限运行，不引入新提权面
5. **数据驻留**：会话与记忆全部在员工本机（隐私友好）；公司侧仅有 LLM 元数据统计（D5，无内容）

---

## 七、员工机文件布局（最终形态）

```
%LOCALAPPDATA%\hermes\
├── hermes-agent\          # 代码 checkout（内网仓库 release 分支）含内置 skills、venv
├── config.yaml            # 公司项由分发管理，其余默认
├── company-auth.json      # 双 token（ACL: 仅本人）
├── SOUL.md                # OA 助手人设（distribution-owned）
├── mcp.json               # → ai-admin.laplace.local/mcp（distribution-owned）
├── memories\              # 本机记忆（员工自有）
├── sessions\              # 会话历史（员工自有）
├── skills\                # 员工个人 skills（自有；公司 skills 在代码树内）
└── logs\
```

---

## 八、里程碑与建设顺序

### 8.1 总原则：后台先行

Hermes 端侧改造的每一项都以后台接口为前提（provider 插件→网关、扫码登录→认证服务、OA skill→MCP）。正确顺序是：**先把后台管理服务建到可联调状态（M1），再启动 Hermes fork 改造（M2 起）**。在 M1 完成前，fork 侧唯一可做的是 4.1（仓库地址，无后台依赖）；并行抢进度的空间在于：后台 M1 开发期间，fork 侧可以提前做技术预研（阅读 onboarding 代码、provider 插件骨架），但联调必须等接口就绪。

```
后台团队:  M1 认证+网关 ──► M3 MCP 服务 ────────────► M6 管理后台完善
                 │               │
Fork 侧:    （仅4.1可做）        │
                 └► M2 端侧打通 ─┴► M5 扫码登录+自动更新
运维:                        M4 安装链路 ──────────► 灰度下发
```

### 8.2 阶段表

| 阶段 | 内容 | 依赖 | 责任方 |
|---|---|---|---|
| M1 后台地基 | 认证服务（扫码+双token）、LLM 网关（中转+模型分配+配额记账）联调就绪 | 企微 corp 应用、厂商账号 | 后台 |
| M2 端侧打通 | provider 插件 + 手工放置 token 文件，本机 Agent 经网关对话成功 | **M1** | fork |
| M3 OA 能力 | MCP 服务首批 5 个工具 + `oa-workflow` skill + SOUL.md 联调 | **M1**、OA 系统接口 | 后台 + fork |
| M4 安装链路 | install.ps1 仓库地址修改、分发资产落地、构建出 MSI、干净虚拟机全流程验证 | 内网 Git 仓库 | fork + 运维 |
| M5 登录与更新 | 桌面扫码 onboarding、401 静默刷新、双轨自动更新、更新服务器 | M2、M4 | fork + 运维 |
| M6 管控完善 | 管理后台六大模块完整化（3.4）、灰度下发、企业签名 | M5 | 后台 + 运维 |

**fork 侧工作量排序**：扫码 onboarding（最大）> provider 插件 > 分发配置 > install.ps1 仓库地址 ≈ 更新接线（基本现成）。

---

## 九、开放问题（不阻塞开发，实施中确认）

1. 企微扫码用「企业自建应用网页授权」还是「企微扫码登录组件」—— 取决于你们企微管理端可申请的应用类型
2. 网关是否需要支持多上游厂商容灾（厂商 A 故障切 B）—— 建议 M6 再考虑
3. `hermes update` 的触发策略：员工手动 / 桌面端提示 / 启动时强制检查 —— 建议「启动时检查 + 提示一键更新」，重大版本可强制
4. 员工离职回收：除 token 吊销外，本机残留数据是否需要运维清理脚本（MSI 卸载 + HERMES_HOME 清除）
