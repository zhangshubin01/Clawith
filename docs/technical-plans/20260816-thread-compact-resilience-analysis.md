# Thread Compact 容错治本方案深度分析

> 日期：2026-08-16 ｜ 分支：f-shubin-0806 ｜ 关联事故：`invalid_thread_compact_output`（Run `80c41b22-a853-4554-878c-efff0b089fc6`）
> 现状：已应急切换 compact 模型为 deepseek-v4-pro（DB `cd1d82ec-…` + system_settings + .env）。本文分析**代码层治本**。
> **实施状态（2026-08-16）**：**全部路线已落地**。Phase 1（L1 + L3 + L6）、Phase 2（L2 自研状态机 + `complete_llm_once` 显式 `temperature=0`、`raw_invalid_tool_calls` 保留通道）、Phase 3（L4 方案 a 文本 JSON 通道；L5 经拍板保持 fail-closed 不做）均已实施于 `run_compactor.py` / `single_step.py`，测试见 `test_agent_runtime_run_compactor.py` 与 `test_llm_single_step.py`；§7 六项决策点记录见文末。**已通过真实事故 checkpoint 回放验证**（flash 事故模型与现行 pro 配置双路径一次成功，见 §8）。

---

## 1. 事故路径精确定位（代码级）

事故链（每步有据可查）：

1. 线程触达 80% 高水位 → `compact_run_if_needed` 节点（`node_executor.py:525`）调用 `RuntimeRunCompactorService.compact_if_needed`。
2. `_compact_batches`（`run_compactor.py:593-659`）逐批调用 `complete_llm_once`（`single_step.py:42`）。
3. deepseek-v4-flash 生成 `commit_thread_summary` 工具调用，**摘要文本内嵌双引号未转义**，`function.arguments` 不是合法 JSON。
4. `_sanitize_tool_calls_for_context`（`caller.py:86-158`）发现坏 JSON → 日志 `[LLM] Invalid tool arguments JSON for commit_thread_summary` → 返回 `(None, 修复指令, tool_name)`。
5. `complete_llm_once` 忠实地把修复指令放进 `LLMCompletionStep.retry_instruction`（`single_step.py:100-117`）——**但 `_compact_batches` 从不读取这个字段**。
6. `_summary_from_step`（`run_compactor.py:509-557`）看到 0 个工具调用、内容为空（模型只发了工具调用，无正文）→ 抛 `invalid_thread_compact_output`（`RunCompactorError`，`is_deterministic_compact_error=True`）。
7. `node_executor.py:528-540`：确定性错误 → lifecycle 置 `failed` + terminal。图级 `COMPACT_RETRY_POLICY`（`graph.py:50-58`）只重试瞬态错误 → **Run 硬失败，无任何修复/重试**。

**核心缺口一句话**：`retry_instruction` 是 `complete_llm_once` 明确留给上层（"那些决策属于 durable Graph"）的修复通道，model 节点完整消费了它（`model_step_service.py:812-822` + `node_executor.py:665-745` 的 repair 闭环），唯独 compact 节点把它丢弃。

## 2. 失败模式完整清单（Taxonomy）

