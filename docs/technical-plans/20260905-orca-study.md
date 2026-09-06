# orca（stablyai/orca）整库源码研究报告

日期：2026-09-05
状态：**完成**（分析基于本地仓库 `/Users/shubinzhang/Documents/UGit/orca` HEAD `af821260`，`--depth 1` 浅克隆；Clawith 对照基于 `/Users/shubinzhang/Documents/agent/Clawith` 工作树）
定位：参考资料研究，非实现方案。对照 Clawith 的 run-scoped workspace 隔离、agent_runs 台账、命令租约、多端断流恢复与 skills 渐进披露。

## 0. 项目概览

- **是什么**：orca（`stablyai/orca`，TypeScript，~62k★）——**本地优先的桌面 AI 编排器**。README 定位一句话：*"Run Codex, ClaudeCode, OpenCode or Pi side-by-side — each in its own worktree, tracked in one place"*。它本身**不运行任何 LLM**，只负责**启动/监控多个第三方 coding-agent CLI**（Codex、ClaudeCode、OpenCode、Pi 等 30+ 个，见 `src/shared/tui-agent.ts:3-39` 的 `TuiAgent` 联合类型），每个 agent 跑在自己的 git worktree 里，统一编排、统一追踪。
- **三层架构**：① Electron **desktop**（`src/` 主进程，agent 编排/worktree 管理/SQLite 台账）；② **cloud relay**（`cloud/`，纯 WebSocket 转发器，无业务逻辑）；③ **mobile companion**（`mobile/`，React Native，通过 relay 与 desktop 配对，做通知/终端镜像/轻交互）。
- **核心结论**：orca 的**编排能力不在云端、不在大模型里，而在「本地单机 + 一套精巧的持久化契约」上**——单写者租约（HMAC claim + 单调 fence）、loopback HTTP 统一追踪（spool 持久化 + fail-open）、四字段幂等 mutation envelope、journal 游标增量回放、relay splice 十态机。这些**分布式系统「租约/幂等/回放」原语**，是它最值得 Clawith 借鉴的部分——**不是 UI、不是 CLI 集成、不是 worktree 本身**。
- **对标关系**：orca 是单机多进程（1 桌面 → N 个 CLI agent PTY），Clawith 是多租户服务端（1 服务 → N 个 LangGraph run，`RuntimeCommandDaemon` 领命令）。**架构形态相反，但「编排一个长生命周期的外部执行体」这一本质问题相同**，可迁移的是租约/幂等/回放的设计契约。

---

## 1. 多 agent 并行 + worktree 隔离

### 1.1 orca：每个 agent 一个 worktree，靠 git 原生机制隔离

- **agent 枚举**：`TuiAgent` 是 30+ 个 CLI agent 的联合类型（`src/shared/tui-agent.ts:3-39`），每个 agent 的启动命令由 `resolveAgentLaunchCommand()` 按配置拼装（`src/shared/tui-agent-launch-command.ts:24-101`），prompt 注入分 `argv` / `flag-prompt` / `hermes-query` / `flag-prompt-interactive` / `flag-interactive` / `followup` 六种方式（`buildAgentStartupPlan()`，`src/shared/tui-agent-startup.ts:40-182`）。
- **worktree 创建**：`addWorktree()` 用 `git worktree add --no-track -b <branch>`（`src/main/git/worktree-add.ts:143-237`），并设置 `branch.<branch>.base` 与 `push.autoSetupRemote`。
- **worktree 预热池**：`src/main/worktree-create-preparation.ts:72-250` 维护 `prepare → claim → retarget → rearm` 状态机——预创建 worktree 池，claim 时按 `canonicalBase` + `measureRetargetDivergence` 判断能否复用还是重建，避免 agent 启动时冷等 clone。`resolveWorktreeCreateBase()`（`src/main/worktree-create-base.ts:8-29`）决定基线 commit。
- **worktree 身份抗 rename**：`canonicalWorktreeIdentity()` 用 `wt2:host:instanceId` 三段标识，**刻意不含路径**（`src/shared/worktree/identity.ts:20-29`），这样目录被移动/重命名不影响身份。agent-session 的 scope key 用 `\u0000` 分隔 `host + wslDistro + workspaceId`（`src/shared/agent-session-record.ts` 的 `agentSessionScopeKey`）。

### 1.2 对 Clawith

