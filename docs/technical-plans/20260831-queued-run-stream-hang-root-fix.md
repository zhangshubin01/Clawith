# Direct Chat「排队 run 流悬挂 → resume 实时事件 8 分钟无法到达前端」根治方案

- 日期：2026-08-31
- 范围：`backend/app/api/websocket.py`（`message_loop` / `_run_runtime_and_stream` 调度侧）+ `event_stream.py` 新增车道准入探测（service 层）；执行层（`scheduling_lane.py`、`command_worker.py`）零改动
- 状态：已定稿（grill 第 1 轮 + 四视角并行审查第 2 轮结论均已并入，见 §7），待实施
- 关联：`20260831-ws-attach-waiting-idle-timeout-root-fix.md`（waiting 120s 误杀，另一事故，已部署）；证据链见 Langfuse 埋点（下文 §1）

## 1. 事故实录与埋点证据（2026-08-31，UTC）

session `e47a3f7e`（direct chat，agent Android 工程师 07）：

| 时间 | 事件 |
|---|---|
| 09:52:34 | run `6b6dd377` 创建并执行（计算器项目） |
| 10:06:31 | 用户发「在重新尝试」→ 车道被占 → 排队为新 run `d94aef3f`（start 命令未认领） |
| 10:12:05 | `6b6dd377` 停驻 waiting_user，向用户提问 |
| 10:19:16 | 用户回复「提交吧」→ resume 命令 `b7aa91fe` 入队并被 worker 立即认领 |
| **10:19–10:27** | **前端零流式更新，一直「思考中」**；后端持续工作 |
| 10:25:53.4 | `6b6dd377` 完成收尾（MR 已建、产物已推送）→ 车道释放 |
| 10:25:53.9 | `d94aef3f` 认领车道执行 → 10:27:05.9 完成 → 历史事件爆发式重放到前端，画面恢复 |

**Langfuse 埋点直方图（每分钟 span 数，= 后端真实活动心跳）：**

| 分钟 (UTC) | `6b6dd377`（resume trace `385e17a9`） | `d94aef3f`（排队 trace `196bf46b`） |
|---|---|---|
| 10:06–10:18 | — | **0 × 13 分钟**（start 从未被认领） |
| 10:19 | 24 | 0 |
| 10:20–10:25 | 8 / 19 / 24 / 42 / 43 / 43 | 0（10:25 起 6） |
| 10:26–10:27 | 0（已结束） | 56 / 2（完成） |

两条根 span 间隔仅 513ms（10:25:53.388 → 10:25:53.901）= 车道「终态释放 → 排队认领」的交接时序。前端侧旁证：nginx runtime-state 轮询恰在 10:27:06 停止。**结论：后端无真空，冻结是投递层故障。**

## 2. 根因链（代码级）

1. **单 socket 串行消息泵**：`message_loop`（websocket.py:529-584）一次只跑一个流；`_run_runtime_and_stream`（1132-1223）在流未结束时循环 `receive_json`，期间收到的新消息只入 `queued_messages`/`pending_runs`。任何一条流悬挂 = 泵整体停摆。
2. **排队 run 的流可以无限悬挂**：`6b6dd377` 停驻 waiting 后（10:12:05），泵弹出 `d94aef3f` 起流。其 start 命令永远无法被认领——车道只在终态释放（`scheduling_lane.py:15` `_TERMINAL_STATUSES={completed,failed,cancelled}`，waiting 不算），等待中的 `6b6dd377` 永久持车道。而 `_worker_alive`（event_stream.py:218-282）的「command pending」分支把该悬挂流判为存活，120s idle 杀不掉。
3. **resume 被排在这个悬挂流之后**：10:19:16 的 resume 持久化入队、worker 正常执行（Langfuse trace 10:19:17.9 起），但它的流排在 `pending_runs` 里、悬挂流之后 → socket 零事件 8 分钟。
4. 前端 `showDirectRunThinking` 发消息置 `isWaiting=true`，只有 WS 包能清 → 「思考中」冻结到 10:27:06 重放爆发。

**关键不变量（本方案的地基）**：车道持有者最多一个，且只有它能「执行」或「停驻 waiting」——waiting 的 run 持有车道是有意设计（见 §3.3 论证）。因此其余 run 此刻必然零事件。

