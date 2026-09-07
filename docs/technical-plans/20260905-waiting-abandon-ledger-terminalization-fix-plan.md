# waiting 遗弃路径 ledger 终态化 — 生产级修复方案与多角度评审

> 日期 2026-09-05。前序分析见 `20260905-waiting-abandon-ledger-terminalization-analysis.md`（§1-§6）。
> 本文按 `production-fix-plan` skill 四件套交付：参考对比（≥10 项目）→ 双源根因 → 最小可回退方案 → 9 角度评审裁决。
> 所有代码级事实经 read_file 核对（行号以当前 `backend/app/services/agent_runtime/` 为准，并行会话会漂移，以**函数名**定位）。

---

## 0. 裁决与一句话结论

**评审裁决：有条件通过（回改后交付）**。三轮核对累计回改：

1. **（上轮）「ledger 终态化与 channel 回写必须成对」不成立**——`tool_exchange.py:406-424` 已有「`failed` → 合成缺失结果 → `summarize` → `retry_model`」分支，**ledger 终态化（unknown/started→failed）是解除 compactor 硬屏障的必要且充分条件**，channel 回写是可选增强。
2. **（本轮）三处实现级硬伤**（见 §3.5）：`run_is_terminal` 自开 session 不能直接换调；`mark_tool_execution_failed` 拒 terminal→terminal、`unknown→failed` 需新原语；reconcile settle 缺 `owner_terminal` 守卫。
3. **（再审）Z2 不能整体替换 lane 启发式**（见 §3.3）：lane 释放与终态事件发射不同事务，纯事件信号在「事件落库晚于 lane 释放」边界会漏结算 → 改 **OR 两个信号**（事件信号覆盖 `lane_key=NULL`，lane 信号保留原行为，零回归）。

根因判断**正确**；参考项目**准确**且扩到 13 条（含诚实负结论）。

---

## 1. 参考项目怎么解决（源码级核对，本机路径）

