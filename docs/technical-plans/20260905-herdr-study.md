# herdr 源码研究报告

日期：2026-09-05
状态：**完成**（分析基于本地仓库 `/Users/shubinzhang/Documents/UGit/herdr` HEAD `af7e189`，--depth 1 浅克隆；对照 Clawith 后端 `/backend/app` 与前端 `/frontend/src` 现行代码）
定位：参考资料研究，非实现方案。对照 Clawith 的 run 生命周期持久化（checkpoint + 命令收件箱）、事件游标回放、WS 断流恢复、部署杀 run 重放、skills/MCP 扩展。

## 0. 项目概览

- **是什么**：herdr（herdrdev/herdr，Rust，35k★）——「your coding agents live on」的本地终端多路复用器：一个 headless server 管理多个 PTY pane，每个 pane 里跑一个 coding agent（claude/codex/opencode/kimi 等 16+ 个），通过本地 Unix socket 的 JSON-RPC API 供 GUI/TUI/CLI 前端接入，支持 detach/reattach、SSH 远端 attach、以及**零停机 server 替换**（live handoff）。
- **核心结论**：herdr 的持久化哲学是「**结构态与高 churn 态分离**」——`session.json` 存 pane/agent 结构态（低频、小），`session-history.json` 存屏幕历史（高频、大），版本化迁移 + 未来版本拒绝 + 原子写。事件流用一个 `MAX_EVENTS=512` 的环形缓冲 + 单调递增 `sequence` 做**游标回放**。agent 状态权威来源是**双重**：agent-native hook（权威）为主、终端尾部屏幕模式匹配（fallback）为辅。这些「分离 + 游标 + 双重来源」手法与 Clawith 的 checkpoint 持久化、事件游标回放、run 状态机同构，可迁移性高。
- **对标关系**：herdr 是**单机单用户**的终端 multiplexer，Clawith 是**多租户**的分布式 Agent 平台（LangGraph checkpoint + PostgreSQL 命令收件箱 + Redis 实时层）。可借鉴的是**持久化/事件/状态判定的设计契约，不是实现**；herdr 的「handoff」是 server 进程替换（对应 Clawith 部署），而非 agent 间委派。

## 1. 会话持久化 + detach/reattach + agent_resume

herdr 把「会话结构」与「恢复动作」拆成两层：

- **多会话命名 + socket 路径**：`src/session.rs`（1060 行）管理多命名会话、每个会话的 client socket 路径、`stop_session`/`delete_session`/`local_attach_command`；`STOP_WAIT_TIMEOUT=15s` 定义优雅停止窗口。
- **持久化三文件分离**：`src/persist.rs:3-5` 明确「`~/.config/herdr/session.json` 存结构态，可选 pane 屏幕历史单独存 `session-history.json`，已装插件单独存 `plugins.json`」。
- **版本化快照 + 未来版本拒绝**：`src/persist/snapshot.rs` 定义 `SNAPSHOT_VERSION=3` 与 `SessionSnapshot`/`PaneAgentSessionSnapshot`（source/agent/kind/value）；`parse_snapshot()`（`:447-456`）遇到**更高版本号直接拒绝**，`migrate_snapshot` 只做向前兼容的向后迁移——绝不让旧代码静默错读新结构。
- **原子写 + symlink 解析**：`src/persist/io.rs:48-61` 的 `save_json_to_path()` 先写 `.json.tmp` 再 `rename` 原子落盘（`:54`/`:56`）；`:18-28` 手动跟随 symlink（stow 用户会命中 dangling symlink）。
- **恢复 = 逐 pane 重建**：`src/persist/restore.rs` 的 `restore()` 给每个 pane 起新 shell 于保存的 cwd，pane ID 重映射；`pane_restore_startup()`（`:739-782`）构建 restore plan，用 `resumed_sessions` HashSet 按 `dedupe_key` 去重（同一 agent session 不重复 resume）；cwd 缺失回退 HOME（fail-soft）。
- **agent_resume 把「恢复」映射到各 agent 自己的 CLI**：`src/agent_resume.rs:136-236` 的 `plan()` 按 agent 映射 resume argv（claude `--resume`、codex `resume`、opencode `--session` 等）；`dedupe_key()`（`:238-243`）为每个 agent/session 生成唯一去重键；`is_official_agent_source()`（`:245-266`）白名单 16 个官方 agent source，自定义 source 不套官方 resume 参数。