- Clawith 不靠 `git worktree`，而是 **run-scoped 物化 workspace**：`RunWorkspace`（`backend/app/services/sandbox/local/run_workspace.py:1-246`）把源仓库物化到每个 run 自己的临时 workspace（`TempWorkspaceManifestEntry` 清单 + `_materialized_run_ids` 去重），run 结束 `close_run_workspace` 回收。
- git 元数据通过 `create_git_bundle` / `restore_git_metadata_from_bundle` / `restore_git_metadata_from_remote` 注入（`backend/app/services/gitlab_workspace.py`），并由 `workspace_policy.py` 的 `PublishClass`（`source/derived/artifact/git_metadata`）＋ `redact_git_secrets` 在发布回源时分类与脱敏（`backend/app/services/sandbox/workspace_policy.py:1-147`）。`.git` 物化是 commit `4d3fe431` 对 workspace_sync_conflict 的根治。
- **对照结论**：两者都解决「多执行体不互相污染」。orca 用 git 原生 worktree（省事，但 clone 慢、磁盘重），Clawith 用自研物化 + bundle 注入（可控、可脱敏、可审计）。**orca 的预热池是 Clawith 没有的**——Clawith run 启动时冷物化，可借鉴「预物化池 + 按基线 commit 复用/重建」降低长尾延迟。

---

## 2. 单写者租约（agent-session lease）

orca 最硬的一块分布式原语，全部落在 `src/shared/agent-session-record.ts` 与 `src/main/runtime/`：

- **单写者租约 `AgentSessionLease`**（`agent-session-record.ts`）字段：
  - `runtimeFence` —— **单调递增整数**，每次状态转移 +1，构成「谁是最新写入者」的比较基准；
  - `handoffStage` —— 交接阶段；
  - `ownerProcess` —— 写者进程标识，**PID 复用安全**（防止旧 PID 被新进程占用后冒充 owner）；
  - `spawnToken` —— 每次 spawn 刷新，防「旧 spawn 的僵尸写覆盖新 spawn」；
  - `leaseDeadlineAt` —— 租约到期时间；
  - `claimStatus ∈ {reserved, live, conflicted, released}`；
  - `deathEvidence` —— 进程死亡证据（用于仲裁「到底谁还活着」）。
- **HMAC claim 签名**：`AgentSessionClaimSigner` 用 `HMAC-sha256` 派生出 32 字节 coordination key，并生成 `identityDigest` / `worktreeScopeDigest`（`src/main/runtime/agent-session-claim-identity.ts:88-173`）。claim 是「谁先签名谁持有」，不是靠中心锁。
- **续租**：`renewAgentSessionLeases()`（`src/main/runtime/agent-session-lease-renewal.ts`）周期刷新 `leaseDeadlineAt`。

### 2.1 对 Clawith

- Clawith 的租约在 `claim_next_command()`（`backend/app/services/agent_runtime/persistence.py:701-739`）：`_claim_statement()` 按 `(created_at,id)` 全局 FIFO + `with_for_update(skip_locked=True)` 抢最早命令，start 走 `_acquire_start_lane()` 抢排队 lane（唯一部分索引 `uq_agent_runs_active_lane` 防双 holder），认领后 `claim_expires_at=now+60s`。
- **心跳韧性**：`_heartbeat()`（`backend/app/services/agent_runtime/command_worker.py:520-561`）执行全程每 20s（`AGENT_RUNTIME_COMMAND_CLAIM_RENEW_SECONDS=20`）续租，连败 `_max_heartbeat_failures` 次才停——修掉「60s 短租约跨 30min 图执行」反模式。
- **对照结论**：Clawith 租约偏「短租约 + 心跳续期」，orca 偏「fence 单调计数 + HMAC claim + deathEvidence 仲裁」。**orca 的 `runtimeFence`（单调计数）是 Clawith 可借鉴的**：Clawith 的 `expectedRuntimeFence` 等价物目前靠 `attempt_count` + 时间戳，缺一个严格单调的「状态代际」比较，在「多 worker 竞抢同一 run」时仲裁语义不如 fence 干净。

---

## 3. 统一追踪：agent-hooks loopback HTTP + spool 持久化

orca 不解析 agent 的 PTY 输出，而是**让第三方 CLI agent 通过 HTTP hook 主动上报状态**：