| 项目 | 机制 | 精确出处 | 解决的是哪一截 |
|---|---|---|---|
| **deepagents** | `PatchToolCallsMiddleware.before_agent` 扫「有 tool_call 无 result」的悬空 call，合成终态 ToolMessage（`"...was cancelled - another message came in before it could be completed"`） | `libs/deepagents/deepagents/middleware/patch_tool_calls.py:17-49`（全文已读） | 缺陷③（channel 回写） |
| **OpenHands** | `_emit_orphaned_action_errors()`：interrupt/取消落地时，对 `get_unmatched_actions()` 找出的悬挂执行逐条回填合成 `AgentErrorEvent` | `software-agent-sdk/openhands-sdk/openhands/sdk/conversation/impl/local_conversation.py:2598-2626`；`state.py:671-710` | 缺陷③（interrupt 回填终态） |
| **OpenHands** | `reject_pending_actions(reason)`：放弃待确认时状态复位 + 逐条回填 `UserRejectObservation` | `local_conversation.py:2558-2596` | reject→failed 语义 |
| **codex** | `TurnAbortReason` 受控终止词表 `{Interrupted, Replaced, ReviewEnded, BudgetLimited}` | `codex-rs/protocol/src/protocol.rs:4173-4178`；`core/src/tasks/mod.rs:277,509-595` | 遗弃原因显式化 |
| **open-swe** | 进程死但状态 running → 保守标 `lost`（终态，绝不重放）+ 周期 `reconcile_stale_runs` 兜底 | `agent/tools/background_execute.py:173-193`；`agent/reconcile.py:37-119` | 兜底清扫 |
| **orca** | `agent-session-lease-adjudication.ts`：单写者租约裁决。`isProvenDeadProbe`/`deathEvidenceFor`/`runtimeFence` 用**死亡证据**（exit-observed/pid-absent/identity-mismatch）判 owner 死活，**「expiry alone never grants a second owner」、fail-closed**；`settlementRetryRequired` latch——「provider 死留下未结算 terminal rows，此 latch 不是 owner 不确定，必须存活到 journal settlement durably accepted」 | `orca/src/shared/agent-session-lease-adjudication.ts:62-92,108-173,179-254`（已读） | 缺陷④（owner 终态判定弃 lane 启发式、用可靠死亡证据）+ terminalize-never-loop 语义 |
| **12-factor-agents** | factor-05「Unify execution state and business state」：业务状态（tool call/result 列表）即执行状态，别拆两套事实源 | `content/factor-05-unify-execution-state.md`（已读） | 设计原则：ledger 是「工具结局」唯一事实源，channel 只是镜像 |
| **12-factor-agents** | factor-06「Launch/Pause/Resume」+ 注「orchestrator 常不支持 tool selection 与 execution 之间的 pause/resume」 | `content/factor-06-launch-pause-resume.md:27`（已读） | 反向印证：waiting 边界是自建的，框架不负责清理 |
| **langgraph** | ToolNode 默认重抛、`handle_tool_errors` 只 `status="error"`；`ToolMessage.status ∈ {success,error}`，无「不确定」三态 | `prebuilt/tool_node.py`（前序已核） | 反向印证：Clawith「unknown」是自研特有，白名单需自建 |
| **openai-agents-python** | **负结论**：工具结局二值（ToolOutput/error），无三态、无 orphan/reconcile 终态化（grep 命中 tracing/memory/run_state 与工具结局无关） | `src/agents/run.py`/`function_schema.py`（grep 核对） | 印证：unknown 三态自建 |
| **gemini-cli** | **负结论**：无 orphan/abandon/pending-tool 终态化账本；MCP 取消即断流（grep 命中 mcp-client 与 ripgrep 二进制，非工具结局账本） | `packages/core/src/tools/mcp-client.ts`（grep 核对） | 印证：CLI 循环无持久 ledger |
| **gptme** | **负结论**：工具即函数调用，无持久 execution ledger、无 reconciliation（`interrupt` 命中仅为工具定义） | `gptme/tools/`（grep 核对） | 印证：无账本则无「账本终态化」问题 |
| **SWE-agent** | **负结论（不可读）**：本机未克隆（`~/Documents/UGit` 无），其任务队列/状态机后继 `open-swe` 已列作参考 | —（诚实标注未本地核对） | 印证：由 open-swe 代为参考 |

**参考正确性结论**：13 条（8 正 + 5 负/反向）全部经本机源码或 grep 核对。仅 OpenHands 本地 checkout 已迁到 `software-agent-sdk`（行号位移、代码一致）。

**关键范式**：没有任何参考项目有「副作用不确定」三态分类；但它们**一致地**把「悬空/遗弃执行」在**边界处一次性合成终态**，而非让它漂进压缩路径成硬屏障。orca 额外给出 Clawith 缺的**owner 死亡证据裁决**（缺陷④正解）与「未结算 terminal rows 必须存活到 durably accepted」（exactly-once 边界）。

---

## 2. 根因再确认（生产库实时取证，2026-09-05）

`agent_tool_executions` 全表 status 分布：`succeeded 16026 / failed 1985 / started 8 / unknown 5`。13 个非终态行即活证据。

**5 个 `unknown`（从未自动结算）：**

| tool | 落账时间 | error_code / 归类 | 对应缺陷 |
|---|---|---|---|
| `execute_code` | 2026-09-05 17:31（当天） | `tool_cancelled_outcome_unknown`，`cancelled_by_user` | 缺陷①（取消后可能已写→unknown→永等对账） |
| `delete_file` | 2026-08-27 | `tool_execution_exception`，`FileNotFoundError` | 缺陷②（确定性失败误判 unknown） |
| `execute_code` ×3 | 08-03/05/06 | `workspace_sync_conflict`（旧 bug 遗留） | 缺陷①（历史残留） |

**8 个 `started`（08-27~29 死 run 孤儿，reconcile 未结算）：** 1 个 `android_compile`（write/never，lease 已过期 7 天、owner 已终态、仍 started）；7 个读工具（`lane_key=NULL` run）。

