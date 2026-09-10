# TencentDB-Agent-Memory → Clawith 完全集成方案（四服务部署 + 对接点）

**日期**：2026-09-07　**类型**：集成方案（生产级，7 角度自审）　**状态**：评审通过（含诚实负结论）

---

## 0. 结论先行（TL;DR）

用户诉求是「**完全集成进 Clawith**」——真正把 TencentDB-Agent-Memory 四个服务跑起来接进 Clawith（外部服务集成），而非「借鉴设计」。裁决如下：

| 服务 | 端口 | 集成裁决 | 一句话理由 |
|---|---|---|---|
| **Memory Core** | 8420 | ✅ **部署 + SDK 直连（数据面唯一入口）** | backend 加 `tencentdb-agent-memory-sdk-python`，run 结束写 L0、`build_agent_context` 前召回 L1/L2/L3 注入（对齐官方 Adapter 三件事）。 |
| **Panel UI** | 8125 | ✅ **部署 + nginx 反代** | 团队记忆管理面板，经 frontend nginx 挂 `/memory-panel/`，复用既有鉴权入口。 |
| **Knowledge**（Wiki/Code-Graph） | 8424 | ✅ **部署 + 完全对接（fork 补 LocalSourceFetcher）** | 与 Panel 同镜像（memory-hub）；Clawith 走服务端 `/v3/code-graph/*` 统一知识面（同源、同 ACL），fork 补本地目录源（§1.3）。 |
| **Proxy** | 8096 | ✅ **部署（留给外部 agent，Clawith 自身不走）** | 对 Clawith 自身冗余（SDK 已走同一数据面）；价值是让外部 coding agent 接入同一团队记忆。 |

**一句话**：四服务全部署（docker-compose 增量，复用 Clawith deepseek 端点）；Clawith 自身走 **SDK 直连 Memory Core**（写 L0/召回 L1-L3），**不经过 proxy**；Knowledge 走服务端统一数据面（fork 补 `LocalSourceFetcher`），库嵌入降为可选；proxy 留给外部 coding agent。

**诚实负结论（必须先说，详情见 §4）**：
1. **Gateway 鉴权缺口**——第①层 Bearer gate 是 default-open（未设 key 即放行）× proxy `auth.ts` 单点漏 header，二者叠加断 proxy 全栈；SDK 直连不受影响。靠四层纵深防御兜底。
2. **LocalSourceFetcher 需 fork 补齐**——统一知识面的必经一步（约 50 行类 + 一行 register），非可绕过。
3. **LLM 第三份账单**——仅 L1 抽取/summarize 低频低量；embedding 默认 `none` = BM25 零成本。
4. **记忆栈并跑非替换**——TencentDB L0-L3 与 Clawith `memory.md`/`reflections.md`/经验库并存，短期不互相替代。

---

## 1. 方案

### 1.1 部署四服务（docker-compose 增量）

在既有 `docker-compose.yml` 增量起三个容器（memory-hub 单镜像承载 panel 8125 + knowledge 8424），复用 Clawith deepseek 端点，数据落 named volume：

