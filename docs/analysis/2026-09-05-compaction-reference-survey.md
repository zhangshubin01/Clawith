# Compaction / 上下文管理 参考项目对照调研 —— 支撑 run 764eb591「失忆」事故

- **关联报告**：`2026-09-05-compaction-amnesia-764eb591.md`（五根因链 + 修复建议）
- **调研范围**：18 个项目，覆盖 T0（langchain 栈本体）、T1（OpenHands/SWE-agent/codex/gemini-cli/letta-code…）、T2/T3（jcode/herdr/orca/mem0/gptme/claw-code/cline/opencode…）三个分层，全部实读本地源码或已有整库研究报告，非目录级浏览。
- **口径**：每个项目按事故五根因逐条对照——①write 类工具被 read 洪水挤出摘要窗口；②摘要漂移自相矛盾；③模型信摘要→自我怀疑→重做；④循环检测只靠精确 prefix 重复、抓不到漂移循环；⑤编辑数据被低层回退（消息状态与文件状态两层分裂）。
- **方法**：5 个只读子代理并行实读 `UGit/` 本地镜像源码 + `docs/technical-plans/` 已有研究报告；本文件是汇总合成，逐条 file:line 以各子代理实读为准。

---

## 一、TL;DR（结论先行）

1. **治根因①（write 被挤出）最佳范式 = cline 的「确定性 Files 账本」**：`extractFileOps`/`ensureFilesSection` 把「已改文件」做成**行号级、结构化、永不进 LLM 自由文本**的事实节；claw-code 的「每 tool 一行 timeline」、jcode 的「应急确定性提取（tool 名 + 文件清单）」、mem0 的 procedural「Action Result 逐字保留」是同一思路的变体。Clawith 的 `completed_actions` 50 条/2048 字节纯字节截断是唯一把 write 事实交给「和 read 洪水同池竞争」的实现。
2. **治根因②（摘要漂移自相矛盾）最佳范式 = opencode 的「冲突时 conversation 胜 + 未提及也 carry forward」+ gemini-cli 的「anchor + Probe 二次自校验」+ cline 的「冻结标记 + 估计器安全阀」（代码级不变量，而非仅提示词）**。mem0 的 ADD/UPDATE/DELETE/NONE 冲突消解、claw-code 的「无 LLM → 结构性免漂移」是更彻底的两极。
3. **治根因③（信坏摘要→重做）最佳范式 = gemini-cli「摘要失败/反而变大就回退原文」+ SWE-agent「超限即硬退出 fail-closed」+ letta「compaction is summarization, not loss，原文可 recall 检索」**。三者共同点：**绝不把「失真的摘要」当作唯一事实源喂回推理循环**。
4. **治根因④（漂移循环）—— 18 个项目里没有任何一个解决了**。gemini-cli 最接近（prefix + cycle(k=1..5) + LLM 语义等价），orca 的 `payloadFingerprint` 内容指纹 + `runtimeFence` 代际可作「语义去重 + 代际」两维扩展，herdr 的「权威 hook + fallback 屏幕」双源仲裁可作范式。**这是 Clawith 独有课题，值得作为差异化能力自研。**
5. **治根因⑤（编辑数据回退）最佳范式 = orca 的 `runtimeFence` 单调代际（写前校验、过期写入拒绝）+ herdr 的「结构态分离 + 版本化 + 原子写 + 未来版本拒绝」+ letta 的 MemFS git 化 + gptme 的「master context 单层 append-only + byte-range 引用」**。共同指向：**编辑数据必须有「单一权威 + 单调代际/版本化 + 原子写」，与消息流（高 churn 态）解耦**——这正是 Clawith 根因⑤缺失的。

---

## 二、五根因 × 最佳范式 对照表