**五个缺陷：**

- **缺陷①**：`unknown` 唯一出口是人工对账 `reconcile_unknown_tool_execution`，run 遗弃无人点→永久残留；run 终态钩子只释放 lane 不做 execution 清理；**reconcile settle 逻辑（`tool_lease_reconcile.py:213-221`）的 `is_user_reconcilable_unknown_execution` 分支未查 `owner_terminal`，owner 已死仍落 `unknown`**。
- **缺陷②**：`_mark_exception`（`tool_step_service.py:1760-1792`）`known_failure = side_effect_classification=="read" or isinstance(exc,(GroupRuntimeToolError,ToolExecutionError))`，write 工具抛普通异常（FileNotFoundError）→unknown。
- **缺陷③**：waiting 停 run 时 ToolMessage 结果不回写 channel→悬空 tool_call→compactor 硬屏障（`tool_exchange.py:372-399`）。
- **缺陷④**：`_owner_run_terminal`（`tool_lease_reconcile.py:86-109`）用 `bool(lane_key) and not lane_held`，`lane_key=NULL` 的直接对话 run 恒返回 False→死 run 读孤儿永不结算。
- **缺陷⑤**：`run_once` 只处理 `candidates[0]`（`tool_lease_reconcile.py:180`），`order_by lease_expires_at asc nulls_first`，NULL-lease 读孤儿永排队首且 owner 恒判非终态→每次对同一条 `enqueue_resume`（幂等命中 `created=False`→返回 idle，`:251-259`）→永远扫不到第 8 位 write 孤儿。

**后果链（未变）**：`_resolve_incomplete_exchange` 对 `missing + unknown + may_have_side_effect` → `require_confirmation`/`blocked`（372-388）；`any(status in {started,unknown})` → `block_reconcile`（389-399）；`_guard_observed_results` 对「已观察到 result 但 ledger=unknown」硬屏障（279-308）；`_safe_compact_block` 拒绝两类块（`run_compactor.py:300-309`）→ 尾随超 `recent` 预算 → `unsafe_exchange_exceeds_recent_budget`。

---

## 3. 生产级修复方案

### 3.1 核心设计：ledger 终态化是**必要且充分**条件

`_resolve_incomplete_exchange` 对「悬空 call（无 ToolMessage 结果）+ ledger 状态」的处理，经 read_file 核实：

- `missing + unknown` → `require_confirmation` 硬屏障（`:372-388`）；
- `missing + started/unknown` → `block_reconcile` 硬屏障（`:389-399`）；
- **`missing + failed` → `summarize` + `retry_model`（`:406-424`）**：`if any(status == "failed")` → `summary_for_exchange(reason="failed_result_missing")` → 合成缺失结果让模型重规划，**不是硬屏障**。

因此：**只要把收据从 `unknown`/`started` 落成 `failed`（带 `result_summary`），compactor 硬屏障即解除，且无需 channel 回写**——`failed` 的悬空 call 走 `:406-424` 软合成分支。

- 反向：**只回写 ToolMessage 而 ledger 仍 `unknown`**，会被 `_guard_observed_results` 的「observed unknown + side effect」硬屏障挡住（`:278-293`）。
- 结论：**ledger 终态化 = 必要且充分**；**channel 回写 = 可选增强**（只用于修 checkpoint messages 状态，供后续 resume/replay 干净续跑，非解屏障必需）。

### 3.2 修复点 X（最小单点）：`_mark_exception` 确定性失败白名单

`tool_step_service.py:1760-1792`（2 个调用点 `:2599`、`:2884`，都是「工具抛异常」路径，白名单对两者同样生效、符合意图）：

```python
_DETERMINISTIC_FAILURE = (
    FileNotFoundError, NotADirectoryError, FileExistsError,
    IsADirectoryError, PermissionError,
)
known_failure = (
    policy.side_effect_classification == "read"
    or isinstance(exc, (GroupRuntimeToolError, ToolExecutionError))
    or isinstance(exc, _DETERMINISTIC_FAILURE)
)
```