```yaml
services:
  tdai-memory-core:
    image: agentmemory/memory-core:v2.0.2-beta.1      # 多架构，无需登录
    restart: unless-stopped
    environment:
      TDAI_LLM_BASE_URL: ${TDAI_MEMORY_LLM_BASE_URL}   # 复用 deepseek：https://api.deepseek.com/v1
      TDAI_LLM_API_KEY: ${TDAI_MEMORY_LLM_API_KEY}
      TDAI_LLM_MODEL: ${TDAI_MEMORY_LLM_MODEL:-deepseek-chat}
      TDAI_LLM_PROVIDER: openai
      TDAI_DATA_DIR: /data/tdai-memory
      STORE_MODE: ${TDAI_STORE_MODE:-sqlite}
      # 网关 Bearer 默认关闭；生产靠网络隔离 + 不暴露宿主端口（§1.5 安全边界）
    volumes:
      - tdai-memory-core-data:/data/tdai-memory
      - ./deploy/tdai-core/tdai-gateway.yaml:/data/config/tdai-gateway.yaml:ro   # memory.* pipeline 参数 yaml-only，必须挂载
    networks: [default]

  tdai-memory-hub:                                    # panel(8125) + knowledge(8424) 合并镜像
    image: agentmemory/memory-hub:v2.0.2-beta.1
    restart: unless-stopped
    environment:
      LLM_MODE: custom                                # knowledge 直连用户端点
      LLM_PROTOCOL: openai
      LLM_BASE_URL: ${TDAI_MEMORY_LLM_BASE_URL}
      LLM_API_KEY: ${TDAI_MEMORY_LLM_API_KEY}
      LLM_MODEL: ${TDAI_MEMORY_LLM_MODEL:-deepseek-chat}
      REMOTE_INSTANCE_URL: http://tdai-memory-core:8420
      REMOTE_INSTANCE_ID: default
      REMOTE_INSTANCE_NAME: default
      KNOWLEDGE_PUBLIC_BASE_URL: http://tdai-memory-hub:8424/v3   # 必须含 /v3 前缀
      KNOWLEDGE_LLM_BINDING_SYNC: "0"
    volumes:
      - tdai-panel-data:/data/knowledge
      - agentdata:/data/clawith-agents:ro             # §1.3 本地目录建图源（fork LocalSourceFetcher）
    networks: [default]

  tdai-proxy:                                         # 供外部 coding agent，Clawith 自身不走
    image: agentmemory/memory-proxy:v2.0.2-beta.1
    restart: unless-stopped
    volumes:
      - ./deploy/tdai-proxy/config.yaml:/data/config.yaml:ro   # proxy 只读 YAML，不认 env
    networks: [default]
    ports:
      - "127.0.0.1:${TDAI_PROXY_PORT:-8096}:8096"     # 仅本机；外部接入走反代

volumes:
  tdai-memory-core-data:
  tdai-panel-data:
```

**memory-core 的 `tdai-gateway.yaml`**（`memory.*` pipeline 参数与 `skill.*` 只在 yaml、无 env 映射，不挂文件就落到编译期默认）`[源码 gateway/config.ts:522-529]`：

```yaml
deployMode: standalone
stateBackend: local
server: { port: 8420, host: 0.0.0.0 }
data: { baseDir: /data/tdai-memory }
llm: { baseUrl: "${TDAI_MEMORY_LLM_BASE_URL}", apiKey: "${TDAI_MEMORY_LLM_API_KEY}", model: "${TDAI_MEMORY_LLM_MODEL}", maxTokens: 32000, timeoutMs: 300000 }
memory:
  promptMode: ${TDAI_MEMORY_PROMPT_MODE:-code}       # code=工程记忆（纯闲聊抽 0 条）| chat=个人对话
  capture: { enabled: true }
  extraction: { enabled: true, enableDedup: true, maxMemoriesPerSession: 20 }
  persona: { triggerEveryN: 50, maxScenes: 15 }
  pipeline: { everyNConversations: 5, enableWarmup: true, l1IdleTimeoutSeconds: 600, l2DelayAfterL1Seconds: 90, l2MinIntervalSeconds: 900, l2MaxIntervalSeconds: 3600 }
  recall: { enabled: true, maxResults: 5, scoreThreshold: 0.3, strategy: hybrid, timeoutMs: 5000 }
  storeBackend: ${TDAI_STORE_MODE:-sqlite}
  embedding: { provider: none }                      # 禁用向量 → 退化纯 FTS5/BM25（零 embedding 成本）
skill:
  enabled: true
  routing: { mode: bm25, searchTopK: 20 }
  extraction: { enabled: true, maxIterations: 16 }
```

**关键决策**：
1. **LLM 复用**：`TDAI_MEMORY_LLM_*` 指向 Clawith 既有 deepseek 端点（memory 组只做 summarize/L1 抽取，用便宜模型）；**embedding 默认 `none` = BM25 零成本**。不新建供应商。
2. **端口不暴露**：core/hub 不映射宿主端口，仅 docker 内网（`backend → tdai-memory-core:8420`、`frontend nginx → tdai-memory-hub:8125`）；proxy 仅 `127.0.0.1`。
3. **存储**：默认 sqlite 落 named volume；MongoDB 是**试验特性**（不建议生产默认、切换不迁移数据），规模上来再切。BM25 随后端变（sqlite=FTS5 / mongodb=mongot）。
4. **镜像 pin**：`:latest` → `v2.0.2-beta.1`（当前分支 `feat/server_team`），避免上游静默变更。
5. **部署形态**：默认 `Standalone`（SQLite+本地文件）；不引入 `Service`（TCVDB+COS+Redis，仅 HA 才需）。
6. **init-admin**：首启 `POST /v3/internal/meta/user/init-admin` 自动生成随机 `sk-mem-*` 落 `.admin-key`；业务用户经 `/v3/meta/user/create` 拿 `default_user_key`——**Clawith SDK 直连用 `default_user_key`，不是 admin key**。

