# 压缩进度锚点：打破 compactor 失忆循环（第 2/3 层）

- 状态：评审完成（9 问双轴评审，结论「需修改后实施」，6 项修正已并入本文档与 ADR-0015；待用户确认 Q7 阈值校准后实施）
- 日期：2026-09-03
- 观察文档：`docs/analysis/2026-09-03-compactor-loop-dc557d91.md`（run dc557d91 完整证据链）
- ADR：`docs/adr/0015-compaction-progress-anchor.md`

## 1. 问题

run dc557d91 实测：62 分钟 / 86 步 / 真实压缩 ≥6 次（每 3–7 分钟一次）；压缩后消息前缀三次重建出完全相同的起点（prefix=697aef1a1281，token 10675/10663/10467）；工具调用序列跨轮重复；run 结束摘要仍自认「三项 P1 修复均未执行」（期间 edit_file 14 次）。循环还通过 thread 上下文传染给下一个 run（8ef42390 复现相同读取路径）。

**根因**：摘要输入缺少「已完成动作」的事实。tool_exchange 进摘要时被替换为 ledger 一句话摘要（edit_file 仅「Replaced N occurrence(s) in path」，无内容）——摘要指令再完善（已含循环防护条文）也无法从输入变出事实，只能保守认定「未完成」→ 重建后重做 → 膨胀 → 再压缩。

**参考资料对照**：deepseek-harness（确定性 pruner 先砍后摘要、裁完跳过摘要）、deepagents `_message_eviction`（head+tail 驱逐）、官方 how_to_fix_your_context 04/05（pruning/summarization 实践与小模型摘要）、mem0（ADD-only 事实累积）、LLMLingua（token 级压缩，**不采纳**：编码线程必须 preserve exact identifiers）。

## 2. 已定决策（访谈全按推荐）

| # | 决策 | 内容 |
|---|---|---|
| Q1 | 破法组合 | **D+A 先行**；B 条件触发；不做 token 级压缩 |
| Q2 | 完成判定 | 机械口径：status=succeeded 即入流水，无 verify 语义 |
| Q3 | 循环熔断 | 纳入，默认只告警、`terminate_on_loop` 开关默认 false；与第 5 层开场白熔断共用指纹检测基建 |
| Q4 | 摘要模型 | 维持 flash；换模型留作票 11 A/B 对照项 |
| Q5 | D 窗口语义 | 沿用 retained 预算语义（窗口=压缩后会保留的部分），D=把压缩时的确定性替换常态化到每步，不引入第二套滑窗 |
| Q6 | A 注入位置 | payload 新增独立字段 `completed_actions`（ledger 构造、ADD-only、上限 50 条/约 2KB、按 tool_execution_id 去重），**绕开 `_short_result` 500 字符截断**；摘要指令加「权威进度事实」条文；additive 兼容 v1 |
| Q7 | 熔断阈值 | 同一 prefix **相邻重现**、间隔 ≥1 次真实压缩、且相邻出现间工具哈希序列相同 → **逐次计数**（前缀三现=2 次循环）；run 内累计 ≥1 次循环 = 告警（Langfuse event + 日志） |
| Q8 | D 落点 | **写 checkpoint 原子替换**（`_tool` 尾部与结果同帧）；不做读时投影（`retry_model` 语义坑） |

## 3. 设计

### 3.1 D：确定性即时结算（deterministic step settlement）

- **语义**：已完成（status 终态）的工具交换，一旦离开 recent 窗口（沿用 `_compactable_prefix` 的 retained 预算语义），其工具结果即被替换为 ledger 摘要——**每步做，不等压缩水位**。
- **落点（Q8 定案）**：`_tool` 尾部与结果消息**同帧写 checkpoint 原子替换**（全量重建先例 `_compact` 的 RemoveMessage，`node_executor.py:700-709`）；替换内容只取自已落库 ledger（防 replay 分叉）；与 `summary_covered_through_message_id` 水位防交错。不做读时投影（`retry_model` 语义会让模型重发已完成调用）。
- **替换形态（评审 Q1 定案）**：**块级原子替换**——assistant tool_calls 与其全部结果消息同帧移除，代之以**单个 user 合成消息**（id 复用被删块 `message_ids[-1]`，格式先例 `run_compactor.py:451-458` 的 `historical_tool_exchange`，内容禁嵌时间戳/随机值保重放幂等）。只替换结果会走 `_resolve_incomplete_exchange` 的 succeeded 分支 → `retry_model=True`（`tool_exchange.py:525`）→ 模型重发已完成调用，重蹈投影坑；normal 块原样透传、二次摘要不会发生。
- **窗口计算（评审 Q3 补）**：与 `_compactable_prefix` 一致，排除 `_protected_current_run_message_ids`（`run_compactor.py:271-318`）的 current_input/repair/repair_draft/resume 消息——protected 永不结算。
- **卡片事件核对（评审 Q1 附）**：消息移除后核对 `_runtime_observation_events`（`checkpoint_side_effects.py:381+`）的 Web Chat 活动派生不因 removal 丢卡片。
- **效果**：每步 4–6K token 增长的主燃料（窗口外工具结果全量）被确定性砍掉 → 压缩触发频率大幅下降；即使触发，摘要输入的 covered_messages 已含大量已结算块，摘要更快更小。