**对 Clawith**：对应「checkpoint 持久化 + 部署后 run 续跑」。Clawith 不重放「resume argv」，而是靠 LangGraph checkpoint 单权威：`checkpointer.py:59-75` 的 `runtime_command_config()` 把 `clawith_run_id`/`clawith_command_id` 写进 Graph `metadata`，`create_checkpointer()`（`:170-195`）用 `durability="exit"` 每个 run 边界一 checkpoint、绝不用 wrapper 跳过 checkpoint 切断增量链（docstring `:181-187`）。herdr 的「结构态/历史态分离」可对应 Clawith「产品事实表 vs checkpoint 大对象」的分离。

## 2. pane 状态机 + 双重状态来源

herdr 对「agent 现在在干嘛」的判定是**双重来源 + 仲裁**：

- **屏幕派生态**：`src/detect/mod.rs:10-20` 的 `AgentState{Idle,Working,Blocked,Unknown}`（注释：Idle=agent 完成、Working=工作中、Blocked=需人工输入、Unknown=普通 shell）；`AgentDetection` 聚合 visible_blocker/visible_working/visible_idle，检测手段 = 终端尾部屏幕模式匹配 + OSC title/progress。23 个 `Agent` 枚举各配匹配规则。
- **API 枚举**：`src/api/schema/common.rs:145-148` `PaneAgentState{Idle,Working,Blocked,Unknown}`、`:154-158` `AgentStatus{Idle,Working,Blocked,Done,Unknown}`（Done 只出现在 API 层，屏幕层没有）。
- **agent-native hook 作为权威**：`integration/` 为每个 agent 内嵌 `include_str!` 的 hook 资产；agent 通过 `HookStateReported` 上报真实状态（如 Kimi 的 `KIMI_HOOK_EVENTS`：UserPromptSubmit→working、AskUserQuestion→blocked、Stop→idle），优先于屏幕模式匹配。

**对 Clawith**：Clawith 的 run 状态同样**不在事实表上**（`agent_run.py` 无 status 列，见 §8），而是从 checkpoint `lifecycle.status` 推导：`command_worker.py:177-210` 的 `classify_checkpoint()` 把 checkpoint 归入 `not_started/runnable/execution_error_recoverable/waiting/terminal/inconsistent` 六态，且对「values/next/tasks/interrupts 互相矛盾」的 checkpoint 显式标 `inconsistent`——与 herdr 对「Unknown/模糊态」保守处理同思路。herdr 的「hook 权威 + 屏幕 fallback」对应 Clawith「checkpoint 权威 + 心跳/租约/stall 存活信号」（`event_stream.py:390-454` 的 `_worker_alive()` 多重存活信号判死，§8）。

## 3. 事件 API 与游标回放

herdr 的客户端事件分发是整个「多端接入」的骨架：

- **环形缓冲 + 单调序号**：`src/api/event_hub.rs` 的 `EventHub` 持 `next_sequence: u64`（`:8`）+ `MAX_EVENTS: usize = 512`（`:13`）环形缓冲；`events_after(sequence)`（`:28`）只回放 sequence 之后的增量——断线重连只补缺口，不重发全量。
- **订阅语义**：`src/api/subscriptions.rs`（842 行）poll-based 订阅，`ActiveEventSubscription` 维护自己的游标推进；`ActiveAgentStatusChangedSubscription` 对「订阅建立前的状态」做 setup-window 事件 + snapshot probe，保证新订阅方拿到当前态快照而非只拿增量。
- **传输层**：`src/api/server.rs`（1525 行）Unix socket 每连接一线程、一行一请求、`APP_RESPONSE_TIMEOUT=5s`、`dispatch_to_app_with_timeout` 防 app 侧卡死拖垮 socket。