### 1.2 Memory Core ← Python SDK（Clawith 自身对接，官方 Adapter 三件事）

backend 加依赖 `tencentdb-agent-memory-sdk-python`，新增封装服务 `backend/app/services/tdai_memory.py`（集中连接 + 降级）。

**① 会话结束写 L0**：实现 `RuntimeTerminalProductHandler`，注册进 `terminal_handlers`（`worker_service.py:333`），与 `record_terminal_scores` 同层。协议签名 `[源码 checkpoint_side_effects.py:72-80]`：

```python
async def handle(self, *, run: RuntimeRunRecord, checkpoint: CheckpointObservation) -> None
```

- `terminal_handlers` 仅终态（`completed/failed/cancelled`，`checkpoint_side_effects.py:50`）才调；每个 handler 包 try/except，`if errors: raise errors[0]` → **handler 必须内部自吞异常**（对齐「记忆不阻断」红线）。
- `RuntimeRunRecord`（`command_worker.py:124-147`）**无 `user_id` 字段**——user 维度走三级回退：查库 `AgentRun.origin_user_id`（`models/agent_run.py:107`）→ `checkpoint.state["snapshots"].initial_input.triggered_by_user_id` → 空串兜底。
- v3 SDK 的 `team/agent/user` 在**构造时**固化（缺一抛 `ParamError`），单进程多租户故用 **`(team,agent,user)` 三元组 LRU 工厂**拿客户端；`session_id` 在 `add_conversation` 调用时传 `run_id/thread_id`（写必填）。

**② 构造 Prompt 前召回 L1/L2/L3**：在 `build_agent_context`（`agent_context.py:531`）内、现有 `memory`/`reflections` 读取之后，追加一次召回（`search_atomic` L1 + `read_core` L3 + `list_scenarios` L2），拼成 `<tdai_memory_context>` 块**追加进 `stable_dynamic`**（turn 不变、字节稳定，符合前缀缓存约束）。整个召回 try/except 包裹、失败降级空串。

**③ 有边界注入（可选 P2）**：`SkillClient.listing()` 渲染 skill 目录，仅团队启用 TencentDB skill 沉淀时注入，默认关（避免双份 skill 目录）。

**并跑不替换**：TencentDB L1 自动抽取产物**只读召回、不反写** Clawith `memory.md`/`reflections.md`；替换决策需单独评审（负结论 #4）。

### 1.3 Knowledge（Wiki/Code-Graph）对接 —— 统一知识面（路径 A 核心）

**为什么走服务端而非库嵌入**：库（`@colbymchenry/codegraph`）只解决「怎么建图/查」，服务解决「**图归谁管、谁能用、怎么持续同步、怎么被 agent 发现**」——Auto-Sync、统一 ACL/Fixed Binding、`/v3/tools/list|call` 自发现、Panel 管控。这些治理维度是库嵌入拿不到的，也是「完全集成」必须走服务端的理由。

**两条路径（A 核心 / B 可选）**：

| 路径 | 做法 | 定位 |
|---|---|---|
| **A. 服务端 API** | fork Knowledge 补 `LocalSourceFetcher`，`/v3/code-graph/create` 支持本地路径建图 | **✅ 核心（完全集成）**：统一数据面 + ACL/Auto-Sync/自发现 |
| **B. 库直接嵌入** | backend 加 builtin 工具 subprocess 调 `codegraph`（已实测） | 可选降级：只拿建图+查询，丢统一治理 |

