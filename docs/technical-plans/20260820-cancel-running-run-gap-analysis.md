# cancel 命令对「运行中 run」不生效 — 深度分析 + 现栈复现（后端 + 前端）结论

日期：2026-08-20（含后端直连复现 + 前端 Playwright 复现）
状态：研究结论——**后端 cancel 链路完好；前端 stop 按钮只在「运行中」渲染；另发现「排队中无法取消」的真实缺口**
关联事故：run `16e8088f`（Android工程师 4，goal=重新编译，2026-08-19 09:52–10:04 成功型死循环）

---

## 1. TL;DR

初版假设（lane_held 守卫挡入队 / command worker 不 claim）**均被复现证伪**。真实结论分三层：

1. **运行中 run 的取消是好的**：发 `/ws/chat` `{"type":"abort","run_id":…}` → 后端回 `cancel_requested` → cancel 入 `agent_run_commands` → 协作式 `get_cancel` 在下一节点边界中止 run（`start→rejected/cancelled_before_apply`、`cancel→applied`）。延迟≈一次在途 model LLM 调用（实测 ~12s）。
2. **前端 stop 按钮只在「运行中」渲染**：`AgentDetailPage.tsx:7240` 的 `{activeRun?.canCancel && <button …/>}`，而 `canCancel` 来自 `get_session_runtime_state`，后者只在 `lane_held=True`（start 已被 claim）时返回 `active_run`。
3. **新发现——「排队中」无法取消**：run 从「发送」到「start 被 claim」之间有一个 `pending` 窗口，此窗口内 `lane_held=False`、前端无 stop 按钮、且后端 `_cancel_runtime_run` 的 `if not run.lane_held` 守卫会拒绝 cancel（尽管命令 worker 本可用 `reject_unstarted_run_for_cancel` 拒掉排队 start）。实测该窗口在命令 worker 被心跳风暴（13:01:23 一次排队 9 个 heartbeat run，每个跑多轮模型）占用时可长达 **2.5 分钟以上**。

## 2. 复现记录

### 2.1 后端直连复现（容器内 JWT 直连 /ws/chat + 查 DB）
- **起点取消**：`start` 变 `claimed` 即发 abort → `cancel_requested` → cancel `pending` → `start→rejected(cancelled_before_apply)` → `cancel→applied`，全程 ~3s。
- **循环中取消**：观察到 1 条 `read_file` 后发 abort → cancel `pending` 持续 ~12s（在途 model 调用）→ `tool_call running` → `run_cancelled` → start rejected + cancel applied。

### 2.2 前端 Playwright 复现（`localhost:3008`，token 注入登录）
- 直连 `/agents/27d55a64…/chat`，发「重新编译项目」触发 run。
- 观察 `.btn-stop-generation`（stop 按钮，icon-only，title=Stop）**在 run `pending` 阶段不渲染**；DB 同步显示 `start=pending、lane_held=false`。
- 该 `pending` 阶段持续 **2.5 分钟以上**（13:01:46 → 13:04+），根因是命令 worker 被 13:01:23 的 9 个 heartbeat run 占用（每个 heartbeat 跑多轮模型）。

## 3. 证伪与确认

| 假设 | 裁决 |
|---|---|
| Gap B：`lane_held` 守卫挡入队（运行中） | **证伪**（运行中 lane_held=True，守卫放行） |
| Gap A：`earlier_unfinished` 挡住 claim | **属实但无害**（运行中停靠靠协作式路径，worker 不必 claim） |
| 「排队中」无法取消（新） | **确认，真实缺口**（两层：前端无按钮 + 后端 lane_held 守卫误拒） |

## 4. 事故真因（仍未完全闭环）

事故 run 16e8088f 全程「运行中」（start claimed 09:52→10:06，lane_held=True），stop 按钮本应渲染、点击后本应入队并协作中止。但 DB 无任何 cancel 行。现有复现**无法复现**该「运行中点了取消却没入队」——最可能：
1. 用户当时未点 stop 按钮（而是打字「停止」当普通消息，Web Chat 无飞书那种「回复停止」指令）；或
2. 当时（09:59）前端存在现已修复的瞬时 bug（历史日志已随容器重建丢失，无法坐实）。

## 5. 修正后的修复建议

- **原计划「放宽 claim / lane_held 守卫（针对运行中）」不需要做**。
- **可做（新缺口）：「排队中可取消」**：
  - 后端 `_cancel_runtime_run`：把 `if not run.lane_held` 改为「允许 pending 状态的排队 run 发 cancel」（command worker 的 `reject_unstarted_run_for_cancel` 已能安全拒掉排队 start）。
  - 前端：`get_session_runtime_state` 在 `pending` 阶段也返回 `active_run`（status="queued"，can_cancel=true），让 stop 按钮在排队期也可点。
- **可选增强（非修复）**：`model_service.complete_once` 的 LLM 调用外 asyncio 竞态轮询 cancel，实现秒级停。

## 6. 复现残留（测试栈，无害）

session f23045c7 新增若干「重新编译项目」测试 run（06b1c71e/e0832d74/8c51de39 已 cancelled/completed；01cc7c13 已后端 cancel 待 worker 拒掉排队 start）。均 lane_held=False，无需人工清理。

## 7. 代码位置索引

- 后端：`persistence.py` `_claim_statement`(585)/`_acquire_start_lane`(661)/`enqueue_cancel`(561)/`reject_unstarted_run_for_cancel`(863)；`command_worker.py` `_process_locked`(783)；`cancel_source.py` `get_cancel`(61)；`node_executor.py` `_control_guard`(493)；`websocket.py` `_cancel_runtime_run`(977)/`_run_runtime_and_stream`(1114)；`chat_sessions.py` `get_session_runtime_state`(402)
- 前端：`AgentDetailPage.tsx` stop 按钮(7240)/`fetchSessionRuntimeState`(2482)/`dispatchChatMessage`(3703)；`sessionRuntimeState.ts` `sessionActiveRunFromResponse`(38)