### 3.2 A：已完成动作流水（completed_actions）

- **构造**（确定性、零 LLM 成本）：从 ledger 读取本 run 的已完成工具执行，取 tool_name/path/status/结算时间，ADD-only 累积；按 tool_execution_id 去重；容量 50 条 / 约 2KB，超出裁最旧（流水是「最近进度」，不是全历史）。
- **注入**：压缩 payload（`_payload`）新增 `completed_actions` 字段；`schema_version` 保持 additive 兼容（v1 消费者忽略未知字段；构造方在旧 summary 合并时同步搬运）。
- **摘要指令条文**（`_COMPACTION_INSTRUCTION` 追加）：completed_actions 是权威进度事实——已列出的动作不得在 Pending Jobs/Next Step 中重复安排；Current Work 必须以其为准表达进度。
- **摘要指令 precedence（评审 Q4 定案，三条）**：① completed_actions 是「完成与否」的权威，historical_tool_exchange 仅为细节佐证；② failed 条目不算已完成，进 Pending Jobs 时标注为**重试**而非新任务；③ v1 旧 summary 中「自认未完成」与 completed_actions 冲突时以 completed_actions 为准（旧 summary 失真事实不改写）。另注意：`omitted_tool_exchanges` 是主模型 prompt 字段（预算溢出附注），与 payload 无关，勿混。
- 数据源与读取接口：待核实（子代理报告中，复用 compactor 现有的 tool_execution_ledger 读取路径）。

### 3.3 熔断：run 内循环检测

- **指纹（评审 Q9 补：产物化）**：现仅以日志行 `[LLM-CacheFp]` 存在（`model_step_service.py:2892`），需抽成可调用产物（tools_fp / msg_chain，计算处 `model_step_service.py:713-753`）；检测状态存储点=lifecycle 新字段候选。
- **判据（评审 Q7 校准，原口径硬伤修正）**：同一 prefix **相邻重现**、相邻出现间发生 ≥1 次真实压缩（以 Token Cache ratio=100% 指纹或 compactor 成功返回为准）、且相邻出现间工具哈希序列相同 → **逐次计数**（前缀三现=2 次循环）；run 内累计 ≥1 次循环 → 告警。（原口径「三现=1 次循环、run 累计 ≥2 次告警」下，旗舰证据 run dc557d91 自身不触发——已废。）
- **动作**：Langfuse event（`loop_detected`，带 run_id/prefix/轮次）+ 后端 WARNING 日志；`terminate_on_loop` 配置默认 false，开启时终止 run 并走现有终态化路径。
- 与第 5 层开场白熔断共用「重复模式检测」基建（检测器接口独立，两处消费者各自阈值）。

### 3.4 B（条件触发，暂不实施）

- **触发条件**：D+A 上线后，若真实 run 仍出现「摘要自认未完成但账本有成功记录」型循环，再实施。
- **已核实原料**：`workspace_file_revisions`（before/after 全文，models/workspace.py:28）、ledger `sanitized_arguments`（edit_file old/new 全文，models/agent_tool_execution.py:82）。
- **连带改动**：`_short_result`（tool_exchange.py:181-185）500 字符截断需放宽或 bypass——这正是暂缓的原因。

## 4. 测试策略