## 3. 根治方案

设计原则：**消息泵只流「此刻可能产生活动」的 run；不可能活动的 run 在泵外挂起等待，不占用泵。** 投递串行与执行串行保持同构（车道本来就把执行串行化了），无需多路复用即可根治本事故类。

### 3.1 层 1（主修复）：车道感知的流调度

**探测（service 层新方法 `current_lane_admission(handle)`，落在 `event_stream.py` 的 `DatabaseRuntimeEventStream` 旁，与已部署的 `current_waiting_boundary` 同模块先例一致；api 层不做裸 select）：**

一次 lane-key 范围查询（同 `scheduling_lane_key` 下未终态 run 的 `lane_held`/`created_at`，行数 ≤ 队列深度），裁决：

```
本 run lane_held                                   → stream（resume 目标 / 执行中）
本 run 已终态（最新生命周期事件 ∈ 终态集）          → stream（终态 run 无需车道，重放不得被占道邻居挂起）
存在其他 lane_held run                             → defer（车道被占，start 无法认领）
车道空 且 本 run 是 lane 内最早未终态 run          → stream
车道空 但 存在更早的未终态 run（跨 tab/重连残留）  → defer（更早者先认领，防空等流复挂）
```

- 「最早 start」判定（第 4 分支）是审查新增：跨 socket（多 tab）下 worker 按 FIFO 认领最早的 pending start，「lane 空」不等于「本 run 下一个被认领」；不加此判定会在多 tab 下复现空等悬挂（审查 F1/F5）。
- 「本 run 已终态 → stream」分支是实施阶段双轴审查（Standards+Spec）新增：attach 重连终态 run 时其重放必须立即投递，不能被邻居 waiting run 的占道 defer 到无限期；顺带自愈「排队 run 被 cancel 成终态后仍挂在 deferred」。
- 探测失败（DB 瞬时抖动）：**短退避重试 3 × 0.5s，仍失败才 fail-open**（照旧起流）。重试吸收瞬时抖动，避免「这次探测失败、后续流内读正常」的悬挂回归窗口（审查 F6）。持久故障时流内轮询会立刻抛错走异常分支，不受影响。

**泵控制流（`message_loop` 重构，审查 F3）：**

- 挂起项与 `pending_runs`（流内 followup 队列）**分队列**：`deferred` 独立 deque。
- 循环结构：有挂起项时 `receive_json` 包 `asyncio.wait_for(..., timeout=2s)`——**消息到达优先处理（resume 即时）**；超时才 peek 队首重探（不 pop）。
- **defer 不 append+continue**：那会退化为紧 DB 忙循环（每条 2 次 SELECT、无节律、不再收消息）。defer 只入 `deferred` 队列后回到收消息循环。
- 流结束回环是主重探点（不空等 2s）；2s 定时器只是空闲兜底，顺带自愈「多 tab 空闲 socket」「abort 取消车道持有者后排队 run 被认领」两条边。

**defer 落地补发 `runtime_status(event="queued")` 包**：修正原稿引用错误——`websocket.py:1178-1185` 的 queued 包只在「另一 run 流进行中收到新消息」时发送，空闲泵 defer 的新鲜 intake 原本无任何包（审查 ②③④ 三路独立命中同一错误）。前端 `AgentDetailPage.tsx:3427-3432` 已能消化该包、`isWaiting` 残留由「waiting 提问卡片在场」兜底可接受；前端「排队中」渲染优化并入层 2 票。**「前端零改动」在补发该包的前提下成立。**

**其余语义：**

