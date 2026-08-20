# Direct Chat 多 run 共线程上下文污染根治方案

日期：2026-08-19
状态：已定位根因，方案待实施

## 1. 事故

用户对「Android 工程师 4」agent（`27d55a64-dd6a-40c9-890b-ea326f59cf4a`）发「优化这个Android 项目」，agent 却回复「✅ 项目重新编译完成 … app-debug-20260819-1457.apk」。

台账证明：本次 run（`79f4b0e6`，goal=优化）只调了 `read_file`×6 + `list_files`×2，**零次 android_compile**，最终却照抄了上一个 run（`3cb9e859`，goal=重新编译项目）的历史产物（旧 APK 路径）与收尾总结。

## 2. 根因（代码级）

Direct Chat 多个 run **共用一条 LangGraph thread**（`thread_id = session.id`，见 `chat_intake.py:683/703`）。而 `context_builder.build()` 的窗口选择**无 run 边界感知**：

- `context_builder.build()`（`context_builder.py:534-546`）把**整条 thread**（`runtime_messages_as_json(state)`，跨所有 run）直接喂给 `build_recent_tool_safe_window`。
- `select_recent_blocks`（`tool_exchange.py:733-833`）从尾部向前选块，只按 `target_messages`/`token_budget` 裁剪，**会跨越 run 边界**把上一个 run 的消息一并纳入。
- 后续 `_prompt_messages` 里的 `model_visible_thread_messages`（`thread_visibility.py:9-45`）只过滤上一 run 的「纯文本 assistant 草稿」和「repair/finish 提醒」user 消息，**保留**上一 run 的普通 user 指令（`runtime_input=="current"` 但 `runtime_run_id` 是旧的）和完整 tool exchange（含旧 APK 路径的 tool result）。

于是模型在新 run 里同时看到「重新编译项目」旧指令 + 编译成功的旧 tool result（`app-debug-20260819-1457.apk`），压过了「优化项目」新指令。

## 3. 参考资料研究结论

结合本地参考仓库（`~/Documents/UGit/{langgraph,langchain,deepagents}`）一手源码：

### 3.1 LangChain `trim_messages`（官方「硬左边界」原语）

`langchain/libs/core/langchain_core/messages/utils.py:1133`：

- `strategy="last"` + `max_tokens` + `start_on=<type>` —— 从尾部取最近 token，但**忽略第一个 `start_on` 类型之前的所有消息**。
- 这正是「窗口硬左边界」的官方语义。Clawith 的 `select_recent_blocks` 已经实现了「从尾部取最近 + tool exchange 原子性」，**唯独缺 `start_on`（边界）这一维**。

对应关系：LangChain 用 message type（human/ai/tool）作边界；Clawith 的边界是 run marker（`runtime_input=="current"` + `runtime_run_id`），语义完全同构。

### 3.2 deepagents `SummarizationMiddleware`（「摘要 + 保留最近」范式）

`deepagents/libs/deepagents/deepagents/middleware/summarization.py`：

- 触发（`trigger`：token/message/fraction 阈值）+ 保留（`keep`：最近 N 条）。
- 摘要事件 `{cutoff_index, summary_message}` + `_apply_event_to_messages` 重建有效消息为 `[summary_message] + messages[cutoff_index:]`。
- 摘要消息带 `additional_kwargs={"lc_source": "summarization"}` 标记，供链式摘要时过滤旧摘要。
- 轻量预优化 `truncate_args`：keep 窗口之前的消息，裁剪 `write_file`/`edit_file` 大参数，保留 head+tail preview。

### 3.3 LangGraph `add_messages` + `RemoveMessage`

`langgraph/libs/langgraph/langgraph/graph/message.py`：消息 reducer 支持按 ID 墓碑化（`RemoveMessage`）、`REMOVE_ALL_MESSAGES`。Clawith 的 messages channel 已用 `add_messages`。

### 3.4 对照 Clawith 现状：基础设施已在，缺的是窗口选择那一环

Clawith 已经有 run 边界的一切基础设施：

- run marker：`runtime_input=="current"` + `runtime_run_id`（`langgraph_driver._initial_thread_message`）。
- 切片器：`_current_run_messages`（`model_step_service.py:349`）、`model_visible_thread_messages`（`thread_visibility.py`）、`_protected_current_run_message_ids`（`run_compactor.py:260`）。

