# 「排队中 run 无法取消」修复方案

日期：2026-08-20
状态：方案（待评审后实施）
前置研究：`docs/technical-plans/20260820-cancel-running-run-gap-analysis.md`

---

## 1. 背景与结论回顾

双复现已证：**后端 cancel 链路对「运行中」run 是完好的**（abort → cancel_requested → cancel 入队 → 协作式 `get_cancel` 中止 → `start→cancelled_before_apply`、`cancel→applied`）。

真正缺口在「**排队中（pending）**」阶段：run 从「发送消息」到「start 被 command worker claim」之间有一个 pending 窗口，此窗口内：

1. **前端无 stop 按钮**：`get_session_runtime_state` 只查 `lane_held=True`，pending 阶段 `lane_held=False` → 返回 `active_run=None`。
2. **后端 guard 误拒 cancel**：`_cancel_runtime_run` 的 `if not run.lane_held` 把 pending（排队）当成「已非活跃」拒绝。
3. **（更深一层）cancel 即便入了队也抢不过 start**：`claim_next_command` 按 `created_at` FIFO，start（更早）先被 claim，cancel 被 `earlier_unfinished`（start 已 claimed）挡住，直到 start 跑完 → cancel 才 claim 到 → `already_terminal`。即「只放开 guard」仍停不住排队 run。

该 pending 窗口在命令 worker 被心跳风暴等批量任务占用时，实测可长达 **2.5 分钟以上**。

## 2. 修复总览（三部分，缺一不可）

| Part | 层 | 改动 | 作用 |
|---|---|---|---|
| 1 | 后端 intake | `websocket._cancel_runtime_run` 守卫放宽 | 让排队 run 能发 cancel |
| 2 | 后端 intake | `adapter.cancel_run` 同步拒绝 pending start | 让 cancel 真正抢占 start（run 不启动） |
| 3 | 后端 state | `chat_sessions.get_session_runtime_state` 返回排队 run | 让前端渲染 stop 按钮 |

> 前端**无需改动**：`sessionActiveRunFromResponse`（`sessionRuntimeState.ts:38`）已把 `status` 非 terminal 且 `can_cancel===true` 映射为 `canCancel=true`，对 `"queued"` 状态天然成立；stop 按钮 `AgentDetailPage.tsx:7240` 只看 `activeRun?.canCancel`。

---

## 3. Part 1 — `_cancel_runtime_run` 守卫放宽

文件：`backend/app/api/websocket.py`，`_cancel_runtime_run`（当前 977–1060）。

当前（1047 行）：
```python
if not run.lane_held and existing is None:
    raise ChatRuntimeIntakeError(
        "chat_cancel_not_lane_holder",
        "Cancel target is no longer the active Direct Chat Run",
    )
```

改为：只有当 run **既非 lane holder、又无 pending/claimed 的 start（即已终结）** 时才拒绝，排队/运行/等待均可取消：

```python
cancellable_start = await db.execute(
    select(AgentRunCommand.id).where(
        AgentRunCommand.tenant_id == agent.tenant_id,
        AgentRunCommand.run_id == run.id,
        AgentRunCommand.command_type == "start",
        AgentRunCommand.status.in_(("pending", "claimed")),
    ).limit(1)
)
if (
    not run.lane_held
    and existing is None
    and cancellable_start.scalar_one_or_none() is None
):
    raise ChatRuntimeIntakeError(
        "chat_cancel_not_lane_holder",
        "Cancel target is no longer the active Direct Chat Run",
    )
```

语义矩阵（scope 校验已保证 run 属于本 session/agent/user）：

| run 状态 | start 状态 | lane_held | 是否放行 |
|---|---|---|---|
| 排队 | pending | False | ✅（有 pending start） |
| 运行 | claimed | True | ✅（lane_held） |
| 等待用户 | applied | True | ✅（lane_held） |
| 已终结 | applied/rejected | False | ❌（无 pending/claimed start） |

## 4. Part 2 — `cancel_run` 同步拒绝 pending start

文件：`backend/app/services/agent_runtime/adapter.py`，`RuntimeCommandIntake.cancel_run`（当前 366–379）。