**决策**：走 A。补丁四件套：
1. **`LocalSourceFetcher`（约 50 行类）**：`fetch` 校验本地路径返回 `{localPath, version:null, sourceType:"local"}`；`sync` 重扫（mtime 变化触发重建，**排除 `.codegraph/`**）；`validate` 校验存在可读；`supportedType="local"`。
2. **`registry.ts:16` 一行**：`this.register(new LocalSourceFetcher());`（`detectType` 已路由 local，只差注册）。
3. **compose 挂载**：`agentdata:/data/clawith-agents:ro` 挂进 hub 容器（§1.1）。
4. **镜像构建**：`deploy/panel-knowledge-combined/build.sh` 重构建 → push 私有 registry；fork 与上游升级的分叉是真实维护负担（负结论 #2）。

**建图数据源**：① Clawith 持久工作区（`agentdata` 内稳定目录，走 fork）② 团队共享 upstream 仓库（public HTTPS git，走既有 `GitSourceFetcher`）。

**Wiki（互补非互斥）**：统一数据面下 Wiki 与 CodeGraph 同源。Clawith 现有「团队经验库」管「人验证过的做法」，Wiki 管「文档结构化检索」。**默认决策 = 完全集成 Knowledge 服务（路径 A 服务端统一数据面）**；其中 CodeGraph 是默认开启的首个能力（建图不需 LLM、零额外账单，前置 fork 补丁见上），Wiki ingest 按需启用（消耗 `LLM_*` 账单）。

### 1.4 Panel ← nginx 反代

`frontend/nginx.conf.template` 加一条 location：

```nginx
location /memory-panel/ {
    set $memory_hub_upstream http://tdai-memory-hub:8125;
    rewrite ^/memory-panel/(.*)$ /$1 break;
    proxy_pass $memory_hub_upstream;
    proxy_set_header Host $http_host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_read_timeout 120s;
    proxy_send_timeout 120s;
}
```

面板与 Clawith 前端同源（同域同 TLS），复用登录会话；需独立鉴权时在 nginx 层加 basic-auth。

### 1.5 Proxy（部署但 Clawith 自身不走）

**对 Clawith 自身冗余的三点**：① 双重注入（SDK 已召回 L1-L3，proxy 注入器再注入同一批 → 重复膨胀）；② 破坏可观测性（插 proxy 中间层 = Langfuse trace 归属断裂 + `x-tdai-*` 头脱钩 + 前缀缓存不稳）；③ 多一跳延迟 + 单点。

**真正价值**：让**外部 coding agent（Claude Code / CodeBuddy / Codex / dsh / WorkBuddy / OpenCode / Hermes / OpenClaw 8 类）接入同一团队记忆**，与 Clawith 内部 agent 共享同一数据面。

**session 接入两派（session-id 来源）**：
- **SDK 托管型**（Claude Code/CodeBuddy/Codex/WorkBuddy/dsh）：官方 SDK 自动管理 session，**首选、不踩下述限制**。
- **Header 静态型**（Hermes/OpenClaw）：只能 Header 预选（`x-team-id`+`x-agent-id`+`x-task-id`+`x-conversation-id` 齐全跳过表单），踩两个硬限制：`x-task-id` 当前版本必填（须预建 Task，Clawith 无 task 实体；下一版本才可选）、`x-conversation-id` 静态写死（串桶 + 手动轮换 + tool call 丢 header 致注入盲区）。**结论：对 Hermes/OpenClaw 不承诺完整记忆闭环**。

**安全边界（鉴权面三层 + 四层纵深防御，§4 #1）**：
- 鉴权三层：① 网关 Bearer = 服务间门禁（default-open；`Bearer+x-tdai-service-id` 视为**管理员级凭据、不做用户级鉴权**）② `x-tdai-service-id` = 实例标识 ③ `x-tdai-user-key` = 用户级鉴权（默认生效）。
- 四层纵深防御：**网络层**（三端口不映射公网、仅 docker 内网）+ **边界层**（nginx `/memory-panel/` 鉴权）+ **身份层**（第③层始终生效）+ **服务间层**（第①层 Bearer 默认关，P2 修 `auth.ts` 后按需开）。任一单层失效不导致全盘暴露；第①层 key 泄露=全库可清，按 root 凭据治理。

### 1.6 多租户隔离映射