- 依据：删不存在的文件 = 幂等 no-op，这些异常是**确定性失败**（结局可知=「没生效」，不是「结局不确定」）。落 `failed` 后走**正常结果回写**：`except` 块 `_mark_exception` 返回非 unknown 后，`:3002` 的 `messages.append(_result_message(...))` 把 `failed` ToolMessage 立即写回 channel，模型当场重规划——**无悬空 call，也无需走 406-424 合成**（本轮 read_file 确认的端到端流）。406-424 合成只兜「run 被遗弃、结果从未回写」的场景（Z3c 覆盖）。
- 白名单外普通异常（网络/超时/进程崩溃/未知 bug）仍 `unknown`——保守语义不动。
- 直接命中：生产 `delete_file FileNotFoundError→unknown`。

### 3.3 修复点 Z（reconcile 三处 + 扫描扩展）：**P0 主修复**

**Z2. `_owner_run_terminal` 补可靠终态信号（缺陷④）**——注意**不能直接调 `run_is_terminal`，也不能整体替换 lane 启发式**：

- **不能直接调 `run_is_terminal`**：它签名 `(session_factory, handle)` 且**自开新 session**；而 `_owner_run_terminal` 在 `run_once` 的 `async with db.begin()` 事务内、`with_for_update(skip_locked)` 锁范围内被调，换调会破坏事务与锁范围。
- **不能整体替换 lane 启发式**（本轮评审新发现）：lane 释放与终态事件发射**不同事务**——`SchedulingLaneCompletionHandler.handle` 是独立 terminal_handler、自开事务（`scheduling_lane.py:47-48`）；终态事件在 `checkpoint_side_effects.py::_record_lifecycle_events`（`:920-942` cancel 路径与 lane 释放同事务、`:984-986` 正常路径先于 lane 释放）。虽正常时序事件先于 lane 释放，但**换纯事件信号在「事件落库晚于 lane 释放」的边界会漏结算**。正确做法是 **OR 两个信号**：事件信号覆盖 `lane_key=NULL` 直接对话 run，lane 信号保留原行为（零回归）。

```python
# event_stream.py（终态语义的单一 owner，符合 C3）
async def run_terminal_on_db(db, *, tenant_id, run_id) -> bool:
    row = (
        await db.execute(
            select(AgentRunEvent.event_type).where(
                AgentRunEvent.tenant_id == tenant_id,
                AgentRunEvent.run_id == run_id,
                AgentRunEvent.event_type.notin_(_DELIVERY_EVENT_TYPES),
            ).order_by(
                AgentRunEvent.created_at.desc(), AgentRunEvent.id.desc()
            ).limit(1)
        )
    ).one_or_none()
    return row is not None and row.event_type in _TERMINAL_EVENT_TYPES
```

```python
# tool_lease_reconcile.py::_owner_run_terminal —— OR 两个信号，非替换
async def _owner_run_terminal(db, *, tenant_id, run_id) -> bool:
    if await run_terminal_on_db(db, tenant_id=tenant_id, run_id=run_id):
        return True                       # 覆盖 lane_key=NULL 直接对话 run
    row = (await db.execute(
        select(AgentRun.lane_held, AgentRun.scheduling_lane_key).where(
            AgentRun.tenant_id == tenant_id, AgentRun.id == run_id,
        )
    )).first()
    if row is None:
        return True                       # Run 行已删，视为终态
    lane_held, lane_key = row
    return bool(lane_key) and not bool(lane_held)   # lane run 原行为
```

`lane_key=NULL` 死 run 从此可判终态，lane run 行为零回归。

**Z3a. 消除扫描饥饿 + 批量结算（缺陷⑤）**：`run_once` 不再只取 `candidates[0]` 后对「已 resume 未结算」收据 `return idle`（`:251-259`）打转，改为 `for execution in candidates` 批量结算（保留 `with_for_update(skip_locked)` + 每行 `execution.tenant_id` 作用域，多租户隔离不变）。注：缺陷⑤的根其实是缺陷④——队首 NULL-lease 读孤儿 owner 误判非终态→永不结算→卡死全队；修④后自然前进，批量只是加速 + 防未来新卡点。

