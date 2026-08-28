# 07: 运行时强制记忆固化 hook（Memory Consolidation Gate）

**What to build:** run 成功收尾路径的运行时门禁（设计定稿见 ADR-0005，评审结论 Q1–Q7）：

1. **检测累计**：`node_executor.py::_tool` 中 `execute_pending` 成功后（`result.error is None`），
   解析 `current_call`（`function.name` ∈ {write_file, edit_file}、`function.arguments.path`，
   workspace 相对路径），累计进 `lifecycle["memory_gate_track"]`：
   `{"workspace_writes": n, "memory_writes": n}`（path 以 `memory/` 开头计 memory 写）。
2. **收尾门禁**：`node_executor.py::_model` 的 `intent == "finish"` 分支（设置 `verifying` 前）：
   `workspace_writes > 0` 且 `memory_writes == 0` 且未置 `forced_memory_consolidation` 时——
   - 剩余步数足够（`step_count + 1 <= model_turn_limit`）：追加 `role=user`、
     `runtime_intent="memory_consolidation"` 控制消息（复用 repair 消息模式，条件义务措辞：
     有耐用跨会话信息且未记录则先 read 再原位合并写入 `memory/memory.md` 并同步
     `memory/MEMORY_INDEX.md`，判定无则跳过直接交卷），置
     `forced_memory_consolidation=True`，`status="running"`、`next_route="model"`、
     `pending_tool_calls=[]`，`_schedule_compact(lifecycle)`；
   - 步数不足：放行 finish，置 `memory_gate_skip_reason="step_budget_exhausted"`。
   - 二次 finish 仍无 memory 写：放行，置
     `memory_gate_skip_reason="no_memory_write_after_forced_round"`。
3. **跳过留痕**：`checkpoint_side_effects.py::_record_lifecycle_events` 的 completed 分支，
   `memory_gate_skip_reason` 非空时发射 `memory_consolidation_skipped` 事件（payload 记 reason）。
4. **模型层**：`agent_run_event.py` CHECK 约束白名单追加 `'memory_consolidation_skipped'` +
   Alembic 迁移 `v1_11_4_f072_*`。
5. 统一适用、观察驱动（无 agent 白名单）；failed/cancelled/waiting 天然不强制；纯提示词 D6
   保留作语义兜底。

**Blocked by:** None (can start immediately；设计已评审收敛，见 ADR-0005)

**Status:** ready-for-agent

- [x] `_tool` 节点写标志累计进 `lifecycle["memory_gate_track"]`（write_file/edit_file、`memory/` 前缀分类、成功才计）
- [x] `_model` finish 分支门禁：强制轮注入（条件义务措辞、`runtime_intent="memory_consolidation"`、复用 repair 消息 id 模式）
- [x] 护栏：`forced_memory_consolidation` 只强制一轮；二次 finish 放行；步数预算不足放行 + skip_reason
- [x] `checkpoint_side_effects` completed 分支发射 `memory_consolidation_skipped`（两种 reason、幂等键）
- [x] 模型 CHECK 约束 + Alembic 迁移 `v1_11_4_f072_*`
- [x] `test_agent_runtime_node_executor.py` 先红后绿：有写无记忆→强制轮；有记忆写→直通；无写→直通；强制后仍无→放行+skip_reason；预算不足→放行+skip_reason
- [x] `test_agent_runtime_checkpoint_side_effects.py`：completed 终态事件发射（两种 reason）
- [x] 全量 pytest + `scripts/arch-guard.sh` 通过
- [x] 部署后复跑 04b66f75 同类任务验证记忆固化真实发生（或 `memory_consolidation_skipped` 留痕）——4af61b58 已验证（skip_reason=no_memory_write_after_forced_round，见 ADR 勘误后生产观察）
