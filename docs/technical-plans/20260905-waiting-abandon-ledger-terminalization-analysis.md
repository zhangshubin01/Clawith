# waiting 遗弃路径 ledger 终态化——根因分析（poisoned-thread 方案3）

> 日期：2026-09-05 ｜ 状态：研究分析（方案3 待另开一轮实施）
> 触发：`waiting 遗弃路径 ledger 终态化` 待办；「unknown 落账在 resume/重试路径仍复现」新证据（CréditoMX 线程旧 run `c9231cdd` 重试 `delete_file` 得 `FileNotFoundError` 却落 `unknown`）

## 0. 结论先行

1. `agent_tool_executions.status="unknown"` 是**终态但必须人工对账**的状态，本设计上就「永远不再被运行时自动推进」。它有 **4 个生产源**，全部在 run 被遗弃（无人点 approve/reject）后永久残留，成为 compactor 硬屏障。
2. **resume/重试路径 unknown 复现的根因**在 `_mark_exception`：对 write/external_write 工具抛出的**未分类 Python 异常**一律判 `unknown`——`FileNotFoundError` 这类确定性、可证明无副作用的失败被误判为「不确定结局」。
3. **遗弃路径缺一个 run 边界终态化钩子**：run 达到终态（lane 释放）时，没有任何代码把该 run 名下残留的 `unknown`/`started` receipt 结算掉。租约对账只扫 `started`，不碰 `unknown`。

---

## 1. `unknown` 的 4 个生产源

### 源① `_mark_exception`（retry/resume 路径，delete_file 案根因）

`backend/app/services/agent_runtime/tool_step_service.py:1760-1792`

```python
known_failure = policy.side_effect_classification == "read" or isinstance(
    exc, (GroupRuntimeToolError, ToolExecutionError)
)
...
status="failed" if known_failure else "unknown",
error_code=(exc.code if isinstance(exc, (GroupRuntimeToolError, ToolExecutionError))
            else "tool_execution_exception"),
```

- `delete_file`（write 类）抛 `FileNotFoundError`（普通 Python 异常，非 typed）→ `known_failure=False` → `status="unknown"`。
- 调用链：`execute_pending` 主路径 `except Exception as exc`（`tool_step_service.py:2883`）→ `_mark_exception` → 上层 `if outcome.status == "unknown"`（`:2891`）→ `_waiting_request(error_code="tool_outcome_unknown")` 停 run 于 `waiting_user`。
- **问题**：`FileNotFoundError` 是确定性失败（文件不存在→删除是幂等 no-op），不存在「可能已部分写入」的不确定性，不该判 `unknown`。分类粒度太粗：把所有 write 工具的未分类异常一视同仁。

### 源② deadline/cancel（新 waiting 流）

`tool_step_service.py:1622`（cancel）、`:1646`（deadline），`_execute_application_with_controls`：

```python
status = "failed" if accepted.entry.effect == "read" else "unknown"
# error_code: tool_cancelled_outcome_unknown / tool_deadline_outcome_unknown
```

write 工具超 300s deadline 或被取消 → `unknown`（保守：可能有部分写入）。这是**新 waiting 流自身**的生产源——即便 7c98e9a9/f3fb7a4a 已把 deadline 提到 300s，超时后的 `unknown` 落账与旧流同构。

### 源③ 租约对账孤儿结算

`backend/app/services/agent_runtime/tool_lease_reconcile.py:213-221`：

```python
elif is_user_reconcilable_unknown_execution(execution):
    execution = await mark_tool_execution_unknown(..., result_summary=_ORPHAN_UNKNOWN_SUMMARY, ...)
```

孤儿 `started` receipt（进程死、租约过期）且属于 user-reconcilable 工具（write_file 条件写、图片生成、execute_code、registered dynamic MCP、带 workspace_candidate_ref）→ `unknown`。

### 源④ 重放/续跑既有 `unknown` receipt

`backend/app/services/agent_runtime/tool_execution.py:1320-1331` `_decision_for_existing`：