**Z3b. settle 逻辑补 `owner_terminal` 守卫（缺陷① 的 reconcile 源）**：`tool_lease_reconcile.py:213-221` 的 `elif is_user_reconcilable_unknown_execution(execution):` **未查 `owner_terminal`**——owner 已死时仍落 `unknown`（永无人对账）。改为：

```python
elif is_user_reconcilable_unknown_execution(execution) and not owner_terminal:
    execution = await mark_tool_execution_unknown(...)   # owner 活跃，人工对账还活着
else:
    execution = await mark_tool_execution_failed(...)    # owner 已死 → 直接 failed
```

**Z3c. 扫描含 `unknown` + 降级（回收 5 条历史 unknown）**：扫描条件 `status == "started"` → **`status in {"started","unknown"}`**。`unknown` 收据**无活跃租约**，不走 `takeover_tool_execution_for_reconciliation`；owner 终态时用新原语 `mark_tool_execution_abandoned`（§3.5）降级 `unknown→failed`。降级后仍走 `enqueue_thread_holder_reconcile_wake`（唤醒同 thread 上可能等待此结算的新 run），但**不** `enqueue_resume`（owner 已死）。⚠️ 实施时核验：`runtime_async_pending` 过滤会把声明式异步工具的 `unknown` 收据留给 `AsyncToolPollScheduler`（正确行为），故「5 条 unknown」未必全部由本扫描回收——部分可能归 AsyncToolPollScheduler/ProductReconciler 管。

**安全边界（红线，不变）**：只动「owner 已终态 + 收据非 `succeeded`」；`succeeded` 绝不碰；owner 仍活跃时 `is_user_reconcilable_unknown_execution` 保持 `unknown`。终态化只改 ledger status，**不重跑工具**，绝不触发重复外部写。

### 3.4 修复点 Y + Z1（可选增强，P1）：channel 回写 + run 终态钩子

- **Y（channel 回写）**：借 deepagents `PatchToolCallsMiddleware` + OpenHands `_emit_orphaned_action_errors` 范式，对「有 call 无 result」且收据已 `failed` 的调用，用 `_result_message()`（`tool_step_service.py:457`）合成 `status="failed"` 的 ToolMessage 追加写回 checkpoint messages。目的仅修 checkpoint 状态一致性，**非解屏障必需**。
- **Z1（run 终态兜底钩子）**：`SchedulingLaneCompletionHandler.handle` 追加「owner 终态 + 名下残留 unknown/started → failed」——与 Z3 是**同一动作两个触发点**，二者取一即可。推荐 **Z3 周期扫描为主**（覆盖面广、幂等、不改 checkpoint 时序），Z1 只作「更快回收」增强。
- **落地顺序**：先 **X + Z2 + Z3a/b/c**（纯「把该 failed 的改成 failed」，不动 channel、不动屏障判定，单点可回滚）；**Y+Z1 单独评估**（见 Q7）。

### 3.5 新原语 `mark_tool_execution_abandoned`（unknown→failed 降级）

**本轮发现的三处实现级硬伤**（read_file 核对）：

1. **`run_is_terminal` 自开 session** → Z2 不能直接换调（§3.3 已给 db-session helper 方案）。
2. **`mark_tool_execution_failed` 拒 terminal→terminal**：`_mark_terminal` 对 `execution.status in {"succeeded","failed","unknown"}` raise `tool_execution_terminal_conflict`，且 `_require_lease_owner` 只认 `status=="started"`。故 `unknown→failed`（已终态、无租约）需**专用受控原语**：

