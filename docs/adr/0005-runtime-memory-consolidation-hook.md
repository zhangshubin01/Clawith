# ADR-0005: 运行时记忆固化门禁（Memory Consolidation Gate）

- **状态**: 已接受（2026-08-27）

## 背景

「记忆固化」此前是**纯提示词义务**（D6：`agent_context.py:371-387` `_MEMORY_MAINTENANCE`，在
`{"read_file","write_file"} <= allowed` 时注入每次推理的 system prompt；`b3aadcea` 起含收尾判定条款）。

2026-08-27 复盘发现该义务**可被模型无视**：run `04b66f75`（及 `e2ef5629`、`933a4283`）重写了
CalculatorApp / android-cli-test 的产品代码（write_file/edit_file 大量调用、构建成功），但零
memory 写入——收尾推理原文只谈「give the final summary」，对 memory 判定只字未提，直接交卷；
agent 的 `memory/memory.md` 停留在 8-16 旧条目，与已更新的 README 失同步。D6 提示词当时已在
生产（部署 08:48–09:00 UTC，run 09:07 UTC 启动）。对照 main（45fc701c）：同样无任何运行时
强制——memory 写入唯一路径是模型自主调用写工具。

结论：义务必须落到**运行时**。本 ADR 定义收尾路径上的强制检测与注入机制（Clawith 运行时词表
中的 **Memory Consolidation Gate**），纯提示词义务保留作语义兜底。

## 决策

| # | 决策点 | 结论 | 理由 |
|---|---|---|---|
| 1 | 检测数据源 | **`_tool` 节点把写标志累计进 lifecycle**（`node_executor.py::_tool`，执行成功即 `result.error is None` 才计） | checkpoint 状态天然按 run 隔离（`langgraph_driver.py:488` 每次 start 全新 lifecycle）；Thread Compact 会用 `RemoveMessage(REMOVE_ALL_MESSAGES)` 抹历史消息，收尾扫 messages 会漏判；零 DB 依赖 |
| 2 | workspace 写工具集 | **只数 `write_file` + `edit_file`** | 与 D6 门控语义一致；delete_file/move_file 极少产生耐用知识且无法写入记忆，误报率高（内置写类工具共 4 个：write/delete/move/edit） |
| 3 | memory 写入判定 | **任何 `memory/` 前缀的写都算**（工具 path 为 workspace 相对路径，DB 台账实证） | 实测 agent memory/ 目录含 memory.md、MEMORY_INDEX.md、reflections.md、curiosity_journal.md、user_profile.md；心跳 run 写 `memory/curiosity_journal.md`——只认 memory.md 会把自演化正常的心跳 run 误判为「没写记忆」 |
| 4 | 跳过留痕落点 | **新事件类型 `memory_consolidation_skipped`**：改 `agent_run_event.py` CHECK 约束白名单 + Alembic 迁移，在 `checkpoint_side_effects.py::_record_lifecycle_events` 的 completed 分支发射 | 可索引可查询（前端对事件类型零引用，无前端影响）；运行时拿不到模型的判定动机，payload 只记客观事实（强制轮后仍无写入 / 步数预算不足） |
| 5 | 成本边界 | **至多 1 次强制轮**（lifecycle 标志护栏）；注入前检查剩余模型步数，不足则放行 + 按 #4 留痕 | 强制轮多一次模型调用；护栏防循环；步数预算防「强制轮把临近上限的 run 推到 model_step_limit_reached 失败」 |
| 6 | 适用范围 | **统一适用、观察驱动**：无 agent/tenant 白名单；failed/cancelled/waiting 不强制（门禁只在 finish 意图成功路径上拦截，天然如此） | 工具可用性隐含于观察数据（无 write 工具则观察不到写调用）；多租户平台少一个配置面 |
| 7 | 群聊/委托 run | **不 carve-out，统一强制** | `system_role` 全库只有 NULL/`group_planning`（成员 run 无标记）；`parent_run_id` 同时覆盖群子 run 与 a2a 委托 run，无法区分；统一规则无识别成本，群聊延迟风险有界（仅「写了 workspace 未写 memory」的收尾多一轮） |