| 根因 | 最佳范式（项目：机制） | 对 Clawith 的直接含义 |
|---|---|---|
| ① write 被 read 洪水挤出 | cline：确定性 Files 账本（行号级）；claw-code：每 tool 一行 timeline；jcode：应急确定性提取 | `completed_actions` 改「write 类永不淘汰 + 结构化账本」，不再和 read 同池抢字节 |
| ② 摘要漂移自相矛盾 | opencode：冲突时 conversation 胜；gemini-cli：anchor+Probe；cline：冻结标记+安全阀；mem0：ADD/UPDATE/DELETE | 摘要 prompt 加「合并规则」+ 应用前过期校验；漂移要代码级挡，不能靠提示词自觉 |
| ③ 信坏摘要→重做 | gemini-cli：失败/膨胀即回退原文；SWE-agent：超限 hard exit；letta：摘要不保真、原文可 recall | 摘要质量无法保证时 fail-closed，别把失真摘要当唯一事实喂回循环 |
| ④ 漂移循环检测 | gemini-cli：prefix+cycle+语义等价；orca：内容指纹+代际；herdr：权威+fallback 双源 | `detect_loop` 从精确 prefix 扩展到「内容指纹 + 语义等价 + 重做已编辑文件」软信号 |
| ⑤ 编辑数据回退 | orca：runtimeFence 单调代际；herdr：结构态分离+版本化+原子写；letta：MemFS git 化；gptme：master context 单层 | 编辑数据独立「单调代际 + 版本化 + 原子写」单一权威，与消息流解耦 |

---

## 三、逐项目卡片

### 3.1 codex（OpenAI，Rust 重写）`UGit/codex`
- 位置：`codex-rs/core/src/compact.rs`（`build_compacted_history` L670-760）、`prompts/templates/compact/prompt.md`、`session/context_window.rs:104-109`
- 触发：纯 token 阈值（`auto_compact_scope_tokens >= buffer` 或触达全窗硬上限）；手动 `/compact`
- 模板：**自由文本、无固定 section**；摘要 = 最后一条 assistant 消息 + `SUMMARY_PREFIX`
- write 进摘要：**无任何确定性保留**，只保留用户消息（`COMPACT_USER_MESSAGE_MAX_TOKENS=20000` 截断）；工具输出全随摘要丢弃
- 防漂移：仅 `summary_prefix` 软性指令；`insert_initial_context_before_last_real_user_or_summary`（L612）压缩后把 canonical initial context 精确重注入
- 循环：无检测、无上限；`turn.rs:516` 注释「只要压缩压得下去就不必担心循环」——结构性盲区
- 状态同源：否（消息 session vs 真实沙箱文件系统两层）
- **可迁移**：`InitialContextInjection` 压缩后重注入不变上下文锚点（Clawith 系统提示词/约束随压缩丢失可借鉴）

### 3.2 opencode（TS）`UGit/opencode`
- 位置：`packages/core/src/session/compaction.ts`（`SUMMARY_TEMPLATE` L16-46、`SUMMARY_UPDATE_INSTRUCTIONS` L47-55）、`snapshot.ts`
- 触发：token 阈值（`buffer=20000`、`keep.tokens=8000`、摘要输出 `4096`、工具输出截断 `TOOL_OUTPUT_MAX_CHARS=2000`）
- 模板：**固定 section 强约束** `Objective / Important Details / Work State(Completed|Active|Blocked) / Next Move / Relevant Files`，强制「Keep every section, even when empty」+ `(none)`
- write 进摘要：确定性截断——assistant 展开为 `[Tool call]` + `[Tool result]: truncate(...)`，工具结果截 2000 字符加 `[truncated]`
- 防漂移：**最强之一**——`SUMMARY_UPDATE_INSTRUCTIONS`：①「未再提及也要 carry forward，只丢已完成不再需要的」②「**conversation 与 prior-summary 冲突时 conversation 胜**，陈述纠正后事实并删旧 claim」
- 循环：硬步数上限（`MAX_STEPS_PROMPT` 禁用工具强制收尾），无语义循环检测
- 状态同源：否；但 `snapshot.ts` 用**独立 git 仓库做 content-addressed 文件快照**，step 起止捕获、`computeDiff` 得每步文件 diff——独立于对话的「真实文件改动」第二事实源
- **可迁移**：冲突时 conversation 胜（治②）；content-addressed 文件快照作交叉验证（治⑤）