| TencentDB 概念 | Clawith 映射 | 说明 |
|---|---|---|
| `x-tdai-service-id`（实例） | 部署实例常量 `clawith-prod` | 单部署一实例 |
| `team_id` | `tenant_id` | 租户级记忆边界 |
| `agent_id` | `agent_id` | 每 agent 独立记忆桶 |
| `user_id` | `user_id` / 终端用户名 | 终端用户维度 |
| `session_id` | run/thread id | L0 写入隔离 |
| `memory_id` | 自动派生 `chat_memory-{team}-{agent}` | 面板资产展示 |

v3 SDK 构造强制 `team_id/agent_id/user_id` 非空（缺一抛 `ParamError`），契约层保证不串租户。

**meta 面注册两档**：**最小档（默认）** = 数据面 only，SDK 字符串键直写，不注册 meta 面（Panel 看不到 Clawith team/agent，但记忆隔离/召回完全可用）；**完整档（P2）** = 同步注册进 `/v3/meta/*`（Panel 成为真实管理 UI），代价是两份元数据双向联动。建业务用户是两步：`user/create` 拿 `default_user_key` → `team-member/add` 挂进 team；`team/create` 的 `owner_user_id` 须等于调用 key 的 user_id。

### 1.7 可选能力（默认关闭，按需开启）

| 能力 | Clawith 决策 |
|---|---|
| **ClickHouse 分析看板** | **默认关闭（P2）**：+CH 存储依赖；采集点半错位（Clawith 不走 proxy，`usage_logs` 采不到；走 Knowledge 服务的 code-graph 能采到 `tool_call_logs`）。记忆命中观测走 Langfuse/官方日志、不依赖 CH |
| **`/analyse` URL marker** | **保留默认（零开销）**，但只对走 proxy 的流量生效，Clawith 自身不适用；留给外部 agent |
| **`sessionInit.defaultTaskId`** | **P2**：依赖 proxy 表单，方案默认 Header 预选、表单少用 |
| **`agents/asset-import.ts`** | **✅ 落地执行**：Clawith 现有 `skills/`/`memory/` 冷启动导入，避免零记忆 |

**效果验证（Clawith 自身）**：① 官方第一方 `MemoryGenerationLogClient` 查 L1/L2/L3 生成日志；② Langfuse 埋点，复用 `services/observability/scores.py`（`record_terminal_scores`）挂「召回 N 条/是否采纳/token 增量」score——零新存储依赖。

### 1.8 部署拓扑

```
Clawith stack（既有）
  ├─ backend(8000) ──SDK 直连──▶ tdai-memory-core:8420   （写 L0 / 召回 L1-L3）
  ├─ backend ──HTTP──▶ tdai-memory-hub:8424               （Knowledge 统一数据面，路径 A）
  │                        └─ agentdata 卷 ──ro──▶ /data/clawith-agents（LocalSourceFetcher）
  ├─ frontend nginx ──反代──▶ tdai-memory-hub:8125        （Panel /memory-panel/）
  └─（外部 coding agent）──▶ tdai-proxy:8096 ──▶ tdai-memory-core + LLM upstream
```

---

## 2. 执行序

| 步 | 动作 | 产出 | 依赖 |
|---|---|---|---|
| 1 | compose 增量起 3 服务 + LLM env + 数据卷 + 镜像 pin（v2.0.2-beta.1） | 四服务跑起来（含冒烟） | `.env` 填 `TDAI_*` |
| 2 | `agents/asset-import.ts -y` 冷启动导入 Clawith skills/memory → Memory Hub | 记忆冷启动 | 步 1 |
| 3 | backend 加 SDK 依赖 + `tdai_memory.py`（连接/降级/多租户映射 + Langfuse 埋点） | SDK 数据面客户端 | 步 1 |
| 4 | run 终态收口写 L0（fire-and-forget，`RuntimeTerminalProductHandler`） | L0 落库 + 写入埋点 | 步 3 |
| 5 | `build_agent_context` 追加召回段（L1/L2/L3 → stable_dynamic） | 召回注入 + 命中埋点 | 步 3 |
| 6 | fork Knowledge 补 `LocalSourceFetcher` + 重构建 hub 镜像 + 挂 `agentdata` 卷 | 统一知识面（CodeGraph） | 步 1 |
| 7 | nginx `/memory-panel/` 反代 + 面板鉴权（admin/business 分层） | Panel 同源接入 | 步 1 |
| 8 | 单测 + 多租户验证 + 部署冒烟（§3） | 测试绿 | 步 4-7 |
| 9 | （P2）meta 面注册：tenant/agent 同步进 `/v3/meta/*` | 元数据面同步 | 步 8 后 |
| 10 | （P2）fork proxy 补 `Authorization` header → 启用网关 Bearer | 生产鉴权加固 | 步 1 |
| 11 | （P2）ClickHouse 看板 + `defaultTaskId`（外部 agent 规模化后） | 外部 agent 分析/表单优化 | 步 9 后 |