- **loopback HTTP server**：`src/main/agent-hooks/server.ts` + `server-lifecycle.ts:20-158`——agent 完成后回调本地回环端口，鉴权用 header token `x-orca-agent-hook-token`，带 **slowloris 防护**（慢请求限速），**fail-open 返回 204**（hook 失败不阻塞 agent 本身）。
- **持久化恢复**：`last-status.json` 落盘（`server-constants.ts`），`STATUS_PERSIST_DEBOUNCE_MS=250` 去抖、`HYDRATE_MAX_AGE_MS=7天` 水合有效期、`CLOSED_AGENT_STATUS_TAB_IDS_MAX=1024` 上限——**桌面重启后从磁盘恢复最后状态**。
- **durable replay**：`ingestSpoolRecord()`（`server-ingest-normalization.ts`）把 hook 事件先 spool 到持久队列再消费，防「上报瞬间进程死亡丢事件」。
- **transport-agnostic 规范化**：`normalizeHookPayload()`（`src/shared/agent-hook-listener.ts:20-173`）统一各 agent 的异构 payload，含 **Claude compact completion 幂等 guard**（compact 会重复发 completion，去重）、codex subagent 特判。
- **hook 安装机制**：`managed-hook-installer.ts`（`src/relay/managed-hook-installer.ts:44-72`）用 `require('managed-hook-runtime.js')` **懒加载**体积大的 installer——relay 启动不背安装器，实际执行时才在远程进程内加载。

### 3.1 对 Clawith

- Clawith 的执行态在 **checkpoint + 生命周期事件**（`agent_runs` 表本身**无 status 列**，终态由 `run_is_terminal()` 取最新事件推导，`backend/app/services/agent_runtime/event_stream.py:305-313`）。台账落在 `agent_runs`（`backend/app/models/agent_run.py:27-194`）+ `agent_run_commands`（`agent_run_command.py:25-105`），并外挂 Langfuse 观测（trace 与 `run_id` 关联）。
- **对照结论**：orca 的「agent 主动上报 + spool 持久化 + fail-open」对应 Clawith 的「checkpoint 事件流 + 台账 + 观测」。**orca 的 `last-status.json` 跨重启水合 + spool 先落盘再消费**值得借鉴到 Clawith 的**心跳/事件丢失兜底**场景：Clawith 目前 run 心跳靠 DB 记录，短窗内的「进程死前最后一笔」同样有丢失窗口，spool 的「先持久化、后消费」思路可直接迁移。

---

## 4. 幂等 envelope 与 journal 游标回放

- **四字段幂等 envelope** `AgentSessionMutationEnvelope`（`src/shared/agent-session-wire.ts:164-217`）：`sessionId + clientOperationId + expectedRuntimeFence + payloadFingerprint` —— ① `clientOperationId` 客户端操作幂等键；② `expectedRuntimeFence` 乐观并发（写前校验 fence 是否仍是最新代际）；③ `payloadFingerprint` 内容指纹去重。四者合起来保证「重复投递 / 乱序 / 过期代际写入」都被拒绝或幂等吸收。
- **journal 游标**：`AgentSessionHistoryRequest` 支持 `tail/before/after` 方向 + `cursor`（`agent-session-wire.ts`），`AgentSessionJournalBatch` 按 `cursor` 增量发布、幂等应用——**断线重连后从 cursor 续传，不重放全量**。
- **DB 层幂等**：SQLite `mutation_receipts` 按 `(caller_fingerprint, request_id)` 幂等（`src/main/runtime/orchestration/db/schema/create-core-tables-sql.ts`）。

### 4.1 对 Clawith

- Clawith 幂等靠 `idempotency_key` + 唯一约束 `uq_agent_run_commands_run_idempotency`（`agent_run_command.py:46`）。
- 事件回放靠 `stream_run()` 的 `RuntimeEventCursor(created_at, event_id)` 游标分页（`backend/app/services/agent_runtime/event_stream.py:305-559`）。
- **对照结论**：两者方向一致，**orca 的 `expectedRuntimeFence` 乐观并发 + `payloadFingerprint` 内容去重**比 Clawith 单纯的 `idempotency_key` 多两层防线——Clawith 的「部署杀在途 run → 全量重放 → 分叉撞幂等账本」教训（已闭环）里，若有 fence 代际校验，重放会在写入前被 fence 拒绝，不必事后审计。**值得评估引入一个「run 状态代际」字段**。

---

## 5. cloud relay：splice 十态机 + admission + 断线重连

orca 的 cloud 是**纯转发器**，业务逻辑为零，但分布式原语打磨得很细：

