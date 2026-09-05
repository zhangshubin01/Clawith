# 生产级修复方案：read-dedup 与空转护栏（deepagents 深度对比版）

- 状态：ready-for-agent
- 日期：2026-09-05
- 复盘对象：run `14ba5535-7d34-440e-a7e7-a11fbfb5a918`（Android 工程师 07，413 调用 83% read_file，~226 次重读内容未变，run_cancelled）
- 取代/修订：`20260905-read-dedup-stall-guard.md`（原 spec 的「新建 files channel 清单」与「新建空转熔断」两条路线按本文收缩）
- 关联先例：`docs/adr/0015-compaction-progress-anchor.md`（已落地 `loop_detection` + `completed_actions`）

---

## 0. 摘要

根因已坐实（read 结果进易失消息历史 + 摘要式压缩丢正文 + 弱工作记忆模型 → 压缩后重读 → 再膨胀）。本方案的定位修正为：

1. **不新建 `files` channel、不新建独立熔断、不新建 checkpoint 状态**——Clawith 的 `_prepare_messages` 循环护栏族（`_trailing_config_failure_loop`/`_trailing_identical_calls`/`_soft_loop_reminder`）、`loop_detection`（压缩失忆熔断）与 `completed_actions`（压缩注入）均已实现且带测试，读侧护栏应**并入**它们，而非另起炉灶。
2. **真正的新增只有「模型侧 read 去重」**，落点从旧栈的 `services/llm/context_compressor.py` 移到运行时正确 seam（`_prepare_messages` 的 prepared 消息处）；seen-count 从 `complete_once:2831` 已加载的 `executions` 现算，复用已算好的 `content_hash`，零新增状态。
3. 分三阶段、每阶段有测量 gate：P0 止血（周期内去重）→ P1 读状态注入压缩（跨周期记忆）→ P2 重复读占比信号并入现有循环护栏。

---

## 1. 与 deepagents 深度对比（逐轴，源码级）

| 轴 | deepagents（`libs/deepagents/deepagents/`） | Clawith 现状 | 迁移判断 |
|---|---|---|---|
| 文件状态持久化 | `backends/state.py` `StateBackend`：文件**正文**入 graph state `files` channel，dict-merge reducer（`None` 为删除标记），每步自动 checkpoint，thread 内持久、跨 thread 不持久 | 文件正文在 StorageBackend（本地/S3），整文件 hash `content_hash_bytes`（`storage_runtime/base.py:144`）；graph state 无文件状态 | **不抄**。deepagents 是「沙箱式虚拟 FS 入 state」，Clawith 多租户大仓会把正文塞满 checkpoint。可迁移命题仅「读状态元数据可入 checkpointed state」——而本方案进一步决定连元数据也不入 state（从账本现算） |
| messages channel | `graph.py:76` `DeltaChannel(_messages_delta_reducer, snapshot_frequency=50)` → checkpoint 增长 O(N²)→O(N) | `state.py:149` 裸 `add_messages` | **值得抄**（若新增增长型 channel 必须防膨胀；本方案用「从账本现算、零新增状态」彻底规避） |
| 压缩 | `middleware/summarization.py`：LLM 摘要 + **完整历史 offload 到 `/conversation_history/{session_id}.md` 供后续检索** | `run_compactor.py`：摘要≤8K + recent≤8K + `completed_actions`（确定性写侧事实，`build_completed_actions:573`，payload `_payload:493`）+ `RemoveMessage` 全清 | **半抄**。Clawith 已有「确定性事实注入」；差距=读侧未注入、正文直接丢（lossy）。P1 补读侧注入 |
| 读去重 | **无**（全仓无 read dedup） | 死代码 `_dedup_file_tool_results`（`context_compressor.py:948`，错误栈，见 §2） | **无先例可抄**，Clawith 自造；死代码当算法参考 |
| 空转/循环熔断 | **无**（仅 `recursion_limit=9999` + summarization 兜底） | 三层已有：`_prepare_messages` 循环护栏族（`_trailing_config_failure_loop:2332`/`_trailing_identical_calls:2380`/`_soft_loop_reminder:2431`）+ `loop_detection`（`_advance_loop_detection:392` + 终止 `node_executor.py:773`） | **Clawith 已领先**；P2 只给现有护栏加「交错重读」一个维度，不新建 |
| 超大工具结果 | `middleware/_message_eviction.py`：offload 落盘 + head/tail preview + 引导 offset/limit 分段读 | `config.py:246` `AGENT_RUNTIME_TOOL_RESULT_INLINE_MAX_BYTES=8192` + `summary_truncated` + `archived_body`/`result_ref` | **已有**，P0 软占位复用此机制 |
| 前缀缓存 | AnthropicPromptCachingMiddleware（cache_control 断点） | DeepSeek 前缀缓存（字节稳定前缀纪律） | **Out of Scope**（独立票）；注意 P0 注入占位会改前缀，见 §3 风险 |
| 轮数物理上限 | `recursion_limit=9999` | `model_turn_limit`（`agent.max_tool_rounds` 默认 10000，`agent.py:118`） | 等价，均维持不变 |