**对 Clawith**：Clawith 的事件游标回放在 `event_stream.py`：`_event_statement()`（`:59-81`）用 `(created_at, id)` 复合游标 `after` 增量拉取（`or_(created_at > after.created_at, and_(created_at == ..., id > after.event_id))`），`stream_run()`（`:500-572`）循环 poll 推进游标。两者都是「游标 + 增量 + 缺口重补」，语义同构。herdr 的环形缓冲是内存态（重启即丢），Clawith 的事件持久化在 `agent_run_events` 表（`_TERMINAL_EVENT_TYPES` `:33`），Clawith 更持久。

## 4. live handoff：零停机 server 替换

这是 herdr 最独特、也最值得对照 Clawith「部署杀 run 全量重放」痛点的机制：

- **`HeadlessServer`**（`src/server/headless.rs:193-255`）持 clients HashMap、foreground_client_id、handoff_in_progress 等；`new()`（`:309-381`）、`run()` 事件循环。
- **`perform_live_handoff()`**（`src/server/headless/lifecycle.rs:25-228`）：暂停 PTY reader → 捕获 snapshot → spawn 新 import child → `duplicate_cloexec_fd` 复制 PTY master fd（`pty/backend.rs`）→ 经 Unix socket 用 **SCM_RIGHTS 传 fd** → **三阶段提交**（wait_ready → report_committed → wait_owned_ack）→ `drain_for_handoff()`（`:218`）把 pane 进程连同运行时状态移交新进程。结果：**pane 里的 agent 进程不重启、PTY 不重建**，只是 server 进程被替换。
- **`HandoffRuntimeState`**（`src/handoff_runtime.rs`）：pane_id/child_pid/rows/cols/input_state/terminal_title/initial_history_ansi——一个 pane 的完整运行时状态被序列化移交。

**对 Clawith**：这正是 Clawith「部署杀在途 run → 全量重放 → 分叉撞幂等账本」教训的对立面。Clawith 的选择是**不追求进程级热替换**，而是把「崩溃/被杀」变成可恢复常态：命令收件箱 `agent_run_commands` + 幂等键（§8）+ checkpoint 单权威，让部署后 worker 重放时靠 `claim_next_command` 的 FIFO+幂等收敛到同一终态。可借鉴 herdr 的「**三阶段提交 + fd 移交 + 显式 ready/ack**」契约，在 Clawith 未来做灰度/滚动升级时用于**验证「替换进程确实接管了同一批 pane/run 而非重放」**；但 herdr 是单机 fd 传递，Clawith 是跨进程/跨容器，直接搬不成立。

## 5. remote SSH thin-client + workspace git 集成

- **remote attach**：`src/remote.rs` + `src/remote/attach.rs`（3365 行）实现 `--remote` 模式——SSH 到远端自动安装 herdr 二进制，`SshStdioBridge` 桥接本地 GUI 与远端 server 的 stdio，`reattach_command` 与 live_handoff 远端升级组合，把「本机终端多路复用」扩展成「远端会话可随时 detach/reattach」。
- **workspace git**：`src/workspace/git/discovery.rs` 识别 `GitSpaceMetadata`/`GitWorktreeInfo`（repo_root/git_dir/git_common_dir/is_bare/is_linked_worktree），正确处理 linked worktree；`src/workspace/git/status.rs` 的 `GitStatusCacheEntry`+`GitStatusFingerprint` 用 mtime stamp 增量刷新，`git_status_cache_key` 缓存脏状态。