| # | 失败模式 | 触发点 | 现状 | 备注 |
|---|---|---|---|---|
| 1 | 工具参数 JSON 非法（引号未转义/截断） | sanitizer → retry_instruction 被丢弃 | **硬失败** | 本次事故；flash 实测 2/5 合法 |
| 2 | 参数 JSON 非法 + 伴生正文 | 同上，但 `content` 非空 | 静默文本回退 | 回退内容可能是"以下是摘要："前缀，真正的摘要丢了 |
| 3 | 0 工具调用 + 纯文本 | `_summary_from_step` L514 | 文本回退（有意） | 有测试覆盖 |
| 4 | 0 工具调用 + JSON 文本体 | L526-529 | 结构化回退（有意） | 有测试覆盖 |
| 5 | 多重工具调用（≥2 个） | L511 | **硬失败** | 有测试断言 |
| 6 | 字段集合不匹配（缺/多字段） | L543 | **硬失败** | schema 已 `additionalProperties:false`，但模型可能不守 |
| 7 | 字段值非字符串 | L551 | **硬失败** | |
| 8 | `finish_reason=length` 截断，JSON 半截 | sanitizer 视为非法 JSON | 同 #1 硬失败 | 输出预算不足时会发生 |
| 9 | 文本工具协议（`<tool_call>` 包络） | `normalize_textual_tool_protocol` → textual_retry_instruction | 同 #1 被丢弃 | |
| 10 | 提供方瞬态错误 | `_compact_batches` L638-643 → `TransientRunCompactorError` | 图级重试 3 次 | 设计正确 |
| 11 | 提供方确定性错误（4xx/401 等） | L644-647 | 硬失败 | 正确 |
| 12 | 摘要超预算（>4096 tokens） | L649-653 | 硬失败 | 正确，防失控 |
| 13 | batch/输入超预算、低水位不达标 | L612-628, L739-757 | 硬失败 | 正确，契约保护 |

**结构性结论**：13 种模式中，#1/#2/#5/#6/#7/#8/#9 共 7 种是"模型输出形态问题"，本可通过 bounded repair 自动恢复；当前全部表现为 run 失败。

## 3. 与参考实现对比（本地参考仓库）

| 维度 | langchain `SummarizationMiddleware` | deepagents `SummarizationMiddleware` | Clawith Thread Compact |
|---|---|---|---|
| 输出形态 | **纯文本**（`response.text.strip()`） | 纯文本（继承 langchain） | **强制结构化工具调用**：exactly once + 5 个必填 string 字段 |
| 失败处理 | `with_retry`（LLM 调用层重试） | 同左 | 确定性错误零重试 |
| 传输通道 | 普通 content | 普通 content | provider 工具调用序列化（escaping 问题域所在） |
| 校验 | 无（文本即所得） | 无 | 严格（字段集合、类型、数量） |

要点：
- langgraph 本身没有 LLM 总结（其 `compact` 是 channel 状态变换，非总结）；总结能力是用户层实现。
- **langchain/deepagents 的"纯文本 + 重试"路线从根上绕开了整个 escaping/JSON 问题域**——没有工具调用信封，就没有"参数 JSON 非法"。
- Clawith 用五段结构化换取下游稳定消费，但实际下游（`context_builder.py:552-565`）只把 `thread_running_summary` 作为**模型可见上下文**展示，无任何程序化字段消费。即：结构的收益没有被真正兑现，脆弱性却实打实付出。

## 4. 治本方案分层设计

### L1 — 修复循环（对齐既有模式，最小改动）⭐ 核心
**位置**：`_compact_batches` 内 per-batch 循环；`_summary_from_step` 增加"可修复"信号。
**机制**：
- `step.retry_instruction` 非空（sanitizer 已给出通用修复指令）→ 作为 user 消息追加到该 batch 的 prompt，重试同批。
- `_summary_from_step` 对 #5/#6/#7/#8 抛专用 `CompactRepairNeeded(instruction)`（新异常类型，非确定性错误）：
  - 多调用 → "Call commit_thread_summary exactly once with all five fields"
  - 字段缺/多 → "Provide exactly these five string fields: …（枚举）"
  - 非字符串 → "All five fields must be strings"
  - 截断（finish_reason=length）→ "Response was truncated; return a shorter complete summary"
- 上限 `COMPACT_REPAIR_LIMIT = 2`（通用工具修复是 1 次；压缩失败代价是 run 失败，值得多 1 次；write_file 先例是 3 次）。
- **优先级修正**：`retry_instruction` 存在时优先重试而非文本回退（修复 #2 的信息丢失）。
- 重试耗尽 → 抛现有 `invalid_thread_compact_output`（**错误码不变**，行为兼容），message 携带 attempt 数与最终原因。
- **无需 checkpoint 化**：compact 无副作用（不执行工具、不落库），节点崩溃后整节点从 checkpoint 重跑安全；修复计数放在进程内即可。

