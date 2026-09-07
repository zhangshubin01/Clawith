# 压缩 prior-run 折叠分叉修复方案（F2 回归）

- **状态**：评审定稿（Phase 4 通过，见 §5）
- **日期**：2026-09-07
- **触发**：Phase 5 验收 `a928f0b2 feat(compaction): F2 前缀缓存复用` 时发现的结构性正确性回归
- **裁决**：**评审通过**（方案为最小根治；另记录一条与本次修复无关的缓存效率观察，见 §4.4）

---

## 1. 结论（裁决开头）

F2（压缩请求复用主请求 cache-stable 前缀）在 multi-run 线程下引入一个**静默信息丢失回归**：压缩摘要请求的 covered 集合来自「checkpoint 原始投影」，而回放内容来自「主请求折叠投影」，两者对 prior-run 的 id 空间分叉 → prior-run 内容被静默丢弃，且 `prior_run_summary`（D-9 设计的折叠表示）也不被喂入。修复方向：**压缩请求必须与主请求使用同一「模型可见」投影**——covered span 覆盖到 prior-run 时，把 `shape.history` 里现成的 `prior_run_summary` 补喂进摘要输入的非缓存前缀尾段。改动最小、可回退、带回归测试。

---

## 2. 参考对比结论（Phase 1，≥10 项目）

**关键决策点**：①压缩/摘要输入必须与「模型可见」投影一致（一致性不变式）；②multi-run 边界折叠（prior-run → 摘要）如何表达；③摘要保真（不能静默丢内容）。

| # | 项目 | 层级 | 结论 | 诚实负结论 |
|---|---|---|---|---|
| 1 | **deepseek-harness** | T0 首选 | `deriveMessages()` 单一投影 + agent-loop 每次 llm 前断言「请求 ≡ 日志投影」；压缩请求逐字节重放最后路由请求的 system+tools+**被遮蔽区**消息（`region.ts buildSummarizationInput`）。这是「模型可见 ⟺ 已记录」的硬保证，压缩保真的地基。 | **无 multi-run 折叠**（单 session 连续对话），故无此分叉 bug；但「单一投影」纪律正是 Clawith F2 缺失、且本方案要补的。 |
| 2 | **deepagents** | T0 | `summarization.py`：触发后 LLM 摘要 + 全文 offload 到 backend；`_message_eviction.py` 超大结果落盘分段读；`_prompt_caching.py` 前缀缓存中间件。摘要作用于同一 `AgentState.messages`，单一投影。 | 无 run 边界折叠；offload 到文件的策略与本问题无关。 |
| 3 | **Letta/letta-code** | T1 | 上下文三层：recall（不可变经验）/ memory blocks / MemFS。「历史 ≠ 当前」用不可变 recall 表达，与 Clawith `prior_run_summary` 同思路。 | 不折叠（用不可变 recall），无「压缩时折叠分叉」问题。 |
| 4 | **jcode** | T1（研究报告） | 压缩三级阈值（80%软/95%硬/10%手动）+ 三模式 + 确定性护栏；「确定性护栏」= 压缩前后状态一致性，与「摘要必须保真」同源。 | 无 multi-run 折叠。 |
| 5 | **LangGraph 官方 summarization** | T0 栈 | `thread_summary` channel 与 `messages` 分属两 channel，摘要只覆盖已折叠的旧消息（SummaryNode）。Clawith `thread_summary` 是其工程化。 | 官方不处理「run 边界折叠」（Clawith 自研 `bound_current_run_window`）。 |
| 6 | **LangChain ConversationSummaryMemory** | 本地库 | 滚动 summary buffer memory，单会话单投影。 | 无 run 边界概念。 |
| 7 | **OpenHands** | T1 | `condenser.py` 历史压缩，单会话。 | 无 run 边界折叠。 |
| 8 | **gptme** | T1 | 轻量 context management，单会话。 | 无 run 边界折叠。 |
| 9 | **mem0** | T2 | 长期记忆抽取（不折叠、不摘要整段）。 | 机制不同路。 |
| 10 | **LLMLingua** | T2 | token 级 prompt 压缩，无摘要保真语义。 | 不适用（无内容级摘要）。 |
| 11 | **claw-code** | T2 | claw 生态 `compact.rs`，单会话。 | 表亲非上游；无 run 边界折叠。 |
| 12 | **context_engineering / how_to_fix_your_context** | 官方 notebook | 官方上下文工程方法论。 | 方法论参考，无此机制。 |