- **splice 十态机** `SPLICE_STATE`（`cloud/packages/relay-contract/src/splice-state-machine.ts:1-34`）：`pre-auth-admitted → … → spliced → e2ee-confirmable → teardown`，把「手机/桌面各自出站 WS 到 relay cell、由 relay 帧级 splice」这条链路拆成十个显式状态。
- **close code 语义** `RELAY_CLOSE_CODE`（`close-codes.ts:1-8`）：`4401/4404/4408/4409/4429/4503`，用不同 code 区分「未授权 / 找不到 / 冲突 / 预算拒绝」等，客户端据此决定重试策略。
- **admission 预算** `RELAY_ADMISSION_BUDGETS`（`admission-budgets.ts:1-76`）：`cloudRunConcurrency=900`、`maxPreAuthConnections=45`、splice 高低水位 `64K/256K`、`spliceWedgedTimeoutMs=10s`——**背压 + 楔死检测**（wedged 连接超时强杀）。
- **转发实现** `wireSplice()`（`cloud/apps/relay/src/splice-forwarder.ts:48-158`）：双方向队列 + backpressure + wedged 检测 + budget 记账。`relay-server.ts:283-502` 处理三条 WS 升级路径（`/v1/connect/` 手机、`/v1/host/control` 控制、`/v1/host/data` 数据）。
- **mobile 断线恢复**：`mobile-relay-reconnect-controller.ts`、`rpc-client-reconnect-wait.ts`（断线退避重连）；通知侧 `notification-reconnect-catchup.ts` 用 **`(seq, epoch)` 单键水位**——epoch 命名计数器生命周期（桌面重启后计数器归零，无 epoch 的 seq 不可信），重连时上报 watermark 由桌面 `getMissedSince` 补发，**内存 seen-set 作第二道去重**（`:1-120`）。
- **E2EE**：`mobile-e2ee-v2-key-schedule.ts` 定义 key schedule（配对派生会话密钥）。

### 5.1 对 Clawith

- Clawith 多端是 **web + 飞书 WS**：`FeishuWSManager`（`backend/app/services/feishu_ws.py:98-467`）——`auto_reconnect=True` 交 SDK 内部重连，但**首次握手失败 SDK 不会重试**，故自建指数退避 `_initial_retry_delay=10 → max 300`（`:331-352`），加 `_no_proxy_ctx` 作用域 bypass macOS 系统代理（`:29-74`），加 30s health-watch 仅记日志不重连（`:360-404`）。
- **对照结论**：Clawith 飞书 WS 的断流恢复「靠 SDK auto_reconnect + 首次握手自建退避」，**没有 orca 那种「事件游标续传 / watermark 补发 / epoch 区分计数器世代」的语义层**——飞书事件丢失只能靠飞书侧重推或 webhook 兜底。**orca 的 `(seq, epoch)` 单键水位 + seen-set 双保险**是 Clawith 飞书（乃至未来 web 长连）消息「不漏不重」可迁移的样板。close code 语义化（不同错码不同重试策略）也值得引入飞书/WS 客户端。

---

## 6. skills 机制：discovery + 版本匹配 guide（反 drift）

- **发现**：`discoverSkills()`（`src/main/skills/discovery.ts:259-317`）扫描 root 列表，用 `SkillScanCoalescer`（TTL 10s）合并扫描、`last-known-root-scan` 降级缓存、`MAX_CACHED_SKILL_ROOTS=1024` 上限；**`unavailable ≠ empty`**——根目录暂不可用 ≠ 没有 skill，降级返回上次已知结果而非空表。
- **选择**：`selectDiscoveredSkills()`（`src/main/skills/agent-skill-selection.ts:8-50`）处理 `exactId`/`name` 的歧义。
- **反 drift 设计**：`skills/orchestration/SKILL.md` 只是 **discovery stub**（薄元数据），**完整指南不在文件里**，由 `orca skills get orchestration` 从 binary 内返回**版本匹配**的完整 guide（`skill-guides/orchestration.md`）——**skill 内容随 binary 版本走，永不与代码 drift**。orchestration skill 内含 coordinator / worker_done / decision gates / task DAG 的编排范式。

### 6.1 对 Clawith