### 3.3 cline（TS）`UGit/cline`
- 位置：`sdk/packages/core/src/extensions/context/compaction.ts`（触发 L256-641）、`basic-compaction.ts`、`agentic-compaction.ts`、`compaction-shared.ts`
- 触发：`COMPACTION_TRIGGER_RATIO=0.9`；目标 `0.7`（长对话 0.5）；`preserveRecentTokens=20000`；`overflowRecovery`（provider 已拒超窗 → 强制确定性 basic，不依赖再次 LLM 成功）
- 模板：双策略——agentic（LLM）固定 section `Goal/State/Highlights/Next/Files`；basic 纯确定性折叠，无 LLM
- write 进摘要：**行号级确定性账本，最严谨**——`extractFileOps`（L412-449）扫 tool_use 块取 read/modified 路径；`extractDiffLineRange`（L508-542）取 diff 行号写 `path:start-end`；`ensureFilesSection`（L647-655）检测 LLM 摘要缺 `## Files` 就补确定性节；basic 的 `summarizeToolActivity` 产出 `Files read/edited + Commands ran` 三元 ledger
- 防漂移：**代码级不变量**——①冻结标记 `COMPACTION_PRESERVED_MARKER`（已幸存的非 typed 消息永不重折叠）②估计器漂移安全阀（防 target 越压越小）③`isSafeCutBoundary` 保证 tool_use/tool_result 不被劈成两半
- 循环：任务级 `maxIterations`（SQLite 持久化），context 层无语义检测
- 状态同源：否（transcript SQLite vs opt-in git checkpoint 仅回滚）
- **可迁移**：确定性 Files 账本（治①，直接解药）；冻结标记+安全阀（治②）；overflow 强制确定性收敛（治③）

### 3.4 OpenHands（已迁 agentic core 到 software-agent-sdk）`UGit/OpenHands`
- 位置：V0 冷凝器历史 `openhands/memory/condenser/impl/structured_summary_condenser.py`、`llm_summarizing_condenser.py`（2026-04-24 迁出，读 git 历史 `7498353ed^`）
- 触发：**按事件条数**（`max_size=100`，`keep_first=1`），非 token 阈值；每条被遗忘事件先 `truncate_content(max_chars=10000)`
- 模板：`StructuredSummaryCondenser` 用 **`StateSummary` pydantic schema 强制 function-calling**，required=`user_context/completed_tasks/pending_tasks`，字段含 `files_modified/commits_made/branch_*` 等
- write 进摘要：无专门 ledger，`str(forgotten_event)` 截 10k 合并进 prompt，靠 LLM 把「已改文件」写进 schema 字段——**与 read 洪水同池竞争**（根因①形态）
- 防漂移：旧摘要 `<PREVIOUS SUMMARY>` 滚动回喂 + schema 强制分节；无硬性防自相矛盾
- 循环：`StuckDetector` 5 场景**全是精确匹配**（4 相同 action+obs / 独白 / 模式重复 / context-window 错误循环），无语义/漂移级
- 状态同源：否（State.history ≠ View ≠ 沙箱文件系统三层）；但 ground-truth history 不被破坏，只遗忘「给模型看的视图」
- **可迁移**：结构化强制摘要（required 字段保写事实，治①）；反例证明「靠提示词防漂移不够」（治②需更硬门禁）

### 3.5 SWE-agent `UGit/SWE-agent`
- 位置：`sweagent/agent/history_processors.py`（`LastNObservations`、`ClosedWindowHistoryProcessor`、`RemoveRegex`）；应用点 `agents.py:540-548`
- 触发：确定性每步恒等（`LastNObservations` 只留最近 n 条）；真超限 `ContextWindowExceededError → exit_context` **硬退出，不摘要**
- 模板：**不是摘要**——被省略 observation 替换为字面量 `"Old environment output: (N lines omitted)"`，零语义保留
- write 进摘要：整体丢弃，只留行数占位符（根因①最坏形态，但「丢弃」≠「改写」，故无漂移）
- 防漂移：不适用（无摘要即无漂移）；`messages()` 纯视图变换，`self.history`/trajectory/文件系统三重真相永不销毁
- 循环：`max_requeries=3`（format/blocklist/bash 语法错误重试上限）；主循环无 max_steps
- 状态同源：否，但省略是纯视图变换，**ground truth 单层只增不改**——模型随时重读文件系统自纠
- **可迁移**：超限即 hard exit 而非盲目摘要（治③ fail-closed）；视图变换不污染真相源（治⑤的干净解法示范）