**结论**：deepagents 最优先借鉴的是①`DeltaChannel` 的 checkpoint 膨胀控制（本方案用「从账本现算」规避）、②压缩时「事实注入/offload」范式、③`_message_eviction` 的「软占位 head/tail + 归档指针」；**不是** `files` channel（那是虚拟 FS）。deepagents 无读去重、无熔断——这两点 Clawith 要么已有（熔断，且是三层）、要么需自造（读去重）。

---

## 2. 架构定位修正（决定 seam，此前方案未抓到的三点）

1. **死代码在错误栈**。run 14ba5535 走运行时栈：
   `node_executor._model`（`node_executor.py:736`）→ `model_step_service.complete_once`（`2825`）→ `_prepare_messages`（`2309`）→ `single_step.complete_llm_once` → `services/llm/client.py`。
   而 `_dedup_file_tool_results` 在 `services/llm/context_compressor.py`（旧 `ContextCompressor` 栈，被 ACP/experience 用，调用入度 in=0 死代码），**不在这条路径上**。它操作的是 `_cached_tokens`/`dynamic_content` 等旧消息模型，与运行时 `LLMMessage` 不同——只能当算法参考，不能接线。
2. **正确 seam = `_prepare_messages`（`model_step_service.py:2309`）**：它已经用 `runtime_messages_as_json(state)` + `ledger` 做循环分析（`_trailing_config_failure_loop`/`_trailing_identical_calls`/`_soft_loop_reminder`），且 `complete_once` 第 2831 行已加载 `ledger, executions`（`executions` 含 `result_metadata.content_hash`）。去重与空转判定都该在这里做「模型侧最后一次整形」，与现有护栏同源。
3. **seen-count 从账本现算，不新建状态**：`content_hash` 已在 `normalize_tool_outcome`（`tool_execution.py:649`，sha256(summary)）算好、随账本 `agent_tool_executions.result_metadata` 持久；`complete_once` 已加载 `executions`。去重的 seen-count 直接从 `executions` 数「同 (path, content_hash) 的历史执行数」，与 `build_completed_actions` 同读法，**无需 `lifecycle.read_dedup_seen` 新字段**。

**可复用件（本方案复用，不新建）**：`content_hash`（段级，`tool_execution.py:649`）、`content_hash_bytes`（整文件，`storage_runtime/base.py:144`）、`executions` 账本（`complete_once:2831` 已加载，seen-count 现算用）、`_prepare_messages` 循环护栏族 `_trailing_config_failure_loop`/`_trailing_identical_calls`/`_soft_loop_reminder`（`model_step_service.py:2332-2436`，P2 复用）、`summary_truncated`/`archived_body`/`result_ref`（软占位复用，`tool_execution.py:645-654`）、`loop_detection`（压缩失忆熔断）、`build_completed_actions`/`_payload`（压缩注入）、`ledger_from_executions`（账本读取）、`_audit_breaker_event`（可观测）、`agent.max_tool_rounds`（per-agent 配置接线范式）。

---

## 3. 生产级方案（三阶段，逐段有测量 gate）

### P0 止血：模型侧 read 去重（核心新增，无阻塞）

**做什么**：同一 `(path, content_hash)` 段的 read_file 结果，在当前压缩周期内累计读取 ≥N=3 次后，第 4 次起不再把重复全文喂模型，改返回**软占位**：`「📄 {path} — 内容未变（已读 N 次）；完整内容已归档，可用 read_file 强制重读」` + 头部/尾部预览（head/tail preview）。

- **判定层**：新增纯函数模块 `ReadDedupDecider`（零副作用）：
  输入 `(tool_name, path, content_hash, seen_count, config)` → 输出 `(放行全文 | 软占位)`。
  仅对 `tool_name == "read_file"` 且 `content_hash is not None`（即 succeeded 且 summary 非空）生效；失败读（`summary=None`）自然不入计数。