**对比结论**：几乎所有参考项目都是「单会话连续对话」，**没有** Clawith 的 multi-run「run 边界折叠」概念（`bound_current_run_window`），因此它们不存在这个 bug——也正因如此，没有可直接照抄的「multi-run 折叠 + 压缩保真」范式。唯一直接相关的是 deepseek-harness 的「**单一投影 + 请求≡日志投影断言**」纪律，它精确点出本 bug 的根因（两个投影分叉）与修复方向（统一投影）。

---

## 3. 根因（Phase 2，代码 + 执行双源）

### 3.1 症状

multi-run 线程（prior-run 遗留未压缩 tool facts + current-run 触发压缩）下，压缩完成后 prior-run 内容（含「目标 + 产物」指针）既没进摘要、也没留在 state → 永久丢失。

### 3.2 根因（代码源，逐函数核实）

压缩请求的 covered 内容与回放内容来自**两个不同投影**：

- **covered 集合**（`run_compactor.py:309 _thread_messages`）= `model_visible_thread_messages(runtime_messages_as_json(state))`（`thread_visibility.py:210`）→ 对 prior-run **保留原始 tool facts**（assistant+tool_calls、tool 结果），**原 id 保留**。
- **回放内容**（`model_step_service.py:1560 _build_history_messages`）遍历 `build.recent_thread_messages`，而它已被 `context_builder.py:696 bound_current_run_window`（`thread_visibility.py:137`）**折叠**：prior-run → 单条 `prior_run_summary`（id=`prior-run-summary:{run_id}`）。

`_compact_messages`（`run_compactor.py:988`，过滤在 `:1007-1009`）按 `entry.state_message_id in covered_ids` 回放 `shape.history`。两个投影对 prior-run 的 id 集**不相交** →：

- **G1**：prior-run tool facts（原 id）在 covered 但不在 `shape.history` → 被静默丢弃，不喂给摘要模型；
- **G2**：`prior_run_summary`（合成 id）在 `shape.history` 但不在 covered → 也不喂给摘要模型。

压缩成功后 `node_executor.py:715-718` 执行 `messages = RemoveMessage(REMOVE_ALL) + recent_messages`，compactable 前缀被物理移除 → prior-run 内容永久丢失。

### 3.3 执行源（实跑复现，真实函数）

用真实 `bound_current_run_window` + `model_visible_thread_messages` 对「prior-run 一次 tool exchange + current-run 指令」构造的线程实跑：

```
compactor 侧可见 id（= covered_ids 候选）: ['current', 'prior-assist-1', 'prior-input', 'prior-tool-1']
主请求侧 shape.history 的 id:              ['current', 'prior-run-summary:run-B']
covered 但不在 shape.history（将被静默丢弃）: ['prior-assist-1', 'prior-input', 'prior-tool-1']
prior_run_summary 是否在 covered_ids:       False
```

### 3.4 为什么不是 §4.4 已讨论的「预算截断」（F-B）

F-B 是「covered 取自无截断投影、主请求 history 取自带预算截断的第二次 build」→ covered 旧消息**仍在** `shape.history` 里（无截断 build 含全量），只是不在最近主请求里 → 缓存 miss（已接受）。本 bug 是**结构性、无条件**的：prior-run 在 `shape.history` 里被 `bound_current_run_window` 折叠成 `prior_run_summary`，原 id 从 `shape.history` 里消失，与预算无关。

### 3.5 回归确认（vs F2 之前）

F2 之前 `_payload`/`_summary_ready_blocks`（`git show a928f0b2~1`）把 prior-run tool exchange 合成 `historical_tool_exchange` 事实（保留原 id `block.message_ids[-1]`）喂入摘要 → 内容被保留。F2 删除该路径后，此保真丢失 → **回归**。

### 3.6 测试盲区

`test_agent_runtime_run_compactor.py:184 _request_shape(list(state["messages"]))` 直接从 `state["messages"]` 构造 shape（与生产数据流不符——生产 shape 来自 context_builder 折叠后的 `selected_thread_messages`），故现有 150 个测试不暴露此分叉。invariant 测试 `test_agent_runtime_model_step_service.py:487` 只断言「同一 build 两次调用逐字节一致」，且用 `recent_session_messages_snapshot=()` 空会话快照、无 prior-run。

---

## 4. 修复方案（Phase 3）

### 4.1 不变式（修复目标）

**压缩摘要输入必须与主请求的「模型可见」投影一致**：covered span 覆盖到的每条 checkpoint 消息，其「模型可见」表示都要喂给摘要模型。current-run 消息的可见表示 = 消息本身（id 匹配）；prior-run 消息的可见表示 = `prior_run_summary`（折叠形式，D-9 已将其措辞为「非当前任务」祈使安全）。