- **abort 移除（审查 F4）**：`abort` 命中挂起项时先探本 run start 命令状态——`claimed/applied`（执行中）→ 改为起流投递而非移除；`pending` → 移出队列 + 走既有 cancel 命令路径（pending-run cancel 已有两层修复，见记忆 `cancel-running-run-gap`）。
- **流异常回队（审查 F8）**：`_run_runtime_and_stream` 异常分支（1194-1211）返回 `(None, queued_messages)` 时，若该 run 仍非终态 → 回 `deferred` 队重探（上限 2 次，超过打 warning 放弃，依赖 attach 兜底）——避免「流被杀」扩大成「投递永久丢失」。
- **可观测性**：仅 defer 决策打一条日志 `[WS] stream_gate decision=defer run_id=%s lane_holder=%s`（stream 决策不记——每起流一条是噪音；对齐既有 `[WS] attach_run` 日志风格，trace_id 由 contextvar 自动注入）。
- **挂起队列无上限**：intake 已过配额检查、深度被用户行为天然封顶；不做 `stream_queue_full` 拒绝（YAGNI，新错误码需前端接）。defer 期间 run 终态/被删由「重放终端事件 / `_require_run` 抛 `run_not_found` → error 包」自然消化。

### 3.2 层 2（后续演进，独立票）：投递与执行解耦

对齐 OpenHands agent-server 的事件总线形态（§4 参考 2）：`stream_web_chat_run` 升级为 per-run 流任务，泵用 `asyncio.wait(FIRST_COMPLETED)` 多路复用（fan-in）；前端在 runtime-state 轮询发现新 run 执行而本 socket 无对应流时补发 `attach_run`（按需附着，不止 `ws.onopen`），以及「排队中」渲染优化。层 1 已闭环本事故类，层 2 属健壮性演进，不阻塞本次交付。

### 3.3 明确不做（附理由）

- **不做「waiting 释放车道」**：waiting 的 run 挂在共享 `runtime_thread_id` 的 checkpoint 上（LangGraph 官方语义：`thread_id` 是持久化游标，interrupt 后状态落盘、无限期等待 resume——见 §4 参考 1）。若 waiting 即放车道，其他 run 在同 thread 上执行会把 parked run 的 checkpoint 覆盖/污染，resume 点丢失——正是已根治过的「多 run 共 thread 上下文污染」（记忆 `direct-chat-run-boundary-fix`）。保留「waiting 持车道」是有意设计。
- **不做「悬挂流加认领超时杀流」**：杀流 = 丢失该 run 后续全部投递（执行照常进行），且 command-pending 保活本来就是对的（排队 run 终将执行，今天 10:25:53 已证）。挂起在泵外即可，无流可杀。
- **不做「有界认领观察窗」**（流内 hook 监测 start 认领超时自终）：TOCTOU 已由「最早 start」准入 + 探测重试覆盖，流内 hook 侵入流语义、价值边际。
- **不做挂起期心跳包**：defer 落地补发的 `queued` 包 + runtime-state 轮询已是双重锚点。
- **不做挂起超时告警 / 队列上限 / rejected 终态移队**（审查第 2 轮裁剪）：取证由 waiting 保活 warning + attach cursor 日志 + Langfuse 直方图替代；深度自然有界；rejected 写入点只对 claimed 生效、非 WS cancel 的残留项自愈。
- **不引入 checkpoint 读取**：探测只读 `AgentRun.lane_held`/`created_at` + `AgentRunCommand.status`，延续「polling over stable product events, never checkpoint internals」模块原则。

## 4. 参考对照（reference-check）

| 决策点 | 依据 | 出处 |
|---|---|---|
| waiting 是合法无限期停驻、resume 是一等公民 | 「waits indefinitely until you resume」；resume 用 `Command`；推荐以 event streaming 驱动可能 interrupt 的图 | LangGraph 官方 `interrupts.md`（https://docs.langchain.com/oss/python/langgraph/interrupts.md ，2026-08-31 fetch 核对） |
| 泵只流「可活动」run、执行与投递解耦的目标形态 | `EventService`+`PubSub`/`Subscriber`：WS 端点只订阅转发事件，从不驱动执行；断线用 `resend_mode='since'`+`after_timestamp` 游标续传；多订阅者并存（`pub_sub.py`、`sockets.py`、`event_service.py`） | OpenHands `software-agent-sdk`（本地 `/Users/shubinzhang/Documents/UGit/software-agent-sdk`，2026-08-31 核对） |
| per-run 流对象、流式结果可转状态再 resume | `RunResultStreaming.to_state()`（result.py:1142）从流式结果生成 RunState 恢复执行 | `openai-agents-python`（本地 `/Users/shubinzhang/Documents/UGit/openai-agents-python`） |