当前：
```python
async def cancel_run(self, command: CancelRunCommand) -> RunHandle:
    run = await self._get_run(tenant_id=command.tenant_id, run_id=command.run_id)
    self._require_existing_v2(run)
    enqueued = await enqueue_cancel(...)
    return self._handle(run, enqueued.command, created=enqueued.created)
```

改为：入队 cancel 后，若该 cancel 是新命令，**同步调用 `reject_unstarted_run_for_cancel`** 把仍在 pending 的 start 立即拒绝（复用 worker 已有的同一函数，幂等）：

```python
    enqueued = await enqueue_cancel(...)
    if enqueued.created:
        # 排队 run 的 start 仍是 pending：在 intake 事务里直接拒绝它，
        # 让 cancel 真正抢占 start（否则 worker 按 FIFO 先 claim start，
        # cancel 只能等 start 跑完后 already_terminal）。
        await reject_unstarted_run_for_cancel(
            self._db,
            tenant_id=command.tenant_id,
            run_id=command.run_id,
            cancel_command_id=enqueued.command.id,
        )
    return self._handle(run, enqueued.command, created=enqueued.created)
```

说明：
- `reject_unstarted_run_for_cancel`（`persistence.py:863`）只拒绝 `pending` / `claimed 且已过期` 的 start；对运行中（claimed 未过期）的 start 是 no-op —— 所以对「运行中/等待中」run 调用它是安全的（那类场景仍走协作式 cancel）。
- worker 侧已有的 `_reject_unstarted_start`（`command_worker.py:506`）调用变幂等：intake 已拒绝后，worker claim cancel 时 `read_latest` 仍为 None，`reject_unstarted_run_for_cancel` 找不到 pending start（已 rejected）→ 仅 `_mark_applied(cancel, None)`。
- 影响面：`cancel_run` 是 Web/飞书/Group 三个入口（`websocket.py:1052`、`feishu.py:95`、`groups.py:1185`）共用，全部受益。

## 5. Part 3 — `get_session_runtime_state` 返回排队 run

文件：`backend/app/api/chat_sessions.py`，`get_session_runtime_state`（当前 402–556）。

当前（443–455）只查 `lane_held=True` 的 holder，无 holder 即返回 `active_run=None`。改为：无 holder 时，**再查该 session 最新一条 `start` 仍为 `pending` 的 run**，作为排队中的活跃 run：

```python
if not holders:
    pending_result = await db.execute(
        select(AgentRun)
        .join(AgentRunCommand, AgentRunCommand.run_id == AgentRun.id)
        .where(
            AgentRun.tenant_id == tenant_id,
            AgentRun.agent_id == agent_id,
            AgentRun.session_id == session.id,
            AgentRun.origin_user_id == current_user.id,
            AgentRun.source_type == "chat",
            AgentRun.run_kind == "foreground",
            AgentRun.runtime_type == "langgraph",
            AgentRun.runtime_thread_id == str(session.id),
            AgentRun.scheduling_lane_key == lane_key,
            AgentRunCommand.command_type == "start",
            AgentRunCommand.status == "pending",
        )
        .order_by(AgentRun.created_at, AgentRun.id)
        .limit(1)
    )
    pending_run = pending_result.scalars().first()
    if pending_run is None:
        return SessionRuntimeStateOut(active_run=None)
    run = pending_run
```

后续逻辑**无需改动**：`run_state_reader.get_run_state` 对无 checkpoint 的 pending run 已返回 `execution_status="queued"`（`run_state_reader.py:352` 的 `fallback = "running" if claimed else "queued"`）；`terminal = status in {completed,failed,cancelled}` 为 False → `can_cancel=not terminal and not cancel_inflight = True`；`can_resume` 仅对 `waiting_user` 为 True，排队时自然为 False。

## 6. 竞态与边界分析

1. **intake 拒绝 vs worker claim 的锁竞态**：`reject_unstarted_run_for_cancel` 对 start 用 `with_for_update`，`claim_next_command` 用 `with_for_update(skip_locked)`。谁先拿到 start 行锁谁赢：
   - intake 先：start 置 rejected → worker 再也 claim 不到（已 rejected，非 pending/claimed）→ run 不启动。✅
   - worker 先：start 置 claimed（lane_held=True）→ intake 的 `reject` 只匹配 pending/过期 claimed，跳过它 → 回到「运行中 cancel」协作式路径。✅
   二者都不会产生错态；实际 intake 在用户点击瞬间执行、worker 有 1–4s idle 退避，intake 几乎必赢。