### 3.6 gemini-cli `UGit/gemini-cli`
- 位置：`packages/core/src/context/chatCompressionService.ts:239`、`services/loopDetectionService.ts`
- 触发：`DEFAULT_COMPRESSION_TOKEN_THRESHOLD=0.5`，保留 `0.3`，函数响应独立预算 `50000`
- 模板：`<state_snapshot>` 结构化快照；**anchor 指令**（历史已有旧快照 → 强制把旧快照仍相关的约束/知识集成进新快照）
- write 进摘要：无 write ledger；tool result 走「反向 token 预算」，超预算的旧大输出截成「最后 30 行 + 存临时文件 + placeholder 引回」
- 防漂移：三层——①anchor 强制新旧融合 ②**Probe 二次自校验**（批判性检查是否漏文件路径/工具结果/约束，给 FINAL）③失败/膨胀护栏（`hasFailedCompressionAttempt` 降级纯截断、`newTokenCount > originalTokenCount` 拒绝、邻接不变量校验失败回退原 history）
- 循环：**双层**——精确前缀/哈希 cycle（k=1..5）+ **LLM 语义等价判断**（「换参数/不同报错=调试进展非循环」）；事件 `ServerGeminiLoopDetectedEvent`
- 状态同源：否（第二层 `compression_state.json` 仅缓存非权威）
- **可迁移**：anchor+Probe（治②③）；失败降级/膨胀拒绝（治③）；语义等价循环检测（治④，最接近漂移循环的现成思路）

### 3.7 gptme `UGit/gptme`
- 位置：`gptme/util/reduce.py:41`、`gptme/tools/autocompact/engine.py:28`、`decision.py:110`
- 触发：token 阈值为主（`0.9*context`），`MIN_SAVINGS_RATIO=0.10`（收益 <10% 不压缩）
- 模板：规则压缩无摘要（master context 引用）；LLM resume 4 段结构化 `Conversation Summary/Technical Context/Current State/Context Files`，Context Files 段回填真实文件内容
- write 进摘要：超大 tool result 生成短摘要 + **追加 master context byte-range 引用**（被截内容在 `conversation.jsonl` 可精确字节恢复）；抽取式压缩始终保留 code block
- 防漂移：**Master Context 架构**——`conversation.jsonl` append-only 永不压缩的权威源，压缩只是「视图/引用」，摘要失真时可精确回读原文
- 循环：`GPTME_MAX_STEPS`（默认无上限）；无语义循环检测
- 状态同源：**是，单层权威**（append-only jsonl；压缩产物是内存视图）
- **可迁移**：master context 单层 append-only + byte-range 引用（治⑤最直接）；`_drop_orphaned_tool_pairs` 保 tool 对原子性（治①⑤）；MIN_SAVINGS_RATIO 收益门槛（治②轻量手段）

### 3.8 claw-code（Rust，claw 生态表亲）`UGit/claw-code`
- 位置：`rust/crates/runtime/src/compact.rs:96`（`compact_session`）、`summary_compression.rs:37`
- 触发：累计 input token 阈值 `100000`；`max_estimated_tokens=10000`、`preserve_recent_messages=4`
- 模板：固定 section `Scope/Tools mentioned/Recent user requests/Pending work/Key files/Current work/Key timeline`；**纯确定性规则生成，无 LLM 摘要**
- write 进摘要：`summarize_block` 逐块转一行——`tool_result {name}: {output}`（错误加 `error` 前缀）、`tool_use {name}({input})`，统一截 160 字符——**每条 tool 调用/结果进 Key timeline 成为一行 ledger 事实**
- 防漂移：无 LLM → 结构性免漂移；`merge_compact_summaries` 显式**扁平化合并**（不重嵌套）；`summary_compression.rs` 去重（`dedupe_key` 小写化）+ `is_core_detail` 保护核心行
- 循环：`max_iterations`（默认 `usize::MAX`）、`max_retries=8`；无语义循环检测
- 状态同源：是，单层（`Session.messages` 唯一状态，压缩就地替换）
- **可迁移**：确定性规则摘要 + 每 tool 一行 ledger（治②③最彻底，代价是语义密度低）；扁平化合并 + 核心行优先级 + 去重（治②）

