# P2-1：a2a 路径（send_message_to_agent）补 observe_tool span —— 深度分析与修复方案

日期：2026-08-28。前提：c8fa8595 已上线（group/scoped 两条路径 span 补盲）。本文分析剩余盲区中结构价值最大的 a2a 路径，产出修复方案供评审。

## 1. 深度分析

### 1.1 调用点解剖（tool_step_service.py）

`execute_pending` 的逐 tool-call 循环中，`send_message_to_agent` 有**两个**分支：

| 位置 | 分支 | 语义 | 是否执行 |
|---|---|---|---|
| 2311-2324 | `reservation.reusable` 存在时 | 从既有收据重建 waiting_request，**不重新执行** | 否（replay） |
| 2549-2621 | `tool_name == "send_message_to_agent" and self._a2a_service` | 真正调用 `RuntimeA2AService.execute` | **是** |

本次只包 2549 分支。2311 是收据重放、无执行发生——observe_tool 的 docstring 语义是「one tool handler execution 的 process-view（latency/errors + 台账对齐键）」，重放路径不执行，不包 span（与 P2-2 fence takeover 的 terminal_outcome 重放同理，保持口径一致）。

### 1.2 execute 契约事实（a2a_runtime.py:774-1022）

1. **自结算**：execute 在事务内自己写台账（`mark_tool_execution_succeeded` / `_mark_rejected`→`mark_tool_execution_failed`），调用方不再走 `_settle_outcome`。
2. **业务异常内部消化**：`A2ARuntimeError`（入参非法/循环守卫/目标不可用等）与 `AgentCycleGuardError` 在 execute 内部被转为 `A2ARuntimeToolResult(outcome.status="failed", result_summary="[A2A:{code}] ...")` 返回——**不抛给调用方**。因此调用方 `except Exception` 只兜底灾难性异常（DB 故障等）。
3. **返回形状**（`A2ARuntimeToolResult`，frozen dataclass）：`outcome: ToolExecutionOutcome`、`target_run_id: uuid.UUID | None`、`waiting_request: dict | None`。
   - 成功：`outcome.status="succeeded"`、`result_ref="agent-run:<run_id>"`（native）或 `"gateway-message:<id>"`（openclaw）、`target_run_id` 非空（native；openclaw 为 None）。
   - `waiting_request` 仅 consult/task_delegate 且 result_ref 非空时非空 → 源 run 中断等待目标 run 完成（waiting 协议）。
   - 拒绝：`outcome.status="failed"`、`result_ref=None`、`waiting_request=None`。
4. **目标侧关联键齐备**：native 目标 run 带 `parent_run_id=source_run.id`、`source_tool_execution_id`、`source_call_instance_id`（payload 里），且目标 run 自身会生成独立 trace。源侧 span 补上后，链路闭合：源 trace 的 tool span ⇄ 台账 ⇄ 目标 trace 的 run span。

### 1.3 观测语义对齐（tracing.py）

- `observe_tool(tool_name, tool_call_id, tool_execution_id, **identity)` → `GenerationHandle | None`；handle 有 `set_output` / `add_metadata` / `mark_error(exc)` / `mark_retry(exc)`。
- `_observe_span` 只对**逃出 with 块**的异常自动标记：retry 控制流 → `mark_retry`（DEFAULT level + retry_pending），其他 → `mark_error` + re-raise。
- **a2a 分支把所有异常消化在 try 内部**（`except Exception` → `_mark_exception`），异常不逃出 → 必须**显式** `tool_handle.mark_error(exc)`，否则失败 span 会伪装成功。这是本方案与 group/scoped 分支（异常逃出自动标记）的关键差异。
- `add_metadata(**values)` 自动过滤 None 值（tracing.py:296），`target_run_id=None` 时安全。

### 1.4 边界情形清单

| # | 情形 | 控制流 | span 语义 |
|---|---|---|---|
| A | 成功 + waiting（consult/task_delegate） | `_result_message` → waiting 早退 return | output=succeeded + metadata{target_run_id, waiting=True, a2a_mode} |
| B | 成功 notify（waiting_request=None） | `_result_message` → continue | 同上，waiting=False |
| C | 业务拒绝（execute 内部消化，outcome=failed） | `_result_message(failed)` → continue | output=failed、level 保持 DEFAULT（与主线「失败但已结算」口径一致，不标 ERROR） |
| D | 意外异常 → `_mark_exception` → unknown | 非 group：return ToolStepResult(waiting, error_code=tool_outcome_unknown) | output=unknown + **显式 mark_error(exc)**（level=ERROR） |
| E | a2a_result 为 None（execute 恒返非 None，死路径） | 落到心跳检查→主线执行 | span 无输出关闭；随后主线开第二个 span（双重记录，可接受，见 §4.2） |
| F | 2311 replay 分支 | 重建 waiting 不执行 | **不包**（见 §1.1） |
| G | `_mark_exception` 内 `_settle_outcome` 抛 `GroupWorkspaceReconciliationPending`（租约丢失） | 逃出 with | `_observe_span` 自动 `mark_retry` + 延后 re-raise（在 `_RETRY_CONTROL_FLOW_NAMES` 中）——与主线租约丢失语义一致，非回归 |