**对 Clawith**：herdr 的「thin client + 远端 agent 状态全在 server 侧」对应 Clawith 的「状态全在服务端、前端只做事件游标续传」。Clawith 多端接入 = Redis 背书的 `RealtimeRouter`（`realtime_runtime/router.py:21`）+ 前端 WS 断流恢复（`useGroupRealtime.ts:5`「forward cursor closes reconnect gaps，polling 只是降级 fallback」、`:14` 单次 catch-up 上限 10×50=500 条；`AgentDetailPage.tsx:2448-2453` 维护 `runtimeEventCursorRef` 游标、`:3417-3418` 从 `event_cursor` 续传）。git 集成对照 Clawith 的 workspace `.git` 物化（commit `4d3fe431`，见 §8），herdr 是「读 git 状态」，Clawith 是「沙箱 git 根治 + 凭证脱敏」。

## 6. 插件系统与 integration

- **integration 资产内嵌**：`src/integration/mod.rs` 用 `include_str!` 把每个 agent 的 hook 资产编译进二进制，版本常量内嵌。
- **registry + 版本 marker**：`src/integration/registry.rs`（495 行）管 17 个 integration target，`integration_status_at` 做版本检查，解析 `HERDR_INTEGRATION_VERSION=` marker——agent 侧集成脚本自报版本，herdr 侧据此判断 hook 是否过期需重装。
- **插件持久化**：`src/persist/plugin_registry.rs` 与 `plugins.json`（`persist.rs:5`）。

**对 Clawith**：herdr 的「内嵌集成资产 + 版本 marker 检查」对应 Clawith 的 skills/MCP 扩展：`plugins/base.py:16-30` 的 `ClawithPlugin` ABC（`register(app)` 装路由/工具钩子/后台服务）+ `clawith_acp` 插件（带 `plugin.json`）；`mcp_client.py` 的 `MCPClient`（`:24`）做 Streamable HTTP/SSE 双传输自动探测（`_detect_transport()` `:311`）、`list_tools`/`call_tool_result`。herdr 的「版本 marker 防 hook 过期」可对应 Clawith 的 skill/MCP 工具配置版本检查思路，但 Clawith 的扩展面（skills 渐进披露 + MCP 工具集 + 插件路由）比 herdr 的单一 hook 资产更广。

## 7. Clawith 侧对照（工具核实）

**表结构**（`backend/app/models/agent_run.py`、`agent_run_command.py`）：

- `agent_runs`：产品侧**身份/投递事实**表，**无 status 列**。`source_type ∈ {chat,trigger,task,a2a,heartbeat}`、`run_kind ∈ {foreground,background,delegated,orchestration}`、`delivery_status ∈ {not_required,pending,delivered,failed}`、`runtime_thread_id`、`graph_name/version`，排队 lane 三件套 `scheduling_lane_key`/`scheduling_position_created_at`/`scheduling_position_id` + `lane_held`/`lane_claimed_at`（`:140-148`）；幂等唯一索引 `uq_agent_runs_source_execution`（`:174-179`）、`uq_agent_runs_active_lane`（`:180-185`）。
- `agent_run_commands`：**持久化命令收件箱**，`command_type ∈ {start,resume,cancel}`、`status ∈ {pending,claimed,applied,rejected}`、`claim_expires_at`/`claimed_by`/`attempt_count`/`idempotency_key`/`deferred_until`（`:56-105`）；唯一约束 `uq_agent_run_commands_run_idempotency`（`:46`）。

**run 生命周期由 checkpoint + 生命周期事件推导**：终态事件集 `_TERMINAL_EVENT_TYPES = {run_completed,run_failed,run_cancelled}`（`event_stream.py:33`），`run_is_terminal()`（`:305-313`）取最新非投递生命周期事件判终态。

**调度/租约**（`persistence.py`、`command_worker.py`）：