- **seen-count 数据源 = 账本，不新建状态**：`complete_once` 第 2831 行已加载 `ledger, executions`，seen-count 直接从 `executions` 现算（同 `build_completed_actions` 的读法）。**不引入 `lifecycle.read_dedup_seen` 新字段**，零新增 checkpoint 状态。
- **接线点**：`_prepare_messages`（`model_step_service.py:2309`）构建 `prepared` 消息处做去重整形。替换时**保留 `tool_call_id`**，保证仍是合法 tool result；软占位复用既有 `summary_truncated`/`archived_body`/`result_ref` 机制（`tool_execution.py:645-654`）给归档指针 + head/tail preview（deepagents `_message_eviction` 同构）。
- **周期内语义**：去重计数**随压缩重置**（压缩 = 上下文清空 = 模型「没读过」）。跨压缩周期的「已读未变」记忆由 P1 的 `files_read` 注入承担，P0 不管跨周期——避免「压缩后模型既无正文又拿占位」的藏内容 bug。
- **不误伤保证**：段级 hash 天然区分「读不同段」与「重复读同段」；写文件 → 段 hash 变 → 自动放行「写后校验」；`offset`/`limit` 变化导致段头变 → hash 变 → 宁漏勿错放行。

**测量 gate**：回放 run 14ba5535（dry-run 或等价样本），`read_file` 调用数 413 → 目标 <150，输入 token 下降 ≥40%；确认 `activity_main.xml`（94 读/60hash，真在改）不被误拦。

### P1：读状态注入压缩 payload（阻塞于 P0 完成，或与 P0 并行）

**做什么**：压缩 payload 增加「已读且未变」事实，让压缩后的模型「看到即知读过、没变」，从结构上失去重读理由。**P1 是唯一负责跨压缩周期记忆的层**（P0 只做周期内去重）。

- 扩展 `run_compactor._payload`（`run_compactor.py:493`）：新增 `files_read` 键（或并入 `completed_actions` 同构），内容 = 从账本聚合的 `{path, 段hash, 整文件hash, 最后读 step}`，ADD-only、字节预算裁剪（复用 `build_completed_actions` 的 50 条/2048B 模式）。
- 摘要指令加一条：「以下文件已读取且内容未变，无需重读」。
- **降级**：清单缺失/失效 → 退回「无提示」（即现行为），不 crash、不产生死引用。

**测量 gate**：压缩后首个 model step 的 read_file 占比下降；「压缩→重读→再膨胀」循环（step115 28.3K→116 9.6K→119 26.7K）不再复现。

### P2：重复读占比信号并入现有循环护栏（阻塞于 P1）

**做什么**：把「窗口内**交错**重复读占比」作为**新维度**加进 `_prepare_messages` 里已有的循环护栏族（`_trailing_config_failure_loop`/`_trailing_identical_calls`/`_soft_loop_reminder`，`model_step_service.py:2332-2436`），不新建熔断。

- 现况：`_trailing_identical_calls` 只抓**连续**相同工具+参数；本 run 是**交错**重读（读 A、读 B、读 C、又读 A），从它眼皮下溜走——这就是要补的维度。
- 新增 `dup_read_ratio`：滑动窗口 W=20 内 `read_file 且 (path, content_hash) 已见` 的占比，数据源 = `runtime_messages_as_json(state)` + `ledger`（与现有护栏同源）。
- 分级收敛**复用现有原语**：**提醒**（复用 `_soft_loop_reminder` 的「纯 prompt 追加」机制）→ **强制 compact**（`_schedule_compact`）→ **终止**（复用 `_trailing_identical_calls` 的 `tool_success_loop` 终止路径）。默认只提醒，终止可开关，同 ADR-0015 决策 3。
- 信号天然区分「长任务推进」（读新段/新文件，hash 首现）与「原地空转」（反复读同批未变段）。

**测量 gate**：长任务（多文件大重构）零误报；空转 run 在 ~50 步内自行收敛而非等人工取消。

### 配置与租户可调

- agent 级 settings 新增 `read_dedup_n`（默认 3）、`stall_window`（默认 20）、`stall_ratio`（默认 0.7），沿用 `max_tool_rounds` 的 per-agent 配置 + API/SettingsTab 接线模式。
- 阈值不改 `max_tool_rounds`/`model_turn_limit`（默认 10000 维持，长任务物理兜底）。

### 可观测性

- 去重计数（每步去重条数/累计）、熔断判定结果、收敛动作，写 audit event（复用 `_audit_breaker_event`）+ 账本 metadata，事后复盘可查（对应 User Story 11）。

### 回滚与开关

- feature flag（租户/agent 级）：read-dedup 默认开、可关；空转熔断默认只提醒。
- 改动均为 additive（payload 加键、prepared 消息整形、护栏族加维度），**零新增 checkpoint 状态**，旧 checkpoint 向后兼容。