- Clawith 把 `SKILL.md` **全文存 DB**（`backend/app/models/skill.py` 的 `Skill` + `SkillFile`，`folder_name` + `SKILL.md` 存储），`_load_skills_index()` 解析 frontmatter、去重排序、返回 markdown 表格（`backend/app/services/agent_context.py:106-209`），并把 `skills_catalog` 注入 context + `skill_policy` 声明「read_file with the exact advertised path before acting」渐进披露（`agent_context.py:658-673`）。内置 skills 由 `skill_seeder.py` 播种，description 含 "Use when…NOT for…"。
- **对照结论**：两者都做「渐进披露」（索引先行、正文按需），但 **orca 的「SKILL.md = stub + 版本匹配全文内嵌 binary」** 是一条 Clawith 可借鉴的路线——Clawith 的 skills 散落 DB/文件/seed，存在「skill 正文与代码版本脱节」的风险，orca 的反 drift 手法（正文跟着发布物走）值得评估，尤其在 skill 数量膨胀后。

---

## 7. orchestration SQLite 台账

- orca 本地编排状态全在 SQLite（`create-core-tables-sql.ts`），核心表：`runs` / `messages` / `deliveries` / `mutation_receipts` / `worker_dispatches` / `worker_terminal_resources` / `worker_terminal_archives`。
- `messages.type` 枚举含 `status/dispatch/worker_done/merge_ready/escalation/handoff/decision_gate/question/heartbeat`——**把「多 worker 协作」建模成消息流**，`coordinator.ts` / `coordinator-task-dispatch.ts` / `mailbox-*` / `federation-*` 驱动。
- `mutation_receipts` 按 `(caller_fingerprint, request_id)` 幂等，是 envelope 幂等在 DB 层的落点。

### 7.1 对 Clawith

- Clawith 台账是 `agent_runs`（身份/投递事实，无 status）+ `agent_run_commands`（持久化命令收件箱）+ checkpoint（执行态）。多 agent 协作靠 `run_kind=orchestration` + 图编排，而非 orca 的「消息类型建模协作」。
- **对照结论**：orca 的 **`messages.type` 把协作关系显式建模成消息流**（decision_gate / handoff / heartbeat 都是一种消息），比 Clawith「靠图和 checkpoint 隐式表达协作」更可观测、可回放。Clawith 若做多 agent 编排，`worker_terminal_resources`（worker 终态资源回收台账）的「终态资源显式记账」思路值得参考——对应 Clawith 的 tool lease reconcile。

---

## 8. Clawith 侧对照汇总（工具核实）

| orca 机制 | orca 文件:行 | Clawith 对标 | Clawith 文件:行 |
|---|---|---|---|
| worktree 隔离（git worktree add） | `worktree-add.ts:143-237` | run-scoped 物化 workspace + .git 物化 | `run_workspace.py:1-246`、`workspace_policy.py:1-147` |
| worktree 预热池 | `worktree-create-preparation.ts:72-250` | **无**（冷物化） | — |
| 单写者租约（fence + HMAC + deathEvidence） | `agent-session-record.ts`、`agent-session-claim-identity.ts:88-173` | command claim + heartbeat | `persistence.py:701-739`、`command_worker.py:520-561` |
| loopback HTTP 统一追踪 + spool + last-status 水合 | `server-lifecycle.ts:20-158`、`server-ingest-normalization.ts` | checkpoint 事件 + agent_runs 台账 + Langfuse | `event_stream.py:305-559`、`agent_run.py:27-194` |
| 四字段幂等 envelope + fence 乐观并发 | `agent-session-wire.ts:164-217` | idempotency_key 唯一约束 | `agent_run_command.py:46` |
| journal 游标回放（tail/before/after + cursor） | `agent-session-wire.ts` | `RuntimeEventCursor(created_at,event_id)` | `event_stream.py:305-559` |
| relay splice 十态机 + close code + admission | `splice-state-machine.ts:1-34`、`admission-budgets.ts:1-76` | 飞书 WS auto_reconnect + 首次握手退避 | `feishu_ws.py:98-467` |
| 通知 watermark `(seq,epoch)` 补发 | `notification-reconnect-catchup.ts:1-120` | **无**（靠飞书侧重推/webhook 兜底） | — |
| skills stub + 版本匹配 guide 反 drift | `skills/discovery.ts:259-317`、`skills/orchestration/SKILL.md` | SKILL.md 全文存 DB + 索引注入 | `skill.py`、`agent_context.py:106-209` |
| CLI agent 启动（resolveLaunchCommand + startup plan） | `tui-agent-launch-command.ts:24-101`、`tui-agent-startup.ts:40-182` | `RuntimeCommandDaemon` | `worker_service.py:435-564` |
| orchestration SQLite 消息流台账 | `create-core-tables-sql.ts` | agent_runs + agent_run_commands + checkpoint | `agent_run.py`、`agent_run_command.py` |