- `claim_next_command()`（`persistence.py:701-739`）：`_claim_statement()`（`:585-662`）按 `(created_at,id)` 全局 FIFO + `with_for_update(skip_locked=True)` 抢最早命令；start 额外走 `_acquire_start_lane()`（`:665-698`）抢排队 lane（`uq_agent_runs_active_lane` 防双 holder）。认领后 `status=claimed`、`claim_expires_at=now+60s`（`AGENT_RUNTIME_COMMAND_CLAIM_TTL_SECONDS=60`，`config.py:175`）。
- **心跳续租**：`_heartbeat()`（`command_worker.py:520-561`）执行全程每 20s（`AGENT_RUNTIME_COMMAND_CLAIM_RENEW_SECONDS=20`，`config.py:176`）续租，连败 `AGENT_RUNTIME_COMMAND_HEARTBEAT_MAX_FAILURES=3`（`config.py:181`）次才停——修掉「60s 短租约跨 30min 图执行」反模式。
- **cancel pending 两层修复**：`reject_unstarted_run_for_cancel()`（`persistence.py:896-948`）把「早于 cancel 的 pending/过期 claimed start」同步置 `rejected`/`cancelled_before_start`，规避「worker 先 start 后 cancel」的 FIFO claim 竞态。
- **daemon 编排**：`running_runtime_worker_context()`（`worker_service.py:880-1003`）起 `AGENT_RUNTIME_COMMAND_CONCURRENCY=10` 个 `RuntimeCommandDaemon`（`:901-911`）+ `RuntimeCommandDaemonSupervisor`（`worker_service.py:542-631`，stall=300s 即 `AGENT_RUNTIME_COMMAND_STALL_SECONDS=300.0` `config.py:187`）+ ChannelDelivery/AsyncToolPoll/ToolLeaseReconcile 等旁路 daemon。Supervisor 靠 `last_active_at`（`worker_service.py:473`）+ 心跳 `_touch_liveness()`（`:475-483`）区分「长图执行」与「真卡死」，卡死 dump coroutine 栈（`:614-631`）。

**checkpoint 持久化**（`checkpointer.py`）：进程级共享连接池 `get_shared_checkpoint_pool()`（`:213-242`，min/max `CHECKPOINT_POOL_MIN_SIZE=1`/`MAX_SIZE=4` `config.py:122-123`）；`checkpoint_serializer()`（`:146-167`）allowlist msgpack 类型 + 可选 AES `EncryptedSerializer`；`runtime_command_config()`（`:59-75`）绑定 run/command 元数据。

**多端接入/断线重连**：后端 `RealtimeRouter`（`realtime_runtime/router.py:21`）Redis 背书 presence + 跨实例路由；前端 `useGroupRealtime.ts:5`「forward cursor closes reconnect gaps」、`AgentDetailPage.tsx:2448-2453` 游标 + `:3417-3418` `event_cursor` 续传。

**转交/委派**：`a2a_runtime.py` 的 `A2AMode = {notify,consult,task_delegate}`（`:44`），委派 run 落 `run_kind="delegated"`（`:667`/`:949`）。

**skills/MCP/插件**：`plugins/base.py:16-30` `ClawithPlugin` + `clawith_acp`；`mcp_client.py:24` `MCPClient` 双传输探测。

**workspace git**（commit `4d3fe431`，`fix(workspace-sandbox): .git 两侧放行 + 凭证脱敏`）：`backend/app/services/sandbox/workspace_policy.py` 的 DERIVED_SEGMENTS 放行 `.git`、`backend/app/services/agent_tools.py` 的 `redact_git_secrets` 剥离 https userinfo/extraheader 凭证、`_drop_incomplete_git_dirs` 维持「.git 要么完整要么不存在」不变量。

## 8. 可迁移点 → Clawith 映射