### L2 — 程序化 JSON 修复（低成本高收益）
**目标**：把"让模型再答一次"降级为"本地修一次"。
**机制**：`json.loads` 失败后，对 arguments 字符串（或文本 JSON 体）做有界修复：
- 方案 a：引入 `json-repair`（成熟纯 Python 库，新增依赖）；
- 方案 b：自研 ~40 行状态机（在字符串边界内转义未转义引号），可单测，无依赖。
**风险**：修复结果可能语义偏差——但摘要是**软产物**（exact inputs 独立保留于 retained messages），偏差可接受；修复后仍走 `_SUMMARY_FIELDS` 校验。
**位置**：L1 重试前先试本地修复（省一次 6.6 万 token 的重发），或重试后兜底。

### L3 — 宽容校验（~20 行）
- 字段缺失 → 以 `""` 填充；多余字段 → 忽略（schema 仍严格指导模型，校验端宽容）。
- 字段非字符串 → 有界 `str()` 转换（截断），不整体失败。
- `invalid_thread_compact_output` 仅保留给结构性灾难（无工具调用且无任何文本且重试耗尽）。
- 与 L1 配合：重试耗尽后宽容收尾，避免"只差一个字段"整 run 失败。

### L4 — 输出契约重构（产品级，需拍板）
- **方案 a：文本 JSON 通道**（**已实施，2026-08-16**）——prompt 要求输出纯 JSON 文本体（五段字段），不走工具调用；坏 JSON 由 L2 状态机本地修复兜底（未引入 `response_format`，避免触及整个 LLM client 层）。传输层彻底绕开 provider 工具调用序列化的 escaping 问题域。这是 langchain/deepagents 路线的结构化版本。
- **方案 b：strict 工具模式**——若 deepseek 支持 OpenAI `strict: true` + `json_schema`（需实测），模型侧自行保证合法性。（未选）
- **方案 c：双通道**——先工具调用；非法则降级文本 JSON 通道（L1+L2 的自然延伸）。（未选，方案 a 已替换工具路径）
- 关键事实支撑：五段结构下游无程序化消费（§3），所以 a/c 的"结构"保留只是锦上添花，损失很小。

### L5 — 降级路径（架构级）
- **跳过压缩**：重试/修复全部耗尽后，返回 `RunCompactResult()`（不压缩）让 run 继续；风险是业务模型调用可能超上下文（会被 model 层预算检查转成另一个错误）。**决策（2026-08-16）：保持 fail-closed**——L4 落地后"彻底失败"仅剩结构性灾难（模型状态可疑），为罕见路径引入降级组件性价比不高。
- **无损存档**（deepagents 模式）：把被覆盖消息写入会话存档（后端文件/DB），摘要丢失不再致命，从而"跳过压缩"降级变得安全。属新组件。**暂不做**；若将来启用"跳过压缩"，必须先落地此项。

### L6 — 可观测性与模型侧配置
- 日志：每次修复尝试记录 `(batch_idx, attempt, reason, 原始 arguments 前 200 字符)`——本次事故若有此日志，根因当天即可定位。
- 错误信息携带 attempt 数与最终原因。
- compact 调用建议 `temperature=0`（压缩是确定性任务，抽样多样性无价值）；`max_output_tokens=8192` 已足够（摘要上限 4096）。

## 5. 分层对比

| 层 | 改动量 | 额外 LLM 成本 | 风险 | 覆盖的事故模式 |
|---|---|---|---|---|
| L1 | ~150 行 + 测试 | 失败路径重发 payload（~6.6万 tokens/次 ×2，deepseek 定价下可忽略） | 低；错误码不变 | #1/#2/#5/#6/#7/#8/#9 |
| L2 | ~50 行或 1 依赖 | 0 | 低（软产物） | #1/#2/#8 的本地修复 |
| L3 | ~20 行 | 0 | 极低 | #6/#7 收尾 |
| L4 | prompt/契约 + 回归 | 0 | 中（影响面最大） | 从根上消除 #1/#8/#9 |
| L5 | 新组件 | 0 | 中 | 兜底策略 |
| L6 | ~30 行 | 0 | 极低 | 可诊断性 |