---

## 4. 关键设计决策与阈值依据

- **N=3**：本 run `MainActivity.kt` 44 读/1hash，N=3 挡掉 ~93% 重复读，收益近饱和；再收紧（N=2）只多 ~2% 却翻倍误伤风险。
- **W=20 / 占比 0.7**：本 run ~55% 调用是「重复读未变」，窗口占比 0.7 可稳抓且不误伤正常读取；理解阶段读新段不触发。
- **周期内去重 vs 跨周期记忆分离**：去重 = 「你当前上下文见过这段」，随压缩重置；跨周期 = P1 注入。二者不可混（混则「压缩后既无正文又拿占位」藏内容）。
- **软占位而非裸占位**：占位带 head/tail + 归档指针（复用 `archived_body`/`result_ref`），模型需要时可自取；裸「内容未变」会在模型真需正文时造成凭空犯错。
- **两种 hash 语义严格分离**：段 hash（`content_hash`）只用于去重；整文件 hash（`content_hash_bytes`）只用于清单/写后校验。混用会致「改了别处、这段没变」被误判为「文件已变」放行重复读。
- **零新增 checkpoint 状态**：seen-count 从账本现算，回应 deepagents `DeltaChannel` 的膨胀教训，也回应 `prune_runtime_checkpoints` 的 blob GC 约束。

## 5. 测试策略（行为级，只测外部行为）

- 重复读同段 → 返回软占位；读不同段 → 返回全文；写后重读改动段 → 放行、未改动段 → 仍去重；窗口内空转 → 产生收敛信号。
- **关键不误伤回归**：大文件 offset 递进分段读（0-2000/2001-4000）不去重；`limit` 变化或总行数变化的段头变化方向「宁漏勿错」；短文件（<1000 字）行为明确；**压缩后首读放行（周期内语义）**；多租户/多 run 隔离（去重 per-run、不跨租户污染——回应死代码注释里的 Blocker #7）。
- 复用现有组织方式：`ReadDedupDecider` 纯函数单测 + 两个接线点集成测试（沿用 `test_agent_runtime_compaction_loop.py` / `test_agent_runtime_completed_actions.py` 风格与 fixtures）。

## 6. 与宪法五原则对齐

- **Evidence Before Claims**：根因数据来自账本聚合查询 + 源码核实；阈值来自实测 hash 分布。
- **Minimal Scoped Changes**：不新建 channel/熔断/offload 生命周期/checkpoint 状态；复用 `loop_detection`、`completed_actions`、`content_hash`、`executions` 账本、`_prepare_messages` 循环护栏族。
- **Contract and State Ownership**：零新增状态字段；不碰 `messages`/`thread_summary` 契约；去重/熔断只读账本与消息流，不写。
- **Tests Prove Behavior**：每个护栏行为级测试 + 不误伤回归。
- **Preserve Existing Work**：复用 ADR-0015 已落地的 `loop_detection` + `completed_actions` + `_prepare_messages` 循环护栏族，读侧作为其补全而非替代。

## 7. 改动清单（blast radius）

| 阶段 | 文件 | 改动 |
|---|---|---|
| P0 | `backend/app/services/agent_runtime/read_dedup.py`（新） | `ReadDedupDecider` 纯函数 |
| P0 | `model_step_service.py`（`_prepare_messages` prepared 处） | 去重整形接线（seen-count 从 `executions` 现算） |
| P0 | `config.py` + `agent.py` + `schemas.py` + `api/agents.py` + `SettingsTab.tsx` | `read_dedup_n` 阈值配置接线 |
| P1 | `run_compactor.py`（`_payload` + 摘要指令） | `files_read` 注入 |
| P2 | `model_step_service.py`（`_prepare_messages` 循环护栏族） | `dup_read_ratio` 维度（复用 `_soft_loop_reminder`/`_trailing_identical_calls`） |
| 全部 | 各阶段测试文件 | 行为级测试 |

## 8. Out of Scope

- 前缀缓存命中率优化（DeepSeek prefix cache）——独立票。注意：P0 注入占位会改动消息前缀，需在测量 gate 观察 cache_read 影响，必要时把占位注入放到缓存断点之后（先例：deepagents MemoryMiddleware「cache_control 断点放在 memory 之后」的同构处理）。
- 换模型 / 加 `context_window_tokens` 配置——不在本护栏范围。
- 调整 `max_tool_rounds`/`model_turn_limit` 默认值或 clamp 上限——维持现状。