## 实现形态

- `backend/app/services/agent_runtime/node_executor.py::_tool`：
  执行成功后解析 `current_call`（`function.name` ∈ {write_file, edit_file}、`function.arguments`
  的 `path`），累计 `lifecycle["memory_gate_track"] = {"workspace_writes": n, "memory_writes": n}`
  （path 以 `memory/` 开头者计 memory 写）。
- `backend/app/services/agent_runtime/node_executor.py::_model` finish 分支（设置
  `status="verifying"` 之前）：gate 条件 = `workspace_writes > 0` 且 `memory_writes == 0` 且未置
  `forced_memory_consolidation`；预算检查 `step_count + 1 <= model_turn_limit`。满足则：
  - 追加 `role=user`、`runtime_intent="memory_consolidation"` 控制消息（复用 repair 消息模式
    `_runtime_message_id`；措辞为条件义务——有耐用跨会话信息且未记录则先 read 再原位合并写入
    `memory/memory.md` 并同步 `memory/MEMORY_INDEX.md`，判定无则跳过直接交卷）；
  - `lifecycle["forced_memory_consolidation"] = True`，`status="running"`、
    `pending_tool_calls=[]`，`_schedule_compact(lifecycle)`（即 `next_route="compact"`，与 repair
    模式同路：先走 checkpoint 边效/压缩节点再回 model 节点）。
  - 预算不足则放行 finish，置 `lifecycle["memory_gate_skip_reason"]="step_budget_exhausted"`。
  - 二次 finish 且仍无 memory 写：放行，置
    `lifecycle["memory_gate_skip_reason"]="no_memory_write_after_forced_round"`。
- `backend/app/services/agent_runtime/checkpoint_side_effects.py::_record_lifecycle_events`：
  completed 终态且 `memory_gate_skip_reason` 非空时，追加事件
  `("memory_consolidation_skipped", payload={"skip_reason", "workspace_writes", "memory_writes"},
  幂等键 checkpoint:{id}:memory_consolidation_skipped)`。
- `backend/app/models/agent_run_event.py`：CHECK 约束白名单追加 `'memory_consolidation_skipped'`；
  Alembic 迁移 `v1_11_4_f072_*`（延续 v1_11_4_fXXX 惯例）。
- 纯提示词 D6 保留不动（语义兜底）。

## 测试

- `backend/tests/test_agent_runtime_node_executor.py` 先红后绿：有 workspace 写无 memory 写 →
  强制轮注入；有 memory 写 → 直通；无任何写 → 直通；强制轮后仍无 → 放行 + skip_reason；
  预算不足 → 放行 + skip_reason。
- `backend/tests/test_agent_runtime_checkpoint_side_effects.py`：completed 终态发射
  `memory_consolidation_skipped`（两种 reason）。
- 模型 CHECK 约束迁移的 schema 断言。

## 回滚

代码 revert 即可；对既有数据无损（不写任何业务数据，只新增事件行与 checkpoint lifecycle 字段，
旧 checkpoint 缺省安全）。Alembic downgrade 前须先删除该事件类型的行（否则约束重建失败）；
或保留约束不降级（旧代码不读新类型，无害）。

## 后果

- 正：记忆固化从「模型自觉」变为「运行时保证」；跳过显式留痕、可审计可告警；心跳等自演化
  run 经 `memory/` 分类天然免疫；无写操作的 run 零额外开销。
- 负：每次「写代码未写记忆」的成功收尾多一轮模型调用（成本/延迟，有界）；强制轮不能保证模型
  听话——它可能继续做产品工作而非写记忆，护栏只保证只强制一次、二次 finish 放行 + 留痕。
- 中性：lifecycle 新增字段对旧 checkpoint 兼容；事件类型白名单是显式枚举，新增类型需迁移。