| # | herdr 机制（文件） | Clawith 对标点 | 可借鉴要点 |
|---|---|---|---|
| 1 | EventHub 环形缓冲游标回放（`event_hub.rs:8/:13/:28`） | 事件游标回放（`event_stream.py:59-81`） | 单调 `sequence` + `events_after` 补缺口语义；Clawith 已是 `(created_at,id)` 复合游标，可补「环形上限 + 越界告警」语义 |
| 2 | 双重状态来源：hook 权威 + 屏幕 fallback（`detect/mod.rs:10-20`） | run 状态机 + stall 检测（`classify_checkpoint` `command_worker.py:177-210` + `_worker_alive` `event_stream.py:390-454`） | 权威信号（hook/checkpoint）为主、环境信号（屏幕/心跳/租约）为辅判存活；Clawith 已有多重存活信号，可参考「显式仲裁优先级」文档化 |
| 3 | 结构态/高 churn 态分离 + 版本化迁移 + 未来版本拒绝 + 原子写（`persist.rs:3-5`/`snapshot.rs:447-456`/`io.rs:48-61`） | checkpoint 持久化（`checkpointer.py:170-195`） | 「产品事实表 vs checkpoint 大对象」分离已落地；可补「snapshot 版本号 + 未来版本显式拒绝」到 checkpoint 元数据 |
| 4 | agent_resume `dedupe_key` 会话去重（`agent_resume.rs:238-243`） | 幂等账本（`uq_agent_run_commands_run_idempotency` `:46` + source 幂等 `:174-179`） | 两者同为「同源同键只处理一次」；Clawith 已更严（含 mismatch 报错 `_require_exact_command_retry`） |
| 5 | live_handoff SCM_RIGHTS fd 移交 + 三阶段提交（`lifecycle.rs:25-228`） | 部署杀 run 全量重放痛点 | 借鉴「wait_ready→report_committed→wait_owned_ack」验证新进程**接管而非重放**；跨容器直接搬不成立 |
| 6 | integration hook 内嵌 + 版本 marker 检查（`integration/registry.rs` `HERDR_INTEGRATION_VERSION=`） | skills/MCP 扩展（`mcp_client.py:311` 传输探测 + `plugins/base.py:16-30`） | 「agent 侧集成自报版本、平台侧判过期重装」可对应 skill/MCP 工具配置版本漂移检测 |
| 7 | remote SSH thin-client + bridge（`remote/attach.rs` `reattach_command`） | 多端接入/断线重连（`RealtimeRouter` `router.py:21` + 前端 `event_cursor` `AgentDetailPage.tsx:3417-3418`） | 「状态全在服务端、客户端只续游标」已是共识；可参考「远端升级 reattach」的显式握手 |
| 8 | fail-soft：原子写、cwd 缺失回退 HOME、单 pane 失败不拖垮恢复（`io.rs`/`restore.rs`） | fail-open 语义（`current_lane_admission` `event_stream.py:257-302` 探针失败放行、单命令失败不杀 daemon `worker_service.py:492-517`） | 一致：探测/恢复失败不破坏主路径，单点坏记录跳过不拖垮整体 |

## 9. 局限（诚实记录）

- herdr 是**单机单用户终端 multiplexer**：无多租户隔离、无认证、无审批、无分布式调度；其队列/锁/一致性几乎无实现可抄，仅持久化/事件/状态判定的**设计契约**可迁移。
- **「handoff」是 false friend**：herdr 的 `handoff_runtime`/`perform_live_handoff` 是 **server 进程级热替换**（pane 进程存活、PTY fd 移交），**不是** agent 间委派/转交；Clawith 的 agent 间委派是 `a2a_runtime` 的 `task_delegate`（`run_kind=delegated`），二者概念正交。本文 §4/§8.5 按「部署热替换」口径对照。
- 事件环形缓冲 `MAX_EVENTS=512` 是**内存态**，重启即丢；Clawith 事件已持久化到 `agent_run_events` 表，持久性更强——该机制对 Clawith 的借鉴价值主要在「游标 + 上限」语义，而非实现。
- 本次未深入：`remote/attach.rs` 后 1700 行（远端升级细节）、`workspace.rs` 的 `WorktreeSpaceMembership`、`pane/terminal.rs` 的 `TerminalState` 状态机细节、`app/session.rs` 的 session 保存调度、Clawith 前端 Direct/Group WS 状态机的完整状态迁移。
- herdr 的「屏幕模式匹配判状态」强依赖终端渲染文本，对 Clawith（checkpoint 结构化状态）无直接迁移价值，仅作为「无权威信号时的保守 fallback」参考。