2. **多条排队 run**：`order_by(created_at).limit(1)` 返回将被 claim 的最早一条；前端 stop 按钮带显式 `run_id`，只针对该 run。Part 2 的 `reject_unstarted_run_for_cancel` 按 `created_at < cancel.created_at` 拒绝**该 run 的所有更早 pending start**，与现有语义一致。
3. **幂等/重复点击**：第二次点击走 `existing is not None` 分支（`_cancel_runtime_run` 1047 前的 existing 查询 + `enqueue_cancel` 的 idempotency_key 去重），`enqueued.created=False` → Part 2 不重复拒绝。
4. **飞书/Group 入口**：`cancel_run` 为共用入口，Part 2 对其排队 start 同样生效；但 `reject_unstarted_run_for_cancel` 的 start 查询带 `command_type=="start"` 且按 run_id 隔离，不误伤其他 run。

## 7. 测试计划（TDD：先红后绿）

**Part 1**（`tests/test_websocket_runtime_chat.py`）：
- 新增：`test_cancel_accepts_queued_run_with_pending_start` —— `lane_held=False` + `start=pending` 时，`_cancel_runtime_run` 应 `cancel_run`（而非抛 `chat_cancel_not_lane_holder`）。
- 保留：`test_cancel_rejects_run_from_another_session`（scope 仍拒）、`test_duplicate_cancel_remains_idempotent_after_lane_release`（幂等仍过）。
- 新增：`test_cancel_rejects_terminal_run_without_active_start` —— `lane_held=False` + `start=applied`（无 pending/claimed）时仍抛 `chat_cancel_not_lane_holder`。

**Part 2**（`tests/test_agent_runtime_adapter.py` / `test_agent_runtime_persistence.py`）：
- 新增：`test_cancel_run_rejects_pending_start_synchronously` —— `enqueue_cancel` 后 `enqueued.created=True` 时，`reject_unstarted_run_for_cancel` 被调用、pending start 置 `rejected/cancelled_before_start`。
- 新增：`test_cancel_run_does_not_reject_running_start` —— start=claimed（未过期）时，`reject` 不误伤。
- 新增：`test_cancel_run_idempotent_retry_skips_reject` —— `enqueued.created=False` 时不重复调用。

**Part 3**（`tests/test_chat_session_runtime_state.py`）：
- 新增：`test_runtime_state_returns_queued_run_when_no_lane_holder` —— 无 holder + 有 pending start 时，返回 `active_run`（`status="queued"`、`can_cancel=True`、`can_resume=False`）。
- 保留：现有「无 holder 且无 pending → active_run=None」「多 holder → 409」「scope mismatch → 409」。

**端到端**（可选，复现手法）：
- 用现栈直连 /ws/chat 发消息后立即 abort，断言：`agent_run_commands` 里 `start→rejected/cancelled_before_start`、`cancel→applied`，且 `agent_tool_executions` 为空（run 从未启动）。

## 8. 部署与验证

- 全量测试 `cd backend && uv run pytest --ignore=tests/test_sso_toggle.py --ignore=agent_data`（基线 2510 passed）。
- `scripts/arch-guard.sh`。
- 现栈复现验证（Playwright）：发消息 → pending 窗口内 stop 按钮可见 → 点击 → run 不启动（无 tool execution）。
- 部署惯例：见 `clawith-workspace-facts` 记忆（worktree、ss-nodes.json symlink、回滚标签、`-p clawith-agent`、alembic 头核对）。

## 9. 代码位置索引

- `backend/app/api/websocket.py` `_cancel_runtime_run`(977，守卫 1047)
- `backend/app/services/agent_runtime/adapter.py` `cancel_run`(366)
- `backend/app/services/agent_runtime/persistence.py` `enqueue_cancel`(561)/`reject_unstarted_run_for_cancel`(863)
- `backend/app/services/agent_runtime/command_worker.py` `_reject_unstarted_start`(506)
- `backend/app/api/chat_sessions.py` `get_session_runtime_state`(402)
- `backend/app/services/agent_runtime/run_state_reader.py` `get_run_state`(260，"queued" fallback 352)
- 前端（无需改）：`sessionRuntimeState.ts`(38)/`AgentDetailPage.tsx`(7240)