```python
if execution.status == "unknown":
    return ... blocked=True, reconciliation_required=True,
           requires_confirmation=effect != "read", error_code="tool_outcome_unknown"
```

resume 撞上一条已是 `unknown` 的历史 receipt → 不重跑、不终态化，直接停 `waiting_user(tool_outcome_unknown)`。

---

## 2. 遗弃后为何永久残留

`unknown` 的唯一出口是**人工对账**：

- `reconcile_unknown_tool_execution`（`tool_execution.py:2157`）只经 API `reconcile_direct_tool_execution` / `reconcile_group_tool_execution` 触发——即用户在 UI 点 approve/reject。
- run 被遗弃（用户直接发新任务、或等待永不被回复）时，没人点对账 → `unknown` 行永久残留。

**没有任何自动兜底**：

- `ToolLeaseReconcileScheduler.run_once` 查询条件 `status == "started"`（`tool_lease_reconcile.py:154`），只救「有租约的悬空 started」，**不碰 `unknown`**。
- run 终态的两个落点都不做 execution 清理：
  - `SchedulingLaneCompletionHandler.handle`（`scheduling_lane.py:65`）只 `lane_held = False`；
  - cancel 侧效（`checkpoint_side_effects.py:934`）只 `lane_held = False` + 事件记录。

---

## 3. 后果链（compactor 硬屏障）

`tool_exchange.py:372-388` `_resolve_incomplete_exchange`：

```python
unknown_with_side_effect = any(
    status == "unknown" and bool(ledger.get(call_id, {}).get("may_have_side_effect", True))
    for call_id, status in missing_statuses.items()
)
if unknown_with_side_effect:
    return ... action="require_confirmation", blocked=True, requires_confirmation=True
```

`may_have_side_effect` 来自 `ledger_from_executions`（`:901`）：`effect != "read"`，默认 True。

→ 缺失 ToolMessage + `unknown` 收据 = 硬屏障 → `run_compactor._compactable_prefix` 在首个屏障处停 → 尾部超 recent 预算 → `unsafe_exchange_exceeds_recent_budget` → run 提前 `failed`。

（方案1 已把中毒线程那两行手工改成 `failed` 解锁；但生产源未修，任何「write 工具超时/取消/抛未分类异常 + run 被遗弃」都会重新制造同样的永久屏障。）

---

## 4. 方案3 修复设计（待实施，未动代码）

### 修复点 A —— run 边界终态化（遗弃→failed+completed 标注）

在 run 达到终态时兜底结算名下残留 receipt。候选落点：`SchedulingLaneCompletionHandler.handle`（`scheduling_lane.py`，覆盖 completed/cancelled/failed 的 lane 释放）。

- 语义：run 终态后，该 run 的 `unknown`/`started` receipt 的续约/对账触发者（该 run 的 resume）已不存在，必须就地终态化。
- 动作：`status="unknown"` → `failed`，`result_summary` 标注 `abandoned: run ended before user reconciliation`；`status="started"` → 交由既有租约对账逻辑（或同样结算）。
- 注意区分「正常 completed 且 receipt 已 succeeded」与「遗弃且 receipt 残留 unknown」——只动后者；且须与租约对账的 `_owner_run_terminal` 判定共用同一「owner 终态」口径，避免竞态。

### 修复点 B —— `_mark_exception` 确定性失败分类（resume/重试路径 unknown 复现的直接根治）

把「可证明无副作用」的确定性异常从 write 工具 exception 中区分出来：

- 白名单 `DETERMINISTIC_NO_SIDE_EFFECT` 异常（`FileNotFoundError`、`IsADirectoryError`、`NotADirectoryError`、`FileExistsError` 等）→ `status="failed"`（known，`error_code="tool_execution_exception"`，`retryable=False`）。
- 更根本：推动工具层返回 typed `ToolExecutionOutcome`（现代 `delete_file` 已是 `_delete_file_outcome`，`agent_tools.py:4659`），裸异常只留给真正不确定的错误（网络中断、超时、进程崩溃）。旧 checkpoint 重放仍会撞裸异常路径，故 `_mark_exception` 的白名单是必要的兜底。