### 1.5 policy 取值

`send_message_to_agent` → `_policy_for_name` 默认支 → `effect="external_write"`, `retry_policy="never"`（builtin_tool_definitions.py:4029-4036）。测试断言以此为准。

## 2. 修复方案

### 2.1 代码改动（tool_step_service.py 2549-2621，仅此一处）

把 `try/except/else` 包进 `with observe_tool(...)`：

```python
if tool_name == "send_message_to_agent" and self._a2a_service:
    # A2A executor path — previously a tool-span blind spot. Wrapped
    # identically to the group/scoped branches; unlike those, the a2a
    # branch swallows its business exceptions inside `execute` and its
    # `except` handler below, so failures are marked explicitly on the
    # span (mark_error) instead of relying on exception escape.
    with observe_tool(
        tool_name=tool_name,
        tool_call_id=call_id,
        tool_execution_id=reservation.execution.id,
        side_effect_classification=policy.side_effect_classification,
        retry_policy=policy.retry_policy,
    ) as tool_handle:
        try:
            actor_user_id = uuid.UUID(context.actor_user_id) if context.actor_user_id else None
            a2a_result = await self._a2a_service.execute(
                tenant_id=tenant_id,
                source_run_id=run_id,
                source_agent_id=agent.id,
                tool_call_id=call_id,
                arguments=arguments,
                reservation=reservation,
                lease_owner=lease_owner,
                actor_user_id=actor_user_id,
            )
        except Exception as exc:
            outcome = await self._mark_exception(
                tenant_id=tenant_id,
                reservation=reservation,
                lease_owner=lease_owner,
                policy=policy,
                exc=exc,
            )
            if tool_handle is not None:
                tool_handle.set_output(_tool_outcome_summary(outcome))
                tool_handle.mark_error(exc)
            if outcome.status == "unknown":
                ...（原样保留两条 return）
        else:
            if a2a_result is not None:
                if tool_handle is not None:
                    tool_handle.set_output(_tool_outcome_summary(a2a_result.outcome))
                    tool_handle.add_metadata(
                        a2a_mode=arguments.get("msg_type"),
                        target_run_id=a2a_result.target_run_id,
                        waiting=(a2a_result.waiting_request is not None),
                    )
                ...（原样保留消息/waiting/continue）
```

要点：
- **输出复用 `_tool_outcome_summary(a2a_result.outcome)`**——`a2a_result` 本身不是 ToolExecutionOutcome，直接 str 会出 dataclass repr 噪音；outcome 字段才是与台账一致的结果摘要（c8fa8595 已提取的 helper 直接复用）。
- `a2a_mode` 从 `arguments.get("msg_type")` 取，不 import `_request`（避免重复解析）；`target_run_id`/`waiting` 是跨链路分析的关键对齐键。
- `set_output` 与 `mark_error` 可以共存（finalize 同时写 output 与 level/status_message）——比主线异常 span（无 output）信息更全，属有意增强。
- 其余分支（heartbeat 检查 2623 起、租约续期 2656 起、group/scoped/主线 with）**零改动**。

### 2.2 测试（tests/test_agent_runtime_tool_step_service.py，复用已部署的 _FakeTraceClient/_A2AService 模式）

1. `test_runtime_a2a_execution_emits_observability_tool_span`：仿 5088 用例装配（`_a2a_call(mode="task_delegate")` + `A2ARuntimeToolResult(succeeded, result_ref=f"agent-run:{target_run_id}", waiting_request={...})` + `monkeypatch tracing._get_client`）。断言：
   - `[s["as_type"] for s in fake.starts] == ["tool"]`、`name == "tool:send_message_to_agent"`
   - metadata：`tool_call_id`、`tool_execution_id == str(execution.id)`、`side_effect_classification == "external_write"`、`retry_policy == "never"`、`target_run_id == str(target_run_id)`、`waiting is True`、`a2a_mode == "task_delegate"`
   - `update["output"] == {"status": "succeeded", "result_summary": "delegation accepted", "error_code": None}`
   - 无 `level` 键（成功不标 ERROR）