**偏离记录**：OpenHands 是「事件总线 + 无状态 WS 订阅者」的全解耦架构，本方案层 1 刻意保持「串行泵 + 车道感知调度」——因为 Clawith 的车道把执行严格串行化，投递串行与执行串行同构，改动面最小（ponytail 原则）；解耦形态留作层 2 演进票，避免一次性重写消息泵。**未覆盖类别**：deep-agents（无调度/车道机制可参照，本轮不涉及子 Agent 编排）；SWE-bench/Terminal-Bench 等评估基准（传输层故障修复，无能力口径变化，不适用）。

## 5. 宪法逐条检查（C1–C6，取自 `scripts/arch-guard.sh`；宪法原文在 `.specify/memory/constitution.md`）

| 规则 | 检查结论 |
|---|---|
| C1 模块边界 | 探测下沉 `event_stream.py` service 层（对齐 `current_waiting_boundary` 先例），api 层不新增裸 select ✓ |
| C2 DAO/查询集中 | 探测查询封装在 service 层单一方法内 ✓（arch-guard 计数不新增 warning） |
| C3 幂等/投递 | 不改 delivery 幂等；queued 包补发是幂等 status 包；流异常回队有上限 ✓ |
| C4 前端零改动 | 成立，前提=defer 落地补发 `queued` 包（前端 3427-3432 已能消化，无需新代码）✓ |
| C5 无新表/迁移 | 只读探测，无 schema 变更 ✓ |
| C6 改动行数/范围 | 集中在 `websocket.py` message_loop + `event_stream.py` 一个方法 + 测试 ✓ |

## 6. 测试清单

`test_websocket_runtime_chat.py`（fake stream / fake DB 行；**loguru 断言用 `patch("app.api.websocket.logger", new=MagicMock())`，不用 caplog**——该文件 1122-1124 已有此先例）：
- 排队 run（lane 被他人持有）→ defer + 补发 `queued` 包；泵不阻塞，期间收到 resume → resume 立即流。
- 车道释放后流结束回环重探起流；挂起期间收到消息不丢（wait_for 超时续接 receive，显式覆盖「超时与消息到达同时发生」竞态）。
- **defer 不忙循环**：defer 后立即回到收消息循环（fake receive 计数断言，无连续探测风暴）。
- **最早 start 准入**：lane 空但存在更早未终态 run → 仍 defer；本 run 是最早 → 流。
- abort 命中挂起项：start pending → 移除 + cancel 命令入队；start claimed → 改起流。
- 流异常且 run 非终态 → 回队重探（≤2 次）；超过 → warning + 放弃。
- 探测连续失败 → 3 次重试后才 fail-open（fake 时间/计数断言）。
- lane_held=True 的 intake（resume）永不 defer；lane 空闲且最早的 fresh intake 立即流。
- defer 期间 run 已完成 → 起流重放终端事件；defer 期间 run 被删 → `run_not_found` error 包。
- onboarding_trigger 在他人持车道时被 defer、释放后正常 greeting。
- defer 日志断言（`[WS] stream_gate`，run_id + lane_holder）；stream 决策无日志（负向）。

回归：**本文件既有 `message_loop` 直接驱动用例全量**（372 native_message、788 abort_enqueues、979 cancel_after_waiting、1059 followup_durably_accepted 等 12+ 条——控制流重构直接触及）+ `test_agent_runtime_event_stream.py` / `chat_stream.py` / `chat_intake.py`（流语义未动，应全绿）；全量后端基线 + `scripts/arch-guard.sh`。

## 7. 部署与验证

- 单 commit 单部署（测试环境红线：不灰度）。
- 复现路径：waiting run 驻留 → 发新消息排队 → 回复 waiting run → 断言 ① 排队消息收到 `queued` 包；② resume 事件 ≤2s 起流（不冻结）；③ 排队 run 执行期事件实时到达而非事后重放。
- 埋点验证（本次建立的证据法复用）：Langfuse `queryMetrics` 按 `metadata.run_id` 拉每分钟 span 直方图，与 WS 包时间线对齐；nginx runtime-state 轮询在 resume 流恢复后立即停止。