### 4.2 改动（最小、局部）

改 `backend/app/services/agent_runtime/run_compactor.py` 三处，不动其它文件：

1. **`_compact_messages`（:988）**：计算 orphan 集合 `covered_ids - {entry.state_message_id for entry in shape.history}`；非空（= 覆盖到 prior-run）时，从 `shape.history` 提取 `state_message_id` 以 `prior-run-summary:` 前缀开头的条目，把其 `message` 追加到**非缓存前缀尾段**（covered history 之后、summary 背景之后）。system + covered history 前缀不变，缓存行为不受影响。
2. **`_compact_dynamic_tokens`（:1052）**：同样条件下把 `prior_run_summary` 计入动态段 token 预算（保持一致记账）。
3. **`_compact_batch`（:1264-1273）shrink 校验基线**：把 `prior_run_summary` 计入 `covered_tokens`（「输入实际所见」口径），避免额外输入使 shrink 校验变松。

抽两个共享 helper 供三处复用，避免 `prior-run-summary:` 字面量重复：`_prior_run_summary_entry(shape) -> CompactHistoryMessage | None`（定位折叠条目）+ `_prior_run_covered_note(shape, covered_ids) -> CompactHistoryMessage | None`（orphan 非空时才返回该条目）。

**为什么放非缓存前缀尾段**：`prior_run_summary` 在**主请求**里是 history 首条（属 cache-stable 前缀），但本修复只求**正确性**（内容不丢），不为此改动压缩请求的前缀布局——缓存命中率维持既有「best-effort + 监控指标」口径（见 §4.4 相关观察）。若后续要追 prior_run 段的缓存命中，另立案。

### 4.3 回归测试（根因路径 + 终态各一）

在 `test_agent_runtime_run_compactor.py` 新增一个 multi-run 场景测试，**用生产数据流构造 shape**（`bound_current_run_window` 折叠 + `model_visible_thread_messages`，而非 `_request_shape(list(state["messages"]))`）：

1. **根因路径**：prior-run（一次完整 tool exchange + current 标记）+ current-run 消息触发压缩，断言摘要模型收到的 prompt 里包含 `prior_run_summary` 的「目标 / 产物摘录」字样（G2 修复）、且 prior-run 原始 tool facts 不被喂入（D-9 折叠语义不变）。
2. **终态**：压缩后 `result.recent_messages` 只含 current-run 消息、`covered_through_message_id` 越过 prior-run（既有断言不变），且 summary 文本非空。
3. **副作用断言（宪法 IV）**：单 run 场景（无 prior-run）时，orphan 集合为空 → 摘要请求内容与修复前逐字节一致（无缓存前缀回归）。

### 4.4 影响面 / 相关观察

- **爆炸半径**：仅 `run_compactor.py` 三处内部函数；`CompactRequestShape`/`RunCompactInputs`/`RunCompactResult` 契约不变，context_builder / node_executor / 消费方零改动。用 `detect_changes`/`trace_path` 复核：`_compact_messages`/`_compact_dynamic_tokens`/`_compact_batch` 的调用方仅在 `run_compactor.py` 内部。
- **相关观察（与本修复无关，另立案）**：`shape.history` 含 `recent_session_messages_snapshot`（`ChatMessage` 表，id 与 checkpoint 不同源），而 `covered_ids` 仅来自 checkpoint → session 快照**永远不在**压缩请求的 covered history 里 → 压缩请求前缀与主请求前缀（含 session 快照）逐字节不匹配 → 缓存 miss。这是**缓存效率**缺口（session 是独立存储、不被压缩移除，**无内容丢失**），落在已接受的「best-effort cache」口径内，建议单独跟踪，不并入本次正确性修复。（**已另立案**：`.scratch/compaction-slimming/issues/06-session-snapshot-cache-miss.md`）

---

## 5. 7 角度评审（Phase 4，含负向探针）

**1. 根因是否正确？** —— **通过**。正向依据：根因（两投影 id 分叉）解释双源全部证据——代码侧（§3.2 函数链）+ 执行侧（§3.3 实跑 4 行输出，G1 三项 orphan、G2 prior_run_summary 不在 covered）。已追到最深层因：不是「某条过滤写错」，而是 F2 用两个投影构建压缩请求、且未断言两投影一致性。**负向探针（反例测试）**：若根因是「prior-run 内容本就该丢」，则 `prior_run_summary`（D-9 明确要保留的「目标+产物」表示）不应存在于 `shape.history`；我对照了 `context_builder.py:696-760` 与 `thread_visibility.py:129-133`，它**确实存在**且是设计产物 → 反例推翻「该丢」假设，根因成立。