```python
# tool_execution.py
_ORPHAN_ABANDONED_SUMMARY = (
    "The owning Run ended before this uncertain outcome could be reconciled. "
    "Treat it as not applied; verify the current state before repeating."
)

async def mark_tool_execution_abandoned(
    db, *, tenant_id, execution_id,
    result_summary: str, error_code: str = "tool_outcome_abandoned",
    metadata: dict | None = None, clock: Callable[[], datetime] | None = None,
) -> AgentToolExecution:
    execution = await _get_locked_execution(
        db, tenant_id=tenant_id, execution_id=execution_id
    )
    if execution.status != "unknown":
        raise ToolExecutionError(
            "tool_execution_terminal_conflict",
            "only an unknown receipt can be marked abandoned",
        )
    # 复用 _normalize_text / _bounded_result_metadata / _supersede_stale_resume_commands
    execution.status = "failed"
    execution.result_summary = result_summary
    execution.result_metadata = _bounded_result_metadata({
        **(metadata or {}),
        "error_code": error_code,
        "retryable": False,
        "abandoned": True,
    })
    execution.lease_expires_at = None
    execution.completed_at = (clock or (lambda: datetime.now(UTC)))()
    await db.flush()
    await _supersede_stale_resume_commands(
        db, tenant_id=execution.tenant_id, run_id=execution.run_id,
        tool_call_id=execution.tool_call_id,
    )
    return execution
```

复用 `_get_locked_execution` / `_normalize_text` / `_bounded_result_metadata` / `_supersede_stale_resume_commands`，**不新造轮子**。摘要常量 `_ORPHAN_ABANDONED_SUMMARY` 语义等价 `_ORPHAN_FAILED_SUMMARY` 但点明「run 遗弃」来源。

3. **reconcile settle 缺 `owner_terminal` 守卫** → 缺陷① 的 reconcile 源（§3.3 Z3b）。

---

## 4. 多角度评审（9 问，逐条裁决）

### 4.1 根因找的是否正确？ —— **通过**（源码 + PG 台账双源）
5 unknown + 8 started 硬证据；当天 `cancelled_by_user→unknown` 实时复现；`delete_file FileNotFoundError→unknown` 命中缺陷②。根因链解释双源全部关键证据（Langfuse `cancelled_by_user` 22 条是最大错误源、正是喂 unknown 的路径；`unsafe_exchange_exceeds_recent_budget` 方案1后未复发但收据仍累积）。已追到最深层：不是「reconcile 没扫到」，而是「④ lane 启发式误判 + ⑤ 饥饿 + ① settle 缺 owner_terminal 守卫」让 reconcile 结构性失效。

### 4.2 根治方案是否正确？ —— **通过（回改后）**
原「成对」论断已撤回。现方案改的是根因本身：把「不确定/悬空收据」在 run 边界落成「可摘要终态 `failed`」。验证：删掉方案，「收据终态化」不再发生 → 根因再发作。白名单（X）是「确定性 vs 不确定」边界，参考无现成答案，自建正确。Z3b（settle 补守卫）+ Z3c（扫描降级）+ 新原语覆盖缺陷①的全部 4 个 unknown 源。

### 4.3 参考的资料是否正确？ —— **通过**
13 条全部源码/grep 级核对，非 README 摘要；负结论诚实标注；无归档/停更项目被当第一依据。

### 4.4 会引起其他问题吗？ —— **通过（带 3 项缓解）**（爆炸半径经消费者枚举）
改动点消费者枚举（grep 实测）：`_mark_exception` 仅 2 调用点（同文件，均属预期）；`_owner_run_terminal` 仅 1 调用点（reconcile 自身）；`run_terminal_on_db` 为新增（终态语义 single-owner 在 event_stream）；`mark_tool_execution_abandoned` 为新增原语（只被 Z3c 调用）；`mark_tool_execution_failed` 4 调用点（reconcile/a2a_runtime/tool_step_service/product_reconciler，不动）。
1. **误终态化仍在执行的收据**：Z3 严格「owner 已终态」+ `with_for_update(skip_locked)` + 行锁，避免与活跃执行竞态。
2. **把真不确定的写过早降级**：owner 活跃时 user-reconcilable 收据保持 `unknown`（Z3b 守卫），仅 owner 终态才降级。
3. **重复外部写**：终态化只改 ledger status，不重跑工具；`failed` 摘要带 "verify before repeating"，工具层 exactly-once 策略不变。