**唯一缺的是**：`context_builder.build()` → `select_recent_blocks` 这条「窗口选择」路径没有把 run 边界作为硬左边界。补上这一环，即对齐 `trim_messages(start_on=...)` 语义。

## 4. 方案（三层）

### L1（核心根治）：run 边界硬左边界 + 上一 run 模板化单句摘要

在 `context_builder.build()` 里，窗口选择前先做 run 边界切片：

1. `thread_messages = runtime_messages_as_json(state)`（完整 thread）。
2. 定位 `current_start` = 第一个 `runtime_input=="current"` 且 `runtime_run_id == context.run_id` 的消息 index。
3. 若 `current_start > 0`（存在上一 run 历史）：
   - 把 `thread_messages[:current_start]` 压缩成**模板化单句摘要**（零模型成本、确定性）——从上一 run 的 current marker 提取 goal，从上一 run 的 tool result 提取关键 `result_ref`/产物路径。
   - 合成一条 user 消息（参考 deepagents 的 `summary_message` 语义）前置到窗口输入。
4. 窗口输入 = `[摘要消息?] + thread_messages[current_start:]`，再喂给 `build_recent_tool_safe_window`。

**关键发现（简化设计）**：无需显式「仅 direct 生效」开关。group/a2a/heartbeat 等非 direct 场景的 thread 天然每 run 独立（`runtime_thread_id` 为 None → `str(run.id)`），thread 里只有当前 run 的消息，`current_start == 0`，切片是 **no-op**。因此「run 边界硬左边界」是通用的、自动正确的——direct 生效，其他场景零影响。这比显式判断 session_type 更简洁、更健壮，且严格满足「不动 group」的意图。

### L2：当前 goal 末尾强控制消息

`_current_run_directive`（`model_step_service.py:911`）目前只在「history 里没找到 initial message」时作为 fallback 注入。强化为：在末尾控制消息位置，始终保证当前 run goal 的强控制存在，不被上一 run 的指令稀释。

### L3：产物新鲜度台账兜底

最终回复引用产物路径时，校验该路径是否出现在当前 run 的 `agent_tool_executions.result_ref` 里；不在则拦截/降级。

## 5. 关键设计决策（用户已拍板）

1. 上一 run 内容：**压缩成模板化单句摘要**（零模型成本、确定性），不完全剥离（保留「上轮编译过」的语境）。
2. 窗口硬左边界：**当前 run 第一条消息**（`runtime_input=="current"`），token 预算只在其内从尾向前裁剪。
3. 作用域：**通用 run 边界切片**（direct 生效、非 direct 自动 no-op），替代「显式仅 direct 开关」。
4. 摘要生成：**纯文本模板**，不走 `run_compactor` 模型摘要链路（根治场景要确定性护栏）。

## 6. 实施步骤（TDD）

1. 红：`tool_exchange` / `context_builder` 测试——跨 run 的上一 run 消息不得进入窗口，且生成单句摘要。
2. 绿：实现 run 边界切片 + 模板摘要。
3. 绿：L2 goal 强控制消息。
4. 绿：L3 台账兜底。
5. 全量测试 + `scripts/arch-guard.sh`。

## 7. 落地状态

- **L1（已落地，提交 `b4a18ba6`）**：`bound_current_run_window` + `ContextBuilder.build` 接入；5 个新测试；全量 2489 passed；arch-guard / ruff 通过。
- **L2（不做）**：现有 `_prompt_messages` 已把窗口最后一条 user 消息（当前 run 的 current marker = goal 原文）提取为末尾控制消息，L1 之后上一 run 指令不再前置，当前 goal 天然成为唯一末尾指令；再写死会与 `_TURN_CONTINUATION_MESSAGE` 防循环机制冲突。
- **L3（单独立项，已研究待实施 → `20260820-artifact-freshness-ledger-fallback-research.md`）**：产物新鲜度台账兜底——检测最终回复引用的产物路径是否在当前 run 的台账里（事实源为 `result_metadata.artifact_refs`，**非** `result_ref`），不在则拦截/降级。属「检测器」而非「根治」，按 `b31f51ec`/`d9eb094e` 教训须用真实 checkpoint 验证字段形态，独立 commit 实施。研究另发现前置缺口：`android_compile`（legacy 文本工具）不 emit 结构化 `artifact_refs`，须先补、与检测器同一批落地。