---

## 9. 可迁移点 → Clawith 映射

| # | orca 机制（文件） | Clawith 对标点 | 可借鉴要点 |
|---|---|---|---|
| 1 | 单写者租约 `runtimeFence` 单调计数（`agent-session-record.ts`） | command claim + attempt_count | 引入「run 状态代际」单调字段，竞抢/重放仲裁语义比时间戳+attempt_count 干净 |
| 2 | HMAC claim + `deathEvidence` 仲裁（`agent-session-claim-identity.ts:88-173`） | claim TTL + heartbeat | PID-reuse-safe owner + 死亡证据，防僵尸写覆盖 |
| 3 | spool 先持久化再消费（`server-ingest-normalization.ts`） | run 心跳/事件 DB 直写 | 进程死前最后一笔事件的丢失窗口，用 spool 兜底 |
| 4 | `last-status.json` 跨重启水合（`server-constants.ts`） | 心跳/状态恢复 | 短窗状态从磁盘恢复，不依赖全量重建 |
| 5 | 四字段幂等 envelope（fence + fingerprint）（`agent-session-wire.ts:164-217`） | idempotency_key | 乐观并发 fence + 内容指纹，重放写入前即拒绝 |
| 6 | journal 游标 `tail/before/after` + reset 语义（`agent-session-wire.ts`） | `RuntimeEventCursor` 分页 | 多端订阅断线续传语义，避免全量重放 |
| 7 | relay splice 十态机 + close code 语义（`splice-state-machine.ts:1-34`、`close-codes.ts:1-8`） | 飞书 WS auto_reconnect | 显式状态机 + 错码驱动重试策略，替代「无限 auto_reconnect」 |
| 8 | admission budget + wedged 检测（`admission-budgets.ts:1-76`） | 飞书/WS 并发控制 | 背压高低水位 + 楔死超时强杀 |
| 9 | 通知 watermark `(seq,epoch)` + seen-set（`notification-reconnect-catchup.ts:1-120`） | 飞书/web 消息去重 | 单键水位防撕裂 + epoch 区分计数器世代，漏重双保险 |
| 10 | skills stub + 版本匹配 guide 反 drift（`skills/orchestration/SKILL.md`） | SKILL.md 全文存 DB | skill 正文随发布物走，防内容与代码脱节 |
| 11 | worktree 预热池 prepare/claim/retarget/rearm（`worktree-create-preparation.ts:72-250`） | run-scoped 冷物化 | 预物化池 + 基线复用/重建，降 run 启动长尾 |
| 12 | `unavailable ≠ empty` 降级（`skills/discovery.ts:259-317`） | skills 索引加载 | 根目录暂不可用时返回上次已知结果，不误报「无 skill」 |

---

## 10. 局限（诚实记录）

- **架构形态差异大**：orca 是本地单机桌面（Electron + SQLite），cloud relay 是纯 WS 转发器、无业务逻辑；Clawith 是多租户服务端（FastAPI/LangGraph/PG）。**租约/幂等/回放原语可迁移，但「无服务器、无 LLM 运行时」的整体范式不可照搬**——orca 不自研 LLM 运行时，编排全靠第三方 CLI agent 的 transcript/hook 上报；Clawith 有 LangGraph 运行时，这是本质分水岭。
- **本次未深入**：`src/renderer/`（9631 个 UI 文件的交互层）、`native/`（Swift/ObjC 原生实现，如 macOS 通知、bwrap 类隔离）、`mobile/` UI 组件细节、`cloud/` 的部署/扩容/K8s 细节、各 agent CLI 的 PTY 接入适配层。
- **orca 多租户能力近零**：无审批、无细粒度权限、无多租户隔离/限流（Clawith 已自研更深），这些不在本研究范围。
- **浅克隆限制**：HEAD `af821260`（`--depth 1`）无完整历史，无法追踪设计决策的演进（如「为何选 fence 而非中心锁」），仅能从代码 + 注释推断。
- **行号精度**：所有 orca/Clawith 行号均经 `read_file` 核实（截至 2026-09-05 工作树）；orca 上游仍在快速迭代，行号随版本漂移。