---

## 3. 测试 / 回滚 / 影响面

**测试（TDD）**：`tdai_memory.py` 单测（`add_conversation` 三元组+session_id 必填、`recall` 不可达返回空串）；`build_agent_context` 三态（启用/关闭/不可达）仍返回合法三段；`LocalSourceFetcher`（本地目录返回 `sourceType:"local"`、非法路径 throw）；多租户（两租户 L0 隔离不串数据）；部署冒烟（`curl` core/hub 健康、Panel 经 `/memory-panel/` 打开）。

**回滚**：改动集中——1 个封装服务 + 1 段 `build_agent_context` 追加 + 3 compose service + 1 条 nginx location + 1 个依赖 + 1 个 fork 补丁。回滚 = 删依赖 + 摘注入段 + `docker compose down` 新服务 + 删 location + 换回官方镜像，**无数据迁移、无 checkpoint/WS/飞书状态机改动**。已写入的 L0-L3 数据留在 named volume（删容器不删卷）。

**影响面**：新增 3 容器 + 1 依赖（纯 httpx）+ 1 段 prompt 注入（stable_dynamic 尾部、上限截断）+ 1 个 fork 镜像 + 若干 Langfuse score（复用 scores.py，零新依赖）。**不改**：`build_agent_context` 签名、checkpoint、WS 状态机、飞书通道、多租户表结构、Langfuse 主对话 trace 归属。爆炸半径 = run 终态收口（异步可摘）+ prompt 注入（降级空串）+ 3 个外部容器。

---

## 4. 诚实负结论清单（须随方案交付、不可省略）

1. **Gateway 鉴权缺口** —— 非空 `MEMORY_CORE_GATEWAY_API_KEY` 断 proxy auth/sessionInit。
   - **研究（精确定性 = 两个独立事实叠加）**：① core Bearer gate 是 **default-open**（`server.ts` `verifyAuth` 未设 `TDAI_GATEWAY_API_KEY` 即返回 `"ok"` 放行，OWASP API8）；② proxy **`auth.ts` 单点漏 header**（`verifyUserKey` 调 `/v3/meta/auth/verify` 只带 `content-type`+`x-tdai-service-id`，漏 `Authorization: Bearer`，对比 `meta/client.ts:532-533` 正确带）。鉴权面三层：第①层 Bearer = 服务间门禁（`Bearer+x-tdai-service-id` 视为管理员级凭据、不做用户级鉴权）；第②层 `x-tdai-service-id` = 实例标识；第③层 `x-tdai-user-key` = 用户鉴权、默认生效。缺口只在「proxy 全栈」路径——**SDK 直连（Python SDK 全线支持 `api_key`=Bearer）、proxy 注入器、hub→core 转发均不受影响**。
   - **推荐（四层纵深防御）**：网络层（三端口不映射公网、仅 docker 内网，与 Clawith postgres/redis 同款）+ 边界层（nginx `/memory-panel/` 鉴权）+ 身份层（第③层始终生效）+ 服务间层（第①层 Bearer 默认关，P2 修 `auth.ts` 的 `fetchOpts.headers` 加 Bearer 后按需开）。**「人→面板」鉴权与「面板→core」服务间鉴权（`REMOTE_INSTANCE_KEY`，默认 `local`）是两层、都要管**。

2. **LocalSourceFetcher 需 fork 补齐** —— 统一知识面的必经一步。
   - **研究**：`ISourceFetcher` 仅 4 成员，`registry.resolve` 已把 `file://`/`/`/`./` 路由到 `"local"`，只差一行 `register`（约 50 行类）；真正门槛是 `agentdata` 卷挂进容器 + 路径映射（`STORAGE_LOCAL_ROOT=/data/agents` 只读挂 `/data/clawith-agents`）。
   - **推荐（已定，走服务端统一数据面）**：fork 补 `LocalSourceFetcher` + `build.sh` 重构建镜像 → push 私有 registry；成本主要是维护 fork 与上游升级的分叉，非写代码量。库嵌入（路径 B）降为可选。