### 3.9 mem0 `UGit/mem0`
- 位置：`mem0/memory/main.py`（`add` L760、`_create_procedural_memory` L1993）、`configs/prompts.py`（`ADDITIVE_EXTRACTION_PROMPT`、`DEFAULT_UPDATE_MEMORY_PROMPT`）
- 触发：被动存储（`add(messages)`），无 token 阈值/窗口自驱
- 模板：事实型固定 JSON schema（15-80 词 self-contained，独立 embedding）；procedural 固定模板（Overview + 编号步骤，`Action Result (Mandatory, Unmodified)` **逐字保留禁 paraphrase**）
- write 进摘要：抽取式（LLM 拆原子事实）；原始消息 `save_messages` evict 只留 10 条
- 防漂移：**强**——①`DEFAULT_UPDATE_MEMORY_PROMPT` 四操作 ADD/UPDATE/DELETE/NONE，含「contradicts → DELETE」②抽取层 dedup 指令（Recently Extracted 首要去重参照、No Within-Response Duplication、No Echo Extraction）③代码层 `hashlib.md5(text)` 去重 + UUID→整数映射防幻觉 ID ④`history` 表记录变更流水（old/new/event）
- 循环：无 agent 循环（`chat()` 直接 NotImplementedError）
- 状态同源：否，三层独立（向量库 / history 流水 / messages）
- **可迁移**：原子事实 + md5 去重 + ADD/UPDATE/DELETE 冲突消解 + 变更 history 流水（治②⑤整组迁移）；procedural「Action Result 逐字 + 固定编号步骤」（治①）；无循环检测（根因④需自建，可借 `linked_memory_ids` 矛盾链接做漂移信号）

### 3.10 jcode `UGit/jcode`（研究报告 `docs/technical-plans/20260905-jcode-study.md`）
- 位置：`crates/jcode-compaction-core/src/lib.rs`（阈值 L6-63）、`crates/jcode-base/src/compaction.rs:932-1027`
- 触发：**三级阈值**——软 `0.80`（后台异步，等 15s 不到就 abort）、硬 `0.95`（同步硬压缩）、手动下限 `0.10`；Proactive EWMA 投影 15 轮提前触发；Semantic embedding 主题漂移（`topic_shift_threshold=0.45`）
- 模板：固定 4 段 `Context/What we did/Current state/User preferences`，结尾提示「可之后搜全文拿精确报错/代码」
- write 进摘要：正常摘要靠 prompt「files changed」；**应急硬压缩走确定性启发式**——`build_emergency_summary_text` + `collect_emergency_summary_hints` 从被丢弃消息里**确定性提取** `Tools used:` + `Files referenced:`（去重截 30 个）；`safe_compaction_cutoff` 保证后缀里每个 ToolResult 都有对应 ToolUse
- 防漂移：①`ActiveCharEstimate` 值+脏标志捆绑（类型杜绝账目静默漂移）②**过期后台摘要丢弃**（应用前校验 `pending_cutoff` 会吞健康尾部则弃）③`anti_signals_block` 五重反信号防压缩风暴
- 循环：无（报告 §6.1 明确 jcode 无循环检测）
- 状态同源：单进程单层 transcript（无文件系统层）
- **可迁移**：三级阈值分层触发 + 应急确定性提取（治①兜底）；值+脏标志捆绑 + 应用前过期校验（治②，对应 Clawith「部署杀→重放→分叉撞账本」教训）

### 3.11 letta-code `UGit/letta-code`（研究报告 `docs/technical-plans/20260903-letta-code-study.md`）
- 位置：`src/agent/prompts/letta_no_memfs.md:27`、`src/agent/memory-constraints.ts`、`src/agent/max-context.ts:1`（`MIN_CONTEXT_WINDOW_TOKENS=30000`）
- 触发：compaction 在 letta host 侧触发（client 只传 `compaction_settings`），本仓库不可见（诚实记录）
- 模板：本仓库无固定 section；核心原则「**compaction 是 summarization 不是 loss**——原文仍可 recall 搜出，你的记忆才是 what mattered 的 ground truth」
- write 进摘要：**不靠摘要保事实**——①全部消息历史 harness 自动存（agent 不可改写）经 recall 子代理 `vector+fts+hybrid` 检索 ②MemFS 记忆块只放「泛化规则 + 索引」不放事件 ③铁律「能从 recall 搜出或重读文件推导的，一律不写进记忆块」
- 防漂移：记忆块三原则（只放身份/行为规则+索引、`[[path]]` 突触、泛化非事件、过大触发 `/doctor`）+ 体积硬约束 `maxFileCharacters=20000`
- 循环：无（循环控制在 host 侧）
- 状态同源：三层记忆但每层同源；MemFS 用 **git 全套**（precommit/postcommit/sync-state/config-lock 防并发写）版本化
- **可迁移**：摘要不承担事实保真 + 原文可 recall（治③，根源消解「信摘要→重做」）；「已改文件」靠重读文件/git 推导而非写进摘要（治①）；MemFS git 化（治⑤，对应 Clawith workspace `.git` 物化方向）