**2. 根治方案是否正确？** —— **通过**。正向依据：方案直接修复根因——恢复「模型可见 ⟺ 已记录」不变式（deepseek-harness 同款），把 prior-run 的模型可见表示（`prior_run_summary`）补进摘要输入。**负向探针（删除测试）**：删掉本方案（保持 `_compact_messages` 现样），根因会不会复发？——**会**，因为 orphan 消息仍被 `:1007-1009` 的 id 过滤静默丢弃，`node_executor.py:715-718` 仍物理移除前缀。

**3. 参考资料是否正确？** —— **通过**。正向依据：deepseek-harness 是 compaction 首选且同栈（LangGraph harness），其「单一投影 + 请求≡日志投影」是本 bug 的直接对症范式；deepagents/LangGraph 官方确认「摘要作用于同一可见消息集」为通用做法。**负向探针**：我怀疑过「claw-code 是同生态、或可抄其 compact.rs」——核下来 claw-code 自述 museum exhibit 且**无 run 边界折叠**（表亲非上游），误引风险排除；deepseek-harness 的 `buildSummarizationInput` 已在研究报告 `20260829-deepseek-harness-study.md` 抽查核实，无误。

**4. 副作用与爆炸半径是否排查完？** —— **通过**。①副作用面：无外部写（不改 checkpoint 写入路径，只改摘要**输入**构造）；缓存前缀不变（prior_run_summary 加在尾段）；无连接/资源/权限变化。②影响面：改动收敛于 `run_compactor.py` 三函数，消费方零改动（§4.4）；验证范围 = `backend` 单测（`.venv/bin/python -m pytest`）+ `scripts/arch-guard.sh`。**负向探针**：我特意找过「会漏掉的消费者」——shrink 校验 `_compact_batch:1264` 若不计入 prior_run_summary 会变松，已在 §4.2 第 3 点显式纳入；`_compact_dynamic_tokens` 预算若漏计会导致装箱估算偏小，已在 §4.2 第 2 点纳入。

**5. 是否最优且必要？** —— **通过**。①枚举 ≥3 候选：更简单「仅文档标注 + pin 测试」/ 本方案「补喂 prior_run_summary」/ 更彻底「恢复 `_summary_ready_blocks` 合成 prior-run tool facts」。②本方案修的是**已发生的回归**（F2 相对旧 `_payload` 丢失保真，实跑复现），非臆想风险（宪法 II）。**负向探针（用更简单档）**：「仅文档标注」能否解决？——**不能**，它不阻止内容永久丢失，只是把静默回归变显式；「恢复旧合成」是否更彻底？——是但更重（重新引入 F2 已删机制，且 prior-run 完整 tool facts 违反 D-9「折叠」语义）。本方案是 Ponytail 阶梯最低有效档。

**6. 是否已有可复用逻辑？** —— **通过**。正向依据：`prior_run_summary` 已在 `shape.history` 现成（`_build_history_messages` 产出），无需重算；orphan 判定复用 `covered_ids`/`state_message_id` 既有字段。**负向探针**：我查过知识图谱/代码是否已有「prior-run 折叠摘要」等价逻辑——有 `bound_current_run_window`（折叠）+ `_prior_run_summary`（措辞），本方案直接复用其产物，不新造。

**7. 会破坏 Clawith 特性吗？** —— **通过**。逐条过宪法 C1–C6 + 红线：C1 证据先行（§3 双源）；C2 最小改动（三函数，无重构）；C3 契约所有权（state/`thread_summary` 形状不变）；C4 测试证行为（§4.3）；C5 保留既有工作（缓存前缀、D-9 折叠语义、F1 指令位置均不动）；C6 模块边界（改在 run_compactor 内部）。红线：checkpoint 语义不变（只改摘要输入）、exactly-once 不涉及、前缀缓存稳定性不降（尾段追加）、飞书/WS 不涉及。**负向探针**：我试过把方案对「前缀缓存稳定性」红线过一遍——prior_run_summary 追加在**尾段**、system+covered 前缀逐字节不变 → 不碰红线。

---

## 6. 落地闭环（Phase 5 待办）

实现后必须跑 `code-review` 对照本方案复核 diff（Spec 轴）：diff 是否忠实实现 §4.2/§4.3、无夹带范围外改动、无「评审时没提过的机制」。diff 与方案一致 = 交付闭环。