2. `test_runtime_a2a_rejection_records_failed_output_without_error_level`：`A2ARuntimeToolResult(outcome=ToolExecutionOutcome(status="failed", result_summary="[A2A:a2a_input_missing] ..."), target_run_id=None, waiting_request=None)`。断言 output.status=="failed"、updates 无 `level` 键、`result.messages[0]["execution_status"]=="failed"`、`result.pending_tool_calls == ()`。
3. `test_runtime_a2a_exception_marks_span_error`：raising a2a stub（`raise RuntimeError("a2a exploded")`）+ `monkeypatch RuntimeToolStepService._mark_exception` 返回 unknown outcome。断言：某 update 含 `level == "ERROR"`、`status_message` 含 `RuntimeError`、output.status=="unknown"、`result.waiting_request["error_code"] == "tool_outcome_unknown"`。

### 2.3 验证清单（与 c8fa8595 同纪律）

1. 新 3 用例 + 既有 a2a 用例（5088/5148 等）+ 全量 `tests/test_agent_runtime_tool_step_service.py`。
2. `ruff check` 两文件；`ruff format --diff` 零新增命中。
3. `scripts/arch-guard.sh` P0 干净。
4. 提交（单一 pathspec）→ deploy.sh → 容器内 grep 特征（`with observe_tool(` 4 处 + a2a 分支 mark_error）→ 等真实 `send_message_to_agent` 调用后在 Langfuse 按 `tool:send_message_to_agent` 验证。

## 3. 评审（逐条核对）

| # | 决策 | 依据（代码事实） | 结论 |
|---|---|---|---|
| 1 | 只包 2549 不包 2311 | 2311 是 receipt replay 不执行；observe_tool 语义=execution process-view（tracing.py:465-471） | 成立 |
| 2 | 包在调用点而非 execute 内部 | group/scoped 先例同层包（c8fa8595）；`_run_identity` contextvar 在 run 执行流内传播，调用点包与主线一致 | 成立 |
| 3 | 异常分支显式 mark_error | execute 内部消化业务异常（a2a_runtime.py:1009-1022），调用方 except 只兜底灾难异常；异常不逃出 with → `_observe_span` 不会自动标记（tracing.py:442-450） | 成立，且是本方案唯一与 group/scoped 不同的机制点 |
| 4 | 拒绝（failed outcome）不标 ERROR | 主线口径：结算为 failed 的 outcome 走 set_output、level 保持 DEFAULT（c8fa8595 主线收敛处） | 口径一致 |
| 5 | 输出用 `_tool_outcome_summary(a2a_result.outcome)` | `_tool_outcome_summary` 只识别 ToolExecutionOutcome；A2ARuntimeToolResult 是外层 dataclass（a2a_runtime.py:55-61） | 成立；str(dataclass) 会漏 status/error_code 结构 |
| 6 | metadata 取 `arguments.get("msg_type")` 而非复用 `_request` | `_request` 是 a2a_runtime 私有函数；arguments 已解析过，原样取值零成本 | 成立 |
| 7 | waiting/notify 都记同一 span 形状 | waiting 早退与 continue 均在 with 内，span 在块尾关闭（CM 语义） | 成立 |
| 8 | 租约丢失（G 情形）不需额外处理 | `_RETRY_CONTROL_FLOW_NAMES` 含 `GroupWorkspaceReconciliationPending`，`_observe_span` 自动 mark_retry+延后 re-raise（tracing.py:383-396 同机制） | 成立，非回归 |

### 4. 残留风险与取舍

1. **E 情形双重 span**（a2a_result None / 非 unknown 异常 fall-through 后主线再开 span）：均为实践死路径（execute 恒返非 None；`_mark_exception` 对 external_write 恒 unknown→return）。接受：宁可有 process-view 双记录，不可有盲区；若未来复活该 fall-through 再收敛。
2. **mark_error 后 set_output 与主线形状差异**：主线异常 span 无 output（异常逃出未及 set_output），a2a 有 output。属信息增强，消费方（Langfuse UI/查询）兼容。
3. **openclaw 目标**：`target_run_id=None`（gateway 路径），metadata 里 waiting 仍能区分；`result_ref="gateway-message:<id>"` 落在 output.result_summary 之外（error_code=None 且 summary 为 _accepted_summary）——如需 gateway 对齐键，后续可把 result_ref 加入 output 摘要。**本轮不做**（保持与 ledger result_summary 一致）。

## 5. 实施顺序

批次 A（低风险，同文件）：本方案 P2-1 + P2-4 heartbeat-blocked 顺手包（可选）。