### 3.12 herdr `UGit/herdr`（研究报告 `docs/technical-plans/20260905-herdr-study.md`）
- 位置：`src/persist.rs:3-5`（结构态 `session.json` / 高 churn 态 `session-history.json`）、`src/persist/snapshot.rs:447-456`、`src/persist/io.rs:48-61`
- 触发：无 LLM 压缩（终端 multiplexer）；持久化 = pane 结构变化写结构态、屏幕历史 append 高 churn 态
- 模板：N/A
- write 进摘要：N/A；但其「**结构态/高 churn 态分离**」是持久化范式——低频小（结构）与高频大（历史）分文件存，高频 churn 不污染结构态
- 防漂移：**版本化迁移 + 未来版本显式拒绝**（`SNAPSHOT_VERSION=3`，旧代码绝不静默错读新结构）+ 原子写（`.tmp` + rename）+ 双重来源仲裁（权威 hook 为主、屏幕匹配 fallback、模糊态保守 Unknown）
- 循环：无（非 LLM）
- 状态同源：两文件分离但同源（同一 persist 模块、同一版本化+原子写）；权威在文件（结构态），屏幕是 fallback 信号
- **可迁移**：结构态分离 + 版本化 + 原子写 + 未来版本拒绝（治⑤，编辑数据应作结构态单独立持久化）；权威+fallback 双源仲裁（治④，循环检测不再只靠精确 prefix）

### 3.13 orca `UGit/orca`（研究报告 `docs/technical-plans/20260905-orca-study.md`）
- 位置：`src/main/agent-hooks/server-ingest-normalization.ts`（spool 先持久化再消费）、`src/shared/agent-session-record.ts`（runtimeFence）、`agent-session-wire.ts:164-217`（四字段幂等 envelope）
- 触发：无 LLM 压缩（编排平面）；spool = hook 事件上报先落盘再消费；幂等 = 每次 mutation 写
- 模板：N/A
- write 进摘要：N/A；但其「**状态代际 + 四字段幂等 envelope**」是写动作防重复/防乱序/防过期机制——`sessionId+clientOperationId+expectedRuntimeFence+payloadFingerprint`
- 防漂移：`expectedRuntimeFence` 乐观并发（过期代际写入被拒）+ `payloadFingerprint` 内容去重 + HMAC claim + `deathEvidence` 防僵尸写 + `(seq,epoch)` 防重启 seq 归零撕裂
- 循环：无 LLM 循环，有进程级「楔死/超限」熔断（relay splice 十态机 + wedged 10s 强杀 + admission 高低水位）
- 状态同源：**最纯单层**——单写者租约 + 单调 fence，状态=台账（SQLite）+ journal 游标，全部走同一幂等 envelope
- **可迁移**：`runtimeFence` 单调代际（治⑤，编辑数据写前校验 fence 仍最新，过期/低层回退写入被拒）；`payloadFingerprint`+代际两维（治④，重复动作检测从「精确 prefix」扩为「内容指纹+代际」）；spool 先持久化再消费（治③，状态变更先落盘再应用）

### 3.14 langchain-ai 官方 notebook（context_engineering + how_to_fix_your_context）`UGit/context_engineering`、`UGit/how_to_fix_your_context`
- 位置：`3_compress_context.ipynb`、`05-context-summarization.ipynb`、`06-context-offloading.ipynb`
- 触发：任务完成后总结一次 / 每次 tool_call 都摘要；无 token 阈值
- 模板：自由文本，无固定 section、无「已完成动作」结构
- write 进摘要：tool result 全文喂 LLM 非确定性摘要；`06` 的 `WriteToScratchpad` 同写回显/字段/持久层三处（demo 级）
- 防漂移：仅 prompt 措辞「retain all essential / 100% essential」
- 循环：无
- 状态同源：摘要与 messages 同 state 但「同源脱节」（摘要 END 后才写、从不回喂推理）
- **可迁移**：反例即根因②③教科书版——自由文本摘要必失真，唯一「幸免」于③是因为摘要不参与推理（反面印证 Clawith 把摘要回喂推理是危险增强点）；`06-context-offloading.md` 引用两条对症手法（Anthropic「LeadResearcher 把计划写进 Memory 防截断」、Manus「写 todo.md 并重写**迫使 agent 复述目标到上下文末尾**」）