3. **LLM 双份消费** —— `MEMORY_LLM_*` + `PROXY_UPSTREAM_*` + Clawith 主对话。
   - **研究**：embedding 默认 `none` = 向量禁用、回落 BM25 零成本；`provider=local`（GGUF）已在 v2.0.2-beta.1 从用户 config 移除（入口不可达）。真正成本只有 L1 抽取/summarize，节奏由 pipeline 参数决定（`everyNConversations=5`/`maxMemoriesPerSession=20`/`triggerEveryN=50`/`l1IdleTimeoutSeconds=600`）——**低频低量**；`PROXY_UPSTREAM_*` 与 wiki ingest 在 Clawith 自身路径零成本。
   - **推荐**：起步 `embedding=none` 跑通；`MEMORY_LLM_*` 配轻量档；质量不够再配远程 embedding（按需升级）。**修正：「三份账单」→ Clawith 自身常驻新增实际一份（L1 抽取，低频低量）。**

4. **记忆栈并跑非替换** —— TencentDB L0-L3 与 Clawith `memory.md`/`reflections.md`/经验库并存。
   - **研究**：风险不在「并存」而在 prompt 里**两套记忆重复/矛盾**（自写表述 vs 自动抽取表述不一）。
   - **推荐**：① 注入时分段标注来源（自写 vs 平台抽取 + 优先级）；② 维持只读召回不反写；③ 替换决策门 = Langfuse 埋点 L1 命中率/采纳率连续达标后再评审「`memory.md` 退役」。

5. **proxy 对 Clawith 自身冗余** —— SDK 已走同一数据面。
   - **研究**：proxy 对 memory 数据面冗余，但它的 skill/knowledge 注入编排是 SDK 拿不到的；Clawith 已有自有 `skills/` 机制，不依赖。
   - **推荐**：维持不走 proxy；重估条件 = 「想让内外 agent 的 skill 注入编排一致」。

6. **默认凭据 `local`/`admin`/`admin`** —— 仅本地体验。
   - **研究**：`init-admin` 首启生成随机 32 位 admin key；业务用户经 `/v3/meta/user/create` 拿 `default_user_key`，不应拿 admin 做业务。
   - **推荐（上线 checklist）**：① 三端口不暴露公网（同 #1）② 首启后换随机 admin key、记入 vault 不落明文 ③ 业务走 `default_user_key` ④ 公网暴露前完成 ①+② 并加 nginx 边界鉴权。

---

## 附：证据索引

- **TencentDB 源码**：`Documents/UGit/TencentDB-Agent-Memory/{MemoryCore,MemoryKnowledge,MemoryProxy,deploy,sdk}` 逐模块 read_file（四服务 `l1-extractor.ts`/`bridge.ts`/`git-fetcher.ts`/`registry.ts`/`start-proxy.sh`/`auth.ts`/`server.ts`/`meta/client.ts`；SDK `v3/{client,skill_client,metadata_client,memory_prompt,memory_generation_log}.py`）。
- **CodeGraph**：`@colbymchenry/codegraph@1.6.0`（MIT，独立库）实测——Clawith backend 399 文件 → 10425 节点 / 28635 边，1.2s，查询结果与真实源码一致。
- **Clawith 源码**：`services/agent_context.py:531`、`services/llm/caller.py:547`、`services/agent_runtime/{checkpoint_side_effects,command_worker,worker_service,node_executor,state}.py`、`services/observability/scores.py`、`models/agent_run.py:107`、`docker-compose.yml`、`frontend/nginx.conf.template`。
- **参考对比**：≥10 项目横向（TencentDB/CodeGraph/Clawith/LangGraph/Letta/OpenViking/Acontext/hermes/GenericAgent/SkillClaw/bisheng 等），详见 `20260907-self-evolving-agents-landscape.md`、`20260907-agent-memory-landscape.md`、`20260907-g3-skill-sedimentation-production-plan.md`。