### 修复点 C —— 被遗弃 deferred 结果回写 channel

遗弃的 waiting 工具对应的 ToolMessage 结果从未回写（`execute_pending` 停 run 时 `messages` 不含该工具结果）。在 A 的终态化同时，按 ledger `result_summary` 合成 ToolMessage 回写 channel，消除 dangling tool_call，使该 exchange 在下次压缩时可被 `summarize`（`failed` 分支已支持，`tool_exchange.py:406`）。

### 验证

- 单测：`_mark_exception` 对 `FileNotFoundError` 产出 `failed`；run 终态时残留 `unknown` 被结算为 `failed` + abandoned 标注；`_resolve_incomplete_exchange` 对遗弃后合成结果走 `summarize` 而非 `require_confirmation`。
- 集成：复用中毒线程场景（write 工具超时/取消 + 用户发新任务遗弃）→ 下次压缩不再 `unsafe_exchange_exceeds_recent_budget`。
- 回归：`scripts/arch-guard.sh`；租约对账 / safe-read 重试 / async poll 三条既有终态路径不受影响（改动只加在 run 终态兜底与异常分类）。

---

## 5.5 参考项目对照（2026-09-05 源码级核查）

按 reference-check 纪律，把三个缺陷逐条对到参考仓库源码（deepagents / langgraph / OpenHands / codex / openai-agents-python / open-swe，均本地读源码取证）。

### 总结论

**没有任何参考项目有「副作用不确定」三态分类**——deepagents 的 `ToolMessage.status`、langgraph ToolNode、OpenHands Observation、codex、openai-agents 的工具结局都是**二值**（success/error 或「抛异常/回传结果」）。Clawith 的 `unknown`（「可能已副作用、需人工对账」）是**自研特有建模维度**，缺陷②（FileNotFoundError 误判 unknown）在参考里**无现成答案，必须自建确定性失败白名单**。

**但「悬空调用 / 遗弃执行」的清理是参考项目都做了、且有现成范式的**，正好是缺陷①③的解：

### 缺陷③ 悬空 tool_call → 压缩硬屏障

- **deepagents `PatchToolCallsMiddleware`（`middleware/patch_tool_calls.py:23-47`）是正解范式**：`before_agent` 扫出「有 tool_call_id 无对应 result」的悬空 call，**合成一条终态 ToolMessage**（`"was cancelled - another message came in before it could be completed"`），把「缺失结果」变成确定性终态，**而非硬屏障**。→ 对应方案3 修复点 C：遗弃的 waiting 工具按 ledger result_summary 合成 ToolMessage 回写 channel。
- **deepagents 压缩三不变式**（可搬）：①`summarization.py:1364-1484` 摘要**非破坏性**——只记 `_summarization_event` 不改 `state["messages"]`，请求侧重建；②`_message_eviction.py:105-116` 驱逐超大结果时**强制保留 `tool_call_id`/`name`/`status`**；③`_overflow_clip.py:131-173` 只裁末尾 ToolMessage 批、不碰 call。→ Clawith run_compactor 应加同款「只缩 result、不动 call 配对」不变量。

### 缺陷① 遗弃 run 的悬挂执行永不终态化