### 4.5 会把其他逻辑搞坏吗？ —— **通过**
`_resolve_incomplete_exchange` / `_guard_observed_results` / `_safe_compact_block` **不动**——它们对「收据已是 failed」本就正确（`failed` 是 `valid_terminal_statuses`、走 406-424 软合成）。新原语 `mark_tool_execution_abandoned` 只新增、不改既有 `_mark_terminal` 行为。后端改动前跑 `scripts/arch-guard.sh` + reconcile 既有终态路径回归。

### 4.6 这是根治的最佳方案？ —— **通过（枚举 3 候选，从 Ponytail 最低档起挑）**
| 候选 | 内容 | 代价/收益 | 裁决 |
|---|---|---|---|
| **A（更简单，最低档）** | X + Z2 + Z3a/b/c：ledger-only 终态化，不动 channel、不动屏障 | 最少 diff；解屏障充分（406-424）；修 13 行 + 防 4 源 | **选 A 为 P0** |
| **B（中间）** | A + Y：加 channel 回写 | 多一个 helper + checkpoint 写；只修 resume/replay 一致性，非解屏障必需 | P1 可选 |
| **C（更彻底）** | A + Y + Z1：加 run 终态钩子即时回收 | 覆盖更全但改 checkpoint 时序、与 Z3 重复 | Z1 与 Z3 取一，暂不引入 |

从 Ponytail 最低档（A）起挑，**A 已满足根治**；B/C 是增强，留待「有 resume/replay 悬空症状」证据再上。

### 4.7 修复方案是否多余？ —— **通过（Y+Z1 降级为可选）**
X/Z2/Z3 不多余（生产各有实锤）。**Y + Z1 对「解 compactor 硬屏障」这一 P0 目标是多余的**——406-424 已兜底。诚实定性：Y+Z1 是 **P1 预防/一致性加固**，按宪法 II 不混入 P0，单独开、有证据再上。

### 4.8 是否已有可复用逻辑？ —— **通过（大量现成；1 处需新原语）**
`run_is_terminal`/`_latest_lifecycle_row`/`_TERMINAL_EVENT_TYPES`（`event_stream.py`）、`mark_tool_execution_failed`/`_mark_terminal`/`takeover_tool_execution_for_reconciliation`（`tool_execution.py`）、`_ORPHAN_FAILED_SUMMARY`/`_ORPHAN_READ_FAILED_SUMMARY`/`_ORPHAN_UNKNOWN_SUMMARY`（`tool_lease_reconcile.py:56-70`）、`_result_message()`（`tool_step_service.py:457`）、`summary_for_exchange(reason="failed_result_missing")`（`tool_exchange.py:188-235,406-424`）、`enqueue_thread_holder_reconcile_wake`、「terminalize never loop」（`command_worker.py:1162/1221`）、orca 的 `settlementRetryRequired` 语义。
**唯一不能直接复用**的是 `unknown→failed` 降级（`_mark_terminal` 拒 terminal→terminal），需新增 `mark_tool_execution_abandoned`（内部复用 `_get_locked_execution`/`_normalize_text`/`_bounded_result_metadata`/`_supersede_stale_resume_commands`）。

### 4.9 会破坏 Clawith 特性吗？ —— **通过（逐条过宪法 + 红线）**
**宪法（`.specify/memory/constitution.md` v1.0.0，实际列 5 原则 + Project Constraints，无 C6——skill 模板的 C6 不适用，如实标注）**：
- **C1 Evidence Before Claims**：✓ 全部函数名/行号 read_file 核对；每个「已有 X」标注出处。
- **C2 Minimal Scoped Changes**：✓ 候选 A 最小 diff，Y/Z1 剥离。
- **C3 Contract and State Ownership**：✓ ledger 是「工具结局」唯一 owner（对齐 12-factor factor-05）；终态语义 single-owner 在 event_stream（`run_terminal_on_db`）。
- **C4 Tests Prove Behavior**：✓ 见 §5 测试。
- **C5 Preserve Existing Work**：✓ 改动集中 3-4 文件；不动 compactor/屏障；不 revert dirty 工作树。
- **Project Constraints**：✓ 无新依赖；不改公共工具行为；exactly-once 不变。