---

## 四、与上一轮（dsh/langchain/deepagents）合并后的完整动作清单

上一轮已实读 dsh（固定 8 section 模板 + 空节 (none) + 增量合并旧 checkpoint + 摘要截断=失败 + 裁完低于阈值跳过）、langchain（ARTIFACTS 节防 silent loss + 「don't repeat completed actions」+ `_find_safe_cutoff` 不劈 AI/Tool 对 + `+messages[cutoff:]` 保留全量 state）、deepagents（offload 到 history 文件 + `truncate_args` 确定性截断 + StateBackend 把 files 放进 graph state 的 `files` channel 单层持久）。**本轮 15 个项目合并后，五根因的完整可迁移动作如下：**

1. **根因①（write 被挤出）**：dsh 空节 (none) 模板 + cline 确定性 Files 账本（行号级）+ claw-code 每 tool 一行 timeline + jcode 应急确定性提取 + mem0 procedural「Action Result 逐字」——组合 = **completed_actions 改结构化账本（write 类永不淘汰 + 按类别分层预算 + 行号级路径）**。
2. **根因②（漂移自相矛盾）**：opencode「冲突时 conversation 胜 + carry forward」+ gemini-cli anchor+Probe + cline 冻结标记/安全阀 + mem0 ADD/UPDATE/DELETE + jcode 值+脏标志 + dsh 增量合并旧 checkpoint 规则——组合 = **摘要 prompt 加合并规则 + 应用前过期校验 + 代码级漂移护栏**。
3. **根因③（信坏摘要→重做）**：gemini-cli 失败降级/膨胀拒绝 + SWE-agent hard exit + letta 摘要不保真/原文可 recall + deepagents offload 到可检索文件——组合 = **摘要失败 fail-closed + 提供「run 内被摘要掉的原文可检索」通道**。
4. **根因④（漂移循环）**：gemini-cli 语义等价 cycle + orca payloadFingerprint+代际 + herdr 双源仲裁——组合 = **detect_loop 从精确 prefix 扩为「内容指纹 + 语义等价 + 重做已编辑文件 + 权威/环境双源」软信号**。
5. **根因⑤（编辑数据回退）**：orca runtimeFence 单调代际 + herdr 结构态分离/版本化/原子写 + letta MemFS git 化 + gptme master context 单层 append-only + deepagents files channel 单层 StateBackend + opencode content-addressed 文件快照——组合 = **编辑数据「单调代际 + 版本化 + 原子写 + 单一权威」，与消息流解耦**。

---

## 五、边界与诚实记录

- **letta 的 compaction 阈值/模板在 host 侧，本仓库不可见**——只核实了「compaction is summarization, not loss」原则与 MemFS 机制，未给 host 侧 file:line。
- **OpenHands 的 V0 冷凝器已迁出**（读 git 历史 `7498353ed^` 的最后一代实现），当前 checkout 是 TS/Electron 客户端。
- **两个 langchain-ai notebook 是 demo 级**，无防漂移/无结构/无循环检测——只作反例与手法来源，不可照搬。
- **18 个项目里循环检测（根因④）无一个完整解决**：codex/opencode/OpenHands/cline/gptme/claw-code/mem0/jcode/letta 均无或仅步数上限；gemini-cli 最接近（prefix+cycle+语义等价）但仍是逐字/语义重复检测、抓不到「prefix 每步变 + 重做已编辑文件」的漂移循环。Clawith 的 `detect_loop` 已是其中最接近「结构化循环熔断」的，缺的是把精确匹配扩到软信号——这是差异化机会而非落后项。
- 所有 file:line 来自 5 个子代理对 `UGit/` 本地镜像与 `docs/technical-plans/` 研究报告的只读实读，未修改任何文件。