- **OpenHands `_emit_orphaned_action_errors()`（`software-agent-sdk/.../local_conversation.py:2598-2624, 671-713`）是正解范式**：`get_unmatched_actions()` 找「有 action 无 observation」的悬挂执行，**一次性回填合成错误事件**。→ 对应方案3 修复点 A 的「遗弃→终态化」清理。
- **OpenHands `reject_pending_actions()`（`local_conversation.py:2558-2596`）**：待人工确认被放弃时，状态复位 + **逐条回填 `UserRejectObservation`**（区分 `rejection_source="user"|"hook"`）。→ 对应方案3「reject→failed」语义。
- **codex 受控终止词表 `TurnAbortReason`（Interrupted/Replaced/ReviewEnded/BudgetLimited）+ 审批 `Aborted` 终态（`codex-rs/protocol/src/approvals.rs`、`tasks/mod.rs:534-535,980-985`）**：遗弃/顶替/预算用尽是**显式原因**而非笼统「不确定」；放弃 in-flight 审批=丢弃 pending + 模型只见 TurnAborted（不误判为被拒绝）。→ Clawith 的「遗弃」应显式区分原因，而非落 `unknown`。
- **open-swe `lost` 终态（`agent/tools/background_execute.py:180-194`）+ reconcile 兜底清扫（`agent/reconcile.py:37-119`）**：进程死但状态 running → 保守标 `lost`（终态，绝不重放），周期清扫兜底。→ 已对标 Clawith 租约对账 `tool_lease_reconcile.py`，但 open-swe 同样**不碰**「等人工对账」的收据——Clawith 要补的是 `unknown` 行的兜底。
- **langgraph 反向印证**：interrupt 无自动回收，悬挂 `INTERRUPT` 写永久留 `checkpoint_writes`，只有手动 `delete_thread/delete_for_runs/prune`；新 run 顶替旧 run 时旧中断是「绕过」非「清理」（`pregel/_loop.py:977-979,736-749`）。→ 框架本身不负责清理，Clawith 的遗弃终态化必须**自建**。

### 缺陷② write 异常误判 unknown（自建，参考无答案）

- langgraph ToolNode 默认**重抛**、`handle_tool_errors` 时只 `status="error"`，无「不确定」分支（`prebuilt/tool_node.py:383-391,1001-1012`）；`ToolMessage.status ∈ {success,error}`（`langchain_core/messages/tool.py:81`）。
- deepagents 把 `OSError`（含 FileNotFoundError）一揽子吞成错误串（`backends/filesystem.py:481-482,498-519`），无类型细分。
- **唯一近似物**：deepagents `backends/protocol.py:838-840` 的「跑了即 `status=success`、用 `artifact.exit_code` 承载失败」双字段分离「跑没跑 / 成没成」；codex 用自然语言 guidance（「aborted commands may have partially executed」）而非结构化三态。
- **结论**：Clawith 的「确定性 failed vs 不确定 unknown」二分需自建判定（异常类型白名单：`FileNotFoundError`/`IsADirectoryError`/`FileExistsError` 等 → failed；网络/超时/进程崩溃 → unknown）。

### 可借鉴点汇总（→ 方案3 实施）

| 参考范式 | 文件:行 | 映射到方案3 |
|---|---|---|
| deepagents `PatchToolCallsMiddleware` 合成终态 ToolMessage | `patch_tool_calls.py:23-47` | 修复点 C 回写 channel |
| deepagents eviction/clip 保留 call↔result 配对 | `_message_eviction.py:105-116`/`_overflow_clip.py:131-173` | compactor 不变量 |
| OpenHands `_emit_orphaned_action_errors` 回填合成终态 | `local_conversation.py:2598-2624` | 修复点 A 遗弃终态化 |
| OpenHands `reject_pending_actions` 逐条回填拒绝 | `local_conversation.py:2558-2596` | reject→failed 语义 |
| codex `TurnAbortReason` 受控终止词表 | `protocol.rs:4173-4178` | 遗弃原因显式化 |
| open-swe `lost` 终态 + reconcile 兜底 | `background_execute.py:180-194` | 兜底清扫 |
| langgraph 特殊控制写 + 负索引 | `_loop.py:818-846` | unknown=待对账写、reconcile=补写、消费即消 |

---

## 5. 范围与风险

- 改动点：`scheduling_lane.py`（或等价 run 终态钩子）、`tool_step_service.py::_mark_exception`、可能 `tool_execution.py`（abandoned 结算 helper）、channel 回写。
- 风险：run 终态兜底若误伤「run 已完成且 receipt 已 succeeded」→ 必须严格只动 `unknown`/`started` 且附 abandoned 标注；与租约对账竞态需用同一 `owner_terminal` 判定 + 行锁（`with_for_update`）。
- 与「feishu waiting 卡片观察」（`20260827-feishu-waiting-card-observation.md`）相互独立：那个是前端呈现缺失，这个是 ledger 终态化。