**工作区红线（`clawith-workspace-facts`）**：
- **durable run/checkpoint**：✓ 只改 ledger，不动 checkpoint 写（P0）。
- **多租户隔离**：✓ 每行按 `execution.tenant_id` 结算，作用域不变。
- **exactly-once**：✓ 终态化不重跑工具；`settlementRetryRequired` 语义保证未结算行重启后继续结算而非丢弃。
- **前缀缓存稳定性**：✓ P0 不动 messages。
- **WS 状态机**：✓ 终态判定复用 event_stream 语义，不改 WS 行为。
- **飞书通道**：✓ waiting 卡片观察与 ledger 独立；owner 仍 waiting（非终态）的行绝不动。

---

## 5. 落地清单（供下轮实施）

| # | 改动 | 文件:函数 | 复用 | 优先级 |
|---|---|---|---|---|
| X | 确定性失败白名单 | `tool_step_service.py::_mark_exception` | — | P0 |
| Z2 | 新增 db-session 终态 helper + `_owner_run_terminal` OR 两信号（非替换） | `event_stream.py::run_terminal_on_db` + `tool_lease_reconcile.py::_owner_run_terminal` | `_latest_lifecycle_row`、`_TERMINAL_EVENT_TYPES`、原 lane 查询 | P0 |
| Z3a | 批量结算、消除饥饿 | `tool_lease_reconcile.py::run_once` | — | P0 |
| Z3b | settle 补 `owner_terminal` 守卫 | `tool_lease_reconcile.py::run_once`（`:213-221` 分支） | — | P0 |
| Z3c | 扫描含 `unknown` + 降级 | `tool_lease_reconcile.py::run_once`（`:154` 条件 + 新分支） | `mark_tool_execution_abandoned` | P0 |
| 新原语 | `mark_tool_execution_abandoned` | `tool_execution.py` | `_get_locked_execution`/`_normalize_text`/`_bounded_result_metadata`/`_supersede_stale_resume_commands` | P0 |
| Y | channel 回写（可选增强） | 回写 helper | `_result_message`、`_ORPHAN_*_SUMMARY` | P1（有证据再上） |
| Z1 | run 终态钩子（与 Z3 二选一） | `scheduling_lane.py::SchedulingLaneCompletionHandler` | 同上 | 暂不引入 |

**测试（C4）**：
1. `_mark_exception` 对 `FileNotFoundError` 产出 `failed`（白名单）；对 `TimeoutError`/`OSError` 仍 `unknown`（负例）。
2. `run_terminal_on_db` 对 `lane_key=NULL` 但已有 `run_completed` 事件的死 run 返回 True（缺陷④）。
3. reconcile 对 `nulls_first` NULL-lease 读孤儿：owner 终态 → 结算 `failed`，不再 `return idle` 打转（缺陷⑤）；批量结算一轮清多行。
4. settle 对 `is_user_reconcilable_unknown_execution` + `owner_terminal` → `failed`（Z3b），`owner 活跃` → `unknown`（负例）。
5. `mark_tool_execution_abandoned` 只接受 `unknown`→`failed`；对 `succeeded`/`started`/`failed` 抛 `tool_execution_terminal_conflict`。
6. 集成：终态化后 `_resolve_incomplete_exchange` 走 `summarize`（非 `require_confirmation`）；断言不重跑工具、无重复外部写。

- 后端改动前跑 `scripts/arch-guard.sh`；宪法 C1-C5 + Project Constraints 为准。
- 回滚：X / Z2 / Z3a/b/c / 新原语各自独立可回滚（新原语无既有消费者）。