## 8. 评审决定记录

### 第 1 轮（grill，2026-08-31）

用户逐题拍板「按推荐」：

- **Q1 队列深度**：上限 8 条，超出拒绝新消息并回 `stream_queue_full` error 包。（第 2 轮审查后**撤回**，见下）
- **Q2 挂起心跳包**：不加（`queued` 包 + runtime-state 轮询双重锚点已够）。
- **Q3 探测失败语义**：fail-open（投递优先，最坏退化为旧行为）。（第 2 轮审查后**细化为退避重试后 fail-open**）
- **Q4 挂起超时告警**：超 10 分钟 `logger.warning` 取证，不自动动作。（第 2 轮审查后**撤回**）
- **Q5 重连缺口**：本期接受 + 文档明示；补挂根治并入层 2 演进票。
- **Q6 rejected 终态**：并入探测，移出队列 + error 包。（第 2 轮审查后**撤回**）
- **Q7 范围**：只修 Direct Chat；群聊同型悬挂审计另开票。
- **Q8 落地方式**：直接 implement（red-green + code-review 两轴审查 + 单 commit 单部署）。
- 关联 ADR：`docs/adr/0012-direct-chat-stream-pump-lane-aware-scheduling.md`。

### 第 2 轮（四视角并行审查，2026-08-31）

- **① 并发竞态（high）**：采纳 F1/F5「最早 start」准入判定、F3 控制流重构（防忙循环）、F4 abort 竞态改起流、F6 探测退避重试、F8 流异常回队（上限 2 次）、F9 补测试、F10 修正 queued 包引用。
- **② 标准/宪法**：采纳——探测下沉 service 层、caplog→patch logger、回归补全 test_websocket_runtime_chat.py 既有用例、补宪法逐条检查节（§5）、日志对齐 `[WS]` 前缀、修正 `_worker_alive` 行号漂移。
- **③ ponytail**：采纳——砍队列上限+拒绝包（Q1 撤回）、rejected 移队（Q6 撤回）、10min 告警（Q4 撤回）、每流 info 日志（只留 defer 日志）；**驳回**「砍 2s 定时器改纯事件驱动」——保留 `wait_for` 定时器（一行实现、顺带自愈多 tab/abort 空闲泵两条边；纯事件管道代码更重）。
- **④ 机制交互**：确认群聊走 `group_websocket.py` 不经过 `_run_runtime_and_stream`（范围成立）、onboarding greeting 车道必空、resume 与泵零耦合、after=None 重放与边界探测无冲突；采纳 F2/F1——defer 补发 `queued` 包、`isWaiting` 残留由 waiting 卡片兜底、前端渲染优化并入层 2。

### 第 3 轮（实施阶段双轴 code-review，2026-09-01）

Standards + Spec 两轴并行子代理审查实施 diff 后修正：

- **Spec 轴**：① 补「流结束回环是主重探点」（`_pick_next_intake` 入口立即重探队首，2s 只作空闲兜底）+ 对应测试；② 补 F8「run 非终态才回队」前置（新增 service 层 `run_is_terminal` 探测）；③ 退避参数与方案对齐为 3×0.5s（共 4 次尝试）。
- **Standards 轴**：④ `_start_command_status` 下沉 event_stream.py service 层（`current_start_command_status`），api 层只留 fail-open 包装并文档化「失败偏向 claimed 复活」默认值；⑤ 探测 docstring 改述——worker 认领查询仍是唯一权威，探测只读车道事实做投递过滤，非第二权威；⑥ 探测新增「本 run 已终态 → stream」分支（attach 重连终态 run 不被占道邻居挂起）；⑦ 修 pyright 新增报错（`_dispatch_client_packet` 拆分收窄 + tuple_ 定点 ignore）、`_MAX_STREAM_REQUEUE_ATTEMPTS` 命名常量、日志统一 f-string、重试常量上移、返回类型收窄为 `Literal["stream", "defer"]`。
- 未采：超时/消息竞态显式用例（3.14 `asyncio.wait_for` 语言层已无该竞态）、attach 复用 `is_onboarding_trigger` 表达「无 user 消息」改名（最小改动，注释已释）。