1. **D 单测**：构造含窗口外已完成交换的消息序列 → 断言组装输出中窗口内保留全文、窗口外为 ledger 摘要（**块级**：assistant tool_calls 与结果同帧消失、单个 user 合成消息、id=`message_ids[-1]`）；未完成/屏障交换永不替换；**protected repair/resume 消息不被结算**；幂等账本与 checkpoint 不受影响；每步结算后 `validate_tool_exchange_integrity`（`tool_exchange.py:716+`）断言通过。
2. **A 单测**：流水构造（去重/容量/排序）、payload 注入形状、旧 summary 合并时流水搬运；摘要指令条文存在性（文案断言，含 precedence 三条）；payload 过投影 + 结构校验（8 section heading）。
3. **熔断单测**：prefix 序列喂入（「间隔无压缩」「间隔有压缩」×「工具哈希序列相同/不同」交叉）→ 逐次计数与阈值 ≥1 判定正确；`terminate_on_loop=false` 时仅告警。
4. **端到端（评审 Q8 补）**：dc557d91 轨迹重放——断言 D+A 生效后循环签名（prefix 三现 + 工具序列重复）不再现；杀 run 重放无分叉；D×压缩交错（结算与 compact 同窗口并发次序）；protected 不被结算。
5. **回归**：现有 compactor 测试全过（实核规模：test_agent_runtime_run_compactor 30 用例、thread_compact_contract 9、test_tool_exchange 11、node_executor 44）。

## 5. 实施顺序与票据

1. 票 A：D（写 checkpoint 块级原子结算）+ A（流水）——本方案主体，先行。
2. 票 B：熔断检测器 + 告警 + 配置开关。
3. 票 C（条件）：B 内容级摘要——挂条件，不排期。

## 6. 风险

- 窗口外工具结果只剩一句话摘要 → 模型丢失窗口外细节：以 retained 预算窗口对冲（窗口内永远全文）。
- 流水容量裁剪丢旧进度：容量 50 条覆盖本 run 绝大多数结算量；裁剪只裁最旧，进度语义单调。
- 熔断误报：默认只告警，不打扰 run；终止需显式开启。
- payload 新字段与 v1 消费者兼容性：additive 设计 + 消费方按缺失容忍。

## 7. 实施落点（已核实，子代理检索报告）

**每步输入主路径**：`_prompt_messages`（`backend/app/services/agent_runtime/model_step_service.py:1299-1574`）→ `build_recent_tool_safe_window`（`context_builder.py:726-732`）→ `build_message_blocks`（`tool_exchange.py:562-702`）。预算内工具结果**全量保留无截断**（证实问题背景）；已有雏形 `omitted_tool_exchanges` 只在预算溢出兜底时打 block B 附注（`model_step_service.py:1181-1185, 1506-1533`），非原位替换。

**D 的落点两选**：

- **A. 写 checkpoint 原子替换（子代理推荐）**：`_tool` 尾部 messages update（`node_executor.py:1193-1217`），与结果消息同帧替换「已 settle 且出窗」的旧 exchange 为合成摘要，参考 `_compact` 的 `RemoveMessage(REMOVE_ALL_MESSAGES)` 全量重建先例（`node_executor.py:700-709`）；或 `compact_if_needed` 的 no-op 分支（`run_compactor.py:791-792` 前）。约束：结算内容只取自已落库 ledger（防 replay 分叉）；与 `summary_covered_through_message_id` 水位防交错。
- **B. 读时投影（不改 checkpoint）**：`context_builder.py:726-732` 或 `make_message` 遍历处替换。天然幂等、ledger 已在 `_load`（`model_step_service.py:1975-2096`）载入全 run map 零额外 DB 读；但需自行处理 `retry_model` 语义（出窗 exchange 置 retry_model=True 的原因=调用方被摘除后模型会重发，`tool_exchange.py:814`），否则「已完成的调用被摘除→模型重发」成为循环新形态。

**可复用件**：合成消息格式照抄 `run_compactor.py:451-465`（`historical_tool_exchange`，块级替换形态见 §3.1）；摘要生成 `_summary_for_exchange`（`tool_exchange.py:188-235`）+ settle 判定 `_guard_observed_results`（实核 `261-325`）；ledger 接口 `_ledger`（`model_step_service.py:1004-1026`，run 级 map）与 `inspect_tool_execution`（`tool_execution.py:975-999`）。时序保障：ledger 先于消息落库（`_settle_outcome` 先 mark 后 node update），结算后立即读 ledger 无竞态。