## 6. 推荐实施路线

**Phase 1（立即可做，最小集）= L1 + L3 + L6**
- `run_compactor.py`：修复循环 + `CompactRepairNeeded` + 宽容校验 + 日志。
- 测试：扩展 `test_agent_runtime_run_compactor.py`（每个失败模式一条：修复成功 / 修复耗尽仍抛同一错误码）；`test_agent_runtime_node_executor.py` 增加"修复后 run 继续"图级用例；更新 `test_multiple_tool_calls_remain_deterministic_failure` 语义（改为"重试耗尽后仍确定性失败"）。
- 交付后可评估：用历史检查点重放（既有复现手段）验证 flash 下失败率归零。

**Phase 2（稳健化）= L2 + temperature=0**
- 自研或引入 json-repair；compact 请求 temperature=0。

**Phase 3（战略，需拍板）= L4 + L5**（**已按 §7 决策实施 L4 方案 a；L5 经拍板不做**）
- 文本 JSON 通道或 strict 模式；无损存档 + 跳过压缩降级。

## 7. 需要拍板的决策点

1. **五段结构化摘要是否产品必需？**（下游无程序化消费 → 若否，L4 方案 a 可大幅简化系统）→ **已拍板（2026-08-16）：非必需，实施 L4 方案 a**（复核确认 `thread_running_summary` 仅整体注入模型上下文，无字段级消费）。
2. 修复上限 2 次是否接受？（token 成本 vs 成功率；也可做成配置项）→ **未改，沿用 2 次**；如需配置化可另开。
3. 是否接受新增 `json-repair` 依赖？（或自研状态机）→ **已拍板：自研状态机**（Phase 2，无新依赖）。
4. 彻底失败时：保持 fail-closed（推荐）还是跳过压缩继续 run？→ **已拍板（2026-08-16）：保持 fail-closed，L5 不做**。
5. compact 请求 temperature=0 是否接受？（压缩场景建议接受）→ **已拍板：接受**（Phase 2 已实施）。
6. 是否顺带把 planning 模型也切 pro？（上轮遗留，与本方案无关但同源风险）→ **未决，与本方案无关**。

## 8. 真实事故回放验证（2026-08-16）

用事故 Run 的历史 checkpoint 对新代码做真实验证（一次性容器 `--entrypoint python` 挂载 `run_compactor.py`/`single_step.py`，env 透传不落盘输出，未动运行中服务）。

- **输入态**：checkpoint `1f1993af-a4a4-64ff-8189-65debeef2018`（ts 2026-08-16T06:23:21.152Z，compact 节点输入，lifecycle running/next_route=compact/error=None，messages 147 条，无 thread_summary，snapshots 含事故 initial_input）。注：事故链尾 `1f1993b0-6b6b-6e4e…` 实为失败后写入（lifecycle@397 failed），真正的 compact 输入态是前者。
- **两条路径**（同一输入态、同一 ledger 重建——96 条 `AgentToolExecution` + prior incomplete 补全、同一预算公式、真实 provider 调用、temperature=0）：
  - `deepseek-v4-flash`（事故模型）：**成功**。compacted=True，五字段摘要完整（中文，task 225 / completed 913 / decisions 421 / blocked 222 / next 372 chars），零修复重试（L1/L2/L3 均未触发）。
  - `deepseek-v4-pro`（现行配置）：**成功**。compacted=True，五字段摘要完整（275 / 744 / 446 / 212 / 328 chars），零修复重试。
  - 两路径均 `covered_through_message_id=2d9476ae-7c23-5d29-80b8-cc0ec66f0b07`、保留 11 条近期消息，边界一致。
- **结论**：同一失败输入在新代码下两条路径均一次成功——flash 事故模式归零，现行 pro 配置亦通过；L4 文本 JSON 通道（无 tools 声明）直接消除了事故中"工具参数内嵌引号未转义"的失败类别。
