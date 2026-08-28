# 上下文瘦身最佳实践一手调研（Context Slimming Best Practices）

> 日期：2026-08-26 ｜ 方法：仅引用官方文档、开源仓库源码、一手论文/工程博客；每条结论附来源 URL 与原文摘录。
> 本地源码来源：`~/Documents/UGit/deepagents`、`~/Documents/UGit/SWE-agent`、`~/Documents/UGit/codex`（2026-08-21 同步）；OpenHands 冷凝器已迁至 `OpenHands/software-agent-sdk` 仓库，按 GitHub 主分支 raw 抓取。
> 未能核验的内容均标注「未找到一手证据」（见各节）。

## 背景与问题

Clawith LLM agent runtime（LangGraph + DeepSeek v4，131K 窗口）实测：静态系统提示 2K token、跨 run 累积线程历史 ~24.5K、工具结果每步 +3.3K、动态块每步 1.8–5.3K 未命中缓存、36 步任务触发 2 次 Thread Compact（0.8×108K 水位，每次 18–30s 摘要调用 + KV 缓存全额重建）。

候选修复方案 4 项：
1. 跨 run 历史更激进摘要化 / 新 run 前轻量压缩
2. 工具结果截断（read_file 4.7K→2K 字符）
3. 动态块拆分（稳定部分移入 prefix-cache 边界内）
4. 验证门（每模型步后 LLM 校验完整轨迹 JSON，每步 8.7K token 全价重算）输入瘦身或降频

---

## 1. Prompt/Prefix Caching 最佳实践

### 1.1 Anthropic：breakpoint 放在「最后一个跨请求不变的 block」

官方文档明确把「静态前缀 + 变化后缀」的处理写成了规则：

> "Place the breakpoint on the last block that stays identical across requests. For a prompt with a static prefix and a varying suffix (timestamps, per-request context, the incoming message), that is the end of the prefix, not the varying block."
> — https://platform.claude.com/docs/en/build-with-claude/prompt-caching （"Best practices for effective caching"）

同页还有一段名为 **"Common mistake: Breakpoint on content that changes every request"** 的警示：把 breakpoint 放在含时间戳/用户消息的块上，会导致 "You pay for a fresh cache write on every request and never get a read"，并明确说明 lookback 机制"only finds entries that earlier requests wrote at their own breakpoints"——即缓存只认「曾经在 breakpoint 处写入过的前缀哈希」，不会自动替你跳过变化块。修复方式就是把 breakpoint 移到最后一个不变的 block 上。

其余官方建议（同页 "Best practices"）：

> "Cache stable, reusable content like system instructions, background information, large contexts, or frequent tool definitions."
> "Place cached content at the prompt's beginning for best performance."
> "Use cache breakpoints strategically to separate different cacheable prefix sections."
> "Use explicit block-level breakpoints when you need to cache different sections with different change frequencies."

关键参数（同页）：最多 **4 个 cache breakpoint**（"You can define up to 4 cache breakpoints if you want to: Cache different sections that change at different frequencies (for example, tools rarely change, but context updates daily)"）；breakpoint 本身不收费（"Adding more cache_control breakpoints doesn't increase your costs"）；cache write = 基价 +25%，cache read = 基价 10%；缓存层级为 `tools → system → messages`，"Changes at each level invalidate that level and all subsequent levels"（改工具定义清空全部缓存；改系统提示清空 system+messages 缓存；消息级改动只影响消息缓存）。可被缓存的内容包括 tools、system、用户/助手消息、图片、以及 **tool use 和 tool results** 两类块。

自动缓存（多轮对话）会自动把断点推进到「最后一个可缓存 block」——如果那个 block 每轮都变，自动缓存同样踩坑，官方建议改用显式断点。另一个生产细节：对话每轮新增 block 超过 **20 个**后，lookback 窗口（20 blocks）会错过上次写入点，官方建议提前加第二个 breakpoint。

> 来源：https://platform.claude.com/docs/en/build-with-claude/prompt-caching （自动缓存、显式断点、缓存层级/失效、费用、最佳实践各节）

### 1.2 OpenAI：动态内容放断点之后是官方推荐写法，且有更优模式（走工具/检索）

OpenAI prompt caching 文档给出三条直接相关建议：

> "**Keep the prefix stable.** Put stable developer instructions and shared reference material first. If developer instructions or shared material contain timestamps, user-specific content, or other dynamic content, place those at the end rather than the beginning, or move them into later conversation messages."
> "**Preserve conversation history.** Append new messages rather than rewriting earlier turns. Summarization, compaction, or context truncation can change the prefix and reset cache reuse."
> — https://platform.openai.com/docs/guides/prompt-caching （"Designing your prompt for caching"）

「动态内容放缓存区外」是官方显式模式，文档专门给了标题 **"Keep changing content after the breakpoint"** 的请求示例：稳定 developer 指令块打 `prompt_cache_breakpoint`，随后紧跟一个独立的「动态 developer 指令（用户特定内容、时间戳）」块，再跟用户当前问题——动态块在断点之后，不进缓存。更进一步，explicit-only 模式保证：

> "Content after the last selected breakpoint is processed at the uncached input-token rate without a cache-write charge, so you can avoid writing changing content that is unlikely to be reused."

多轮 agent 场景的官方示例还建议 **"A breakpoint is added after each tool result to preserve earlier reusable prefixes"**（每次工具结果后加显式断点，防止后续变化破坏此前可复用前缀；该模式示意部署命中率 >90%，官方注明为示意值）。

「动态内容走工具/检索而非消息」也是 OpenAI 官方建议——工具定义动态化时：

> "**Load tools when needed.** Use tool search with `defer_loading: true` to reduce input tokens spent on tool definitions in early requests of multi-turn threads. Discovered tools are appended at the end of context, preserving earlier reusable content."
> 以及 "**Keep tools consistent.** Preserve tool definitions, ordering, and schemas."（宁可用 `tool_choice:"none"`/`allowed_tools` 控制可用性，也不要改/删工具定义）

其他关键参数：每请求最多 **4 次 cache write**，读侧最多回溯 **50 个断点**；最小可缓存前缀 GPT-5.6+ 为 1024 tokens（更早模型 2048）；cache write 1.25×、read 0.1×。

> 来源：https://platform.openai.com/docs/guides/prompt-caching

### 1.3 DeepSeek：无手动断点，命中 = 与某个「cache prefix unit」全量匹配

DeepSeek 磁盘缓存默认开启、无需改代码，但没有 cache_control 之类的标记。命中规则：

> "A subsequent request can only hit the cache if it **fully matches** a **cache prefix unit**."

前缀单元的持久化有三种时机：①每个请求在「用户输入末尾」与「模型输出末尾」各产生一个 prefix unit；②系统检测到多请求的公共前缀后，把该公共前缀持久化为独立 unit；③长输入/长输出按固定 token 间隔切分 unit。官方示例 2（财务报告分析）说明：第 1、2 个请求（system + 报告内容 + 不同问题）都不命中，随后系统把 "system 消息 + <financial report content>" 识别为公共前缀 unit 并持久化，第 3 个请求即命中。另外：

> "The cache system works on a 'best-effort' basis and does not guarantee a 100% cache hit rate."
> "Cache construction takes seconds. Once the cache is no longer in use, it will be automatically cleared, usually within a few hours to a few days."

含义：DeepSeek 上没有「断点」可打，唯一杠杆是**让稳定前缀逐字节一致**（system 与稳定块在前、每步变化的动态块在尾），依赖公共前缀检测来持久化稳定段；命中仍是最佳努力性质，需用 `prompt_cache_hit_tokens` / `prompt_cache_miss_tokens` 实测。

> 来源：https://api-docs.deepseek.com/guides/kv_cache

### 1.4 工程实践交叉印证（SWE-agent 源码）

- `CacheControlHistoryProcessor`（Anthropic 系）：把 cache_control 打到**最后 2 条消息**并清除其余——"Add cache control to the last n user messages (and clear it for anything else). In most cases this should be set to 2 (caching for multi-turn conversations)."
  来源：https://github.com/SWE-agent/SWE-agent/blob/main/sweagent/agent/history_processors.py
- 同文件 `LastNObservations` 的 `polling` 参数注释直接点破「历史裁剪 vs 缓存」的互斥："This is useful for caching, as we want to remove more and more messages, but every time we change the history, we need to cache everything again."——他们的解法是**批量**删除（每 `polling` 步才改一次历史），把缓存重建成本摊薄。

### 1.5 对本方案（③动态块拆分）的启示

- 「每步变化的数据块放在缓存边界之外」**是三厂官方共同推荐**：Anthropic（breakpoint 放最后一个不变 block）、OpenAI（Keep changing content after the breakpoint + explicit-only 免写费）、DeepSeek（无断点，靠稳定前缀逐字节一致 + 公共前缀持久化）。
- 但注意 Clawith 用的是 **DeepSeek：没有 cache_control**。方案③中「拆到边界外」的实现必须落地为「稳定块绝对不变 + 动态块排在末尾」，而不能依赖标记。DeepSeek 公共前缀持久化需要 2 次以上请求才生效（官方示例），且 best-effort——上线后必须用 `prompt_cache_hit_tokens` 做真实回归，不能假设命中。
- 更优/补充模式（官方有据）：**动态内容尽量不进消息流**——Letta 的 system/ 外置按需读取（见 §2.6）、OpenAI 的 `defer_loading` 工具搜索（"appended at the end of context, preserving earlier reusable content"）、Anthropic 的 memory tool/结构化笔记与子 Agent 蒸馏（见 §2.1）。对 Clawith 动态块（每步 1.8–5.3K），可评估「动态块改为按需读文件/工具返回」而非每条消息内联。
- 多段不同变化频率的前缀可用多个断点（≤4）；缓存写 1.25×、读 0.1×——即使写多一段静态内容进缓存，只要复用 ≥2 次仍划算（OpenAI 文档的 break-even 公式也给出定量判据）。

---

## 2. Agent 线程压缩/摘要最佳实践

### 2.1 Anthropic《Effective context engineering for AI agents》——压缩方法论一手出处

这是官方对「何时压缩、怎么压缩」最完整的工程论述：

> "Compaction is the practice of taking a conversation nearing the context window limit, summarizing its contents, and reinitiating a new context window with the summary. Compaction typically serves as the first lever in context engineering to drive better long-term coherence."

Claude Code 的具体机制原文：

> "In Claude Code, for example, we implement this by passing the message history to the model to summarize and compress the most critical details. The model preserves architectural decisions, unresolved bugs, and implementation details while discarding redundant tool outputs or messages. The agent can then continue with this compressed context plus the five most recently accessed files."

调参方法论与「过度压缩」风险：

> "The art of compaction lies in the selection of what to keep versus what to discard, as overly aggressive compaction can result in the loss of subtle but critical context whose importance only becomes apparent later. For engineers implementing compaction systems, we recommend carefully tuning your prompt on complex agent traces. Start by maximizing recall to ensure your compaction prompt captures every relevant piece of information from the trace, then iterate to improve precision by eliminating superfluous content."

「最轻量压缩 = 清工具结果」的官方定性：

> "An example of low-hanging superfluous content is clearing tool calls and results – once a tool has been called deep in the message history, why would the agent need to see the raw result again? One of the safest lightest touch forms of compaction is tool result clearing"

跨会话/跨 run 的两个官方模式：**结构化笔记**（"Structured note-taking, or agentic memory, is a technique where the agent regularly writes notes persisted to memory outside of the context window... maintain project state across sessions, and reference previous work without keeping everything in context"）与**子 Agent 蒸馏**（"Each subagent might explore extensively, using tens of thousands of tokens or more, but returns only a condensed, distilled summary of its work (often 1,000-2,000 tokens)"）。

> 来源：https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents

### 2.2 Anthropic server-side Compaction（API 级自动压缩）

- 机制：`context_management.edits=[{type:"compact_20260112", trigger:{type:"input_tokens", value:...}}]`；"When compaction is enabled, Claude automatically summarizes your conversation when it reaches the configured token threshold... On subsequent requests, append the response to your messages. The API automatically drops all content blocks prior to the compaction block, continuing the conversation from the summary."
- **压缩 × 缓存的正解**：官方专门一节 "Maximizing cache hits with system prompts"——"Without additional cache breakpoints, this would also invalidate any cached system prompt... To maximize cache hit rates, add a cache_control breakpoint at the end of your system prompt. This keeps the system prompt cached separately from the conversation, so when compaction occurs: The system prompt cache remains valid... Only the compaction summary needs to be written as a new cache entry."（对应 Clawith「每次 Compact 后 KV 全额重建」：把系统提示单独断点，重建范围只剩对话部分。）
- 已知局限：**摘要只能用同一模型**（"There is no option to use a different (for example, cheaper) model for the summary"）；**请求含 tools 时摘要可能失败**（模型在内部摘要步去调工具），官方给的修复是把 instructions 写成 "Do not call any tools while writing this summary; respond with text only."。
- 适用性："Good use cases: Long-running agent tasks that process many files..."; "Less ideal use cases: Tasks requiring precise recall of early conversation details"。

> 来源：https://platform.claude.com/docs/en/build-with-claude/compaction 、https://platform.claude.com/docs/en/build-with-claude/context-editing （"When to use compaction"、SDK 侧阈值示例：默认 50000，"Lower values compact more often; raise to 150000 when the task needs more context"）

### 2.3 OpenAI Compaction（Responses API 自动压缩）

- `context_management=[{type:"compaction", compact_threshold: 200000}]`；"compaction to reduce context size while preserving state needed for subsequent turns. Compaction helps you balance quality, cost, and latency as conversations grow."
- 压缩产物是**不透明加密的 compaction item**（"carries forward key prior state and reasoning using fewer tokens"），非人类可读。
- **压缩后的 KV 复用官方建议**："After appending output items to the previous input items, you can drop items that came before the most recent compaction item to keep requests smaller and reduce long-tail latency. The latest compaction item carries the necessary context to continue the conversation."
- 与缓存互斥的官方确认（§1.2 同页表格）：`context_management (Compaction) | Replaces earlier conversation content with a compacted context that can prevent reuse from the first changed token onward.`

> 来源：https://platform.openai.com/docs/guides/compaction 、https://platform.openai.com/docs/guides/prompt-caching

### 2.4 deepagents SummarizationMiddleware（LangGraph 生态参考实现，本地源码）

`~/Documents/UGit/deepagents/libs/deepagents/deepagents/middleware/summarization.py`（LangChain `langchain/agents/middleware/summarization.py` 之上的增强版）：

- **默认阈值**（`compute_summarization_defaults`）：有 `max_input_tokens` profile 时 `trigger=("fraction", 0.85)`、`keep=("fraction", 0.10)`；无 profile 时 `trigger=("tokens", 170000)`、`keep=("messages", 6)`。LangChain 底座默认 `keep=20` 条消息，摘要输入上限 `trim_tokens_to_summarize=4000`。
- **摘要前先把历史落盘**：被淘汰消息以 markdown 追加到 `/conversation_history/{session_id}.md`，摘要消息自带路径引用——"The full conversation history has been saved to {file_path} should you need to refer back to it for details"；旧摘要消息本身不再重复落盘（`_filter_summary_messages`）。
- **摘要前轻量截断**（`truncate_args_settings`）：在低于完整压缩的阈值上，先把旧消息里 `write_file`/`edit_file` 的 tool_call args 截断（保留前 20 字符 + `...(argument truncated)`，`max_length` 默认 2000），"often reclaiming enough context to skip summarizing"。
- **溢出兜底**（`ContextOverflowError` → 摘要重试 + `_clip_overflow_tail`）：见 §3.2。
- **`compact_conversation` 工具**：工具描述原文 "Compact the conversation by summarizing older messages into a concise summary. **Use it when moving on to a completely new, unrelated task**, or after finishing synthesis or extraction when the previous working context is no longer needed."——即官方措辞支持「跨任务/跨 run 边界压缩」；资格门槛为自动摘要触发阈值的 **50%**（防止过早压缩）。

> 来源：https://github.com/langchain-ai/deepagents/blob/main/libs/deepagents/deepagents/middleware/summarization.py （本地同源）
> LangChain 底座：https://github.com/langchain-ai/langchain/blob/master/libs/langchain_v1/langchain/agents/middleware/summarization.py

### 2.5 OpenHands Condenser（阈值、取舍与 benchmark 实证）

OpenHands 冷凝器现位于 `OpenHands/software-agent-sdk`（原主仓库 `openhands/memory/condenser/` 已迁移）：

- **策略**（README）："the context window is condensed by replacing the **first half of all events with a single summary event**. This strategy performs well in benchmarks and strikes a balance between: 1. Per-completion cost... 2. **Cache optimization: condensation destroys the prompt cache, but doing so regularly keeps the cost of rebuilding the prompt cache low.** 3. Early context: events are summarized, and summaries are also summarized in future condensations... 4. Recent context: the back half of the context is untouched."
- **触发**：资源上限（`max_size` 事件数 默认 240、`max_tokens`）或显式请求；token 触发时目标是"eliminate to be under **half the effective max_tokens**"；`keep_first=2` 条事件永不被摘要；失败重试降级（`hard_context_reset`，5 次重试、每次把事件串按 0.8 缩放）。
- **摘要提示词**（`prompts/summarizing_prompt.j2`）：要求保留 USER_CONTEXT、TASK_TRACKING（"PRESERVE TASK IDs"）、COMPLETED/PENDING、CODE_STATE、TESTS（"Failing cases, error messages, outputs"）、CHANGES、VERSION_CONTROL_STATUS，且 "If the events being summarized contain ANY task-tracking, you MUST include a TASK_TRACKING section"——**递归摘要**（"will include previous summaries"）。
- **官方博客的实证**："Our new condenser implementation achieves: Up to **2x per-turn API cost reduction**, Consistent response times in long sessions, Equivalent (or better!) performance on software engineering tasks"；SWE-bench Verified 子集上 "context condensation solves a larger percentage of the SWE-bench problems using less money, fewer tokens, and less completion time - and these trends hold regardless of the breakpoints chosen. The only trade-off is with respect to the number of turns"（偶发多花一轮做冷凝）；"Baseline context management without condensation scales **quadratically** over time, while our condensed approach scales **linearly**"。

> 来源：https://github.com/OpenHands/software-agent-sdk/blob/main/openhands-sdk/openhands/sdk/context/condenser/README.md 、llm_summarizing_condenser.py 、prompts/summarizing_prompt.j2 ；https://github.com/OpenHands/docs/blob/main/sdk/arch/condenser.mdx ；https://openhands.dev/blog/openhands-context-condensensation-for-more-efficient-ai-agents

### 2.6 MemGPT/Letta：层级化记忆与「上下文外存储」

MemGPT 论文（arXiv:2310.08560）的 OS 式层级与**水位线压缩策略**：

- "The prompt tokens in MemGPT are split into three contiguous sections: the system instructions, working context, and FIFO Queue."；外部上下文 = recall storage（消息数据库）+ archival storage（任意长文本对象读写库）。
- **两级水位触发**："When the prompt tokens exceed the 'warning token count'... (e.g. 70% of the context window), the queue manager inserts a system message... (a 'memory pressure' warning) to allow the LLM to use MemGPT functions to store important information... to working context or archival storage. When the prompt tokens exceed the 'flush token count' (e.g. 100% of the context window), the queue manager flushes the queue... evicts a specific count of messages (e.g. 50% of the context window), generates a new **recursive summary** using the existing recursive summary and evicted messages."
- "The first index in the FIFO queue stores a system message containing a recursive summary of messages that have been evicted from the queue."——被淘汰消息"stored indefinitely in recall storage and readable via MemGPT function calls"（摘要 + 可检索原文，双保险）。

Letta（MemGPT 的后续产品）当前形态：

> "Files under `system/` are in the system prompt every turn. **Everything else stays out of context — the agent sees the file tree and reads what it needs.**"
> （记忆的其余部分不进上下文、按需读取——即「历史/状态外置为检索」模式的官方文档表述）
> "Dreaming uses background subagents to review recent conversations, consolidate lessons, and update memory without interrupting active work"，触发器可选 `"step-count"` 或 `"compaction-event"`——**跨 run 记忆整理发生在压缩事件/步数计数时**。

> 来源：https://arxiv.org/abs/2310.08560 ；https://docs.letta.com/agent-sdk/memory/index.md ；https://github.com/letta-ai/context-constitution

### 2.7 Claude Code / OpenAI 官方关于「跨任务、跨会话何时重置」的说法

- Claude Code best practices："Use `/clear` frequently between tasks to reset the context window entirely"；"**/clear**: reset context between unrelated tasks. Long sessions with irrelevant context can reduce performance."；"A clean session with a better prompt almost always outperforms a long session with accumulated corrections."；自动压缩："Claude Code automatically compacts conversation history when you approach context limits, which preserves important code and decisions while freeing space."；压缩保真可配置："Customize compaction behavior in CLAUDE.md with instructions like 'When compacting, always preserve the full list of modified files and any test commands'"。
  来源：https://code.claude.com/docs/en/best-practices （"Manage your session"）
- OpenAI Compaction 文档（§2.3）即官方「线程内自动摘要 + 压缩点之前的 item 可丢弃」的 API 化。

### 2.8 对本方案（①跨 run 压缩）的启示

- **「新 run 前轻量压缩」有官方背书**：deepagents `compact_conversation` 工具描述明言 "Use it when moving on to a completely new, unrelated task"；Claude Code 建议任务切换即 `/clear`；Letta 把跨会话记忆固化在 system/ 与按需读取文件里，线程历史不需要跨 run 全量携带。
- **压缩顺序遵循「最轻量优先」**：先清旧工具结果/截断旧参数（§2.1、§2.4），不够再全文摘要；Anthropic 把 tool result clearing 称为 "safest lightest touch form of compaction"。
- **阈值参考**：deepagents 0.85 触发/0.10 保留；OpenHands 触发后压到「一半」并永远保 `keep_first` 段 + 尾部不动；MemGPT 70% 警告/100% flush、逐出 50%。Clawith 现为 0.8×108K——与 deepagents 同量级，问题不在阈值而在**每次压缩的 KV 全量重建与摘要调用成本**。
- **摘要质量与缓存代价的官方答案**：①摘要提示词要显式保「任务状态/文件状态/失败用例」清单（OpenHands j2 模板、Claude Code CLAUDE.md 定制），按「先 recall 后 precision」调；②压缩必然破坏对话前缀缓存（OpenAI 明说 "Summarization, compaction, or context truncation can change the prefix and reset cache reuse"），缓解 = 把系统提示单独断点（Anthropic）、或接受重建但降低频率（OpenHands "doing so regularly keeps the cost of rebuilding low"）、或压缩点放在 run 边界（减少同 run 内两次 Compact）。Clawith 36 步 2 次 Compact 属于同 run 内高频重建，优先级应放在 ①/②/③ 让历史变轻，而不是把 Compact 水位再调低。
- 需要「精确回忆早期细节」的任务不适合激进的摘要（Anthropic "Less ideal use cases: Tasks requiring precise recall of early conversation details"）——Clawith 可对摘要强度按任务类型分级。

---

## 3. 工具结果截断最佳实践

### 3.1 Anthropic：官方认可「清工具结果」并给出服务端实现

- 服务端 `clear_tool_uses_20250919`："Older tool results (like file contents or search results) are no longer needed once Claude has processed them." "When activated, the API automatically clears the oldest tool results in chronological order. The API replaces each cleared result with **placeholder text** indicating to Claude that it was removed." 可选 `clear_tool_inputs: true` 连调用参数一起清。
  来源：https://platform.claude.com/docs/en/build-with-claude/context-editing
- 博客定性（§2.1）："once a tool has been called deep in the message history, why would the agent need to see the raw result again?"；Claude Code 压缩后保留 "the five most recently accessed files"、丢弃 "redundant tool outputs or messages"。
  来源：https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents

### 3.2 deepagents：两种截断形态（头部切片 + 完全外置）——Clawith 方案②的现成参考实现

`_overflow_clip.py`（本地源码）：

- `read_file` 工具结果：**保留头部 4000 字符 + 恢复指引**——"`read_file` results don't need a fresh `/large_tool_results/{tcid}` write... the agent can recover with `read_file(file_path=original_path, offset=N, limit=K)`"，追加的 notice 原文："[Output was truncated due to context window size limits. The full content is at {original_path}. Use read_file with offset and limit parameters to retrieve specific portions...]"（与工具自身的截断文案同构，保证模型看到的是统一格式）。
- 其他工具结果：整体外置到 `/large_tool_results/{tool_call_id}`，消息本体替换为 TOO_LARGE 桩。
- 触发条件：仅当尾部连续 ToolMessage 批量 token 数超过 keep 预算（默认 5000 tokens，按 "20_000-char floor under a chars/4 approximation"）时才动手——**小结果不截**。
- 旧消息参数截断（`summarization.py` `_truncate_args`）：只对 `write_file`/`edit_file` 的 args 生效，保留**头部 20 字符** + `...(argument truncated)`（默认 `max_length=2000`）——注意是「只留头、不留尾」。

> 来源：https://github.com/langchain-ai/deepagents/blob/main/libs/deepagents/deepagents/middleware/_overflow_clip.py 、summarization.py

### 3.3 SWE-agent：保留最近 N 条观测 + 占位符 + 标签化例外 + 窗口去重

`history_processors.py`（本地源码）：

- `LastNObservations`（原论文即用 n=5）："Elide all but the last n observations... Elided observations are replaced by 'Old environment output: (n lines omitted)'."；支持 `always_keep_output_for_tags`（错误等关键输出打 tag 保底）与 `always_remove_output_for_tags`；`polling` 参数批量删除以摊薄缓存重建（§1.4）。
- `ClosedWindowHistoryProcessor`：同一文件只保留**最后一个查看窗口**，更早窗口替换为 "Outdated window with {n} lines omitted..."（消除重复文件内容）。
- `RemoveRegex`：默认从旧历史剥掉 `<diff>.*</diff>` 块（`keep_last=0`）。

> 来源：https://github.com/SWE-agent/SWE-agent/blob/main/sweagent/agent/history_processors.py

### 3.4 截断对任务成功率的影响证据

- **未找到「纯截断 vs 不截断」的对照实验论文/报告**（按方法要求标注为未找到一手证据）。可类比的最接近实证是 OpenHands 冷凝（摘要化而非纯截断）的 benchmark 报告：SWE-bench Verified 子集上 "solves a larger percentage of the SWE-bench problems using less money, fewer tokens, and less completion time"（§2.5）。
- 间接证据（长上下文中信息位置效应）：MemGPT 论文在讨论为何需要分层记忆时引述 "the model is more capable of recalling information at the beginning or end of its context window, vs tokens in the middle"（即 Liu et al. 2023 "Lost in the Middle"）——支持「旧结果压缩掉、靠新窗口头部/尾部与检索找回」的方向，但不是截断 A/B。
- Claude Code 自身对工具输出截断的**具体阈值（字符数）未在公开文档中给出**（best-practices 与 costs 页均无 truncate 相关条目，已抓取核对）——标注为未找到一手证据。

### 3.5 对本方案（②read_file 4.7K→2K）的启示

- 方向获得官方与开源双重背书：Anthropic 服务端工具结果清除、deepagents 4K 头切片、SWE-agent last-5 观测 + 占位符。
- **修正点**：截断必须同时满足 ①可恢复路径（deepagents 的 path + offset/limit 指引是标准做法）；②错误信息优先保留（SWE-agent 的 keep/remove tags 机制；OpenHands 摘要模板要求保留 "Failing cases, error messages"）；③只对「旧」结果动手（deepagents 阈值 + keep 窗口、SWE-agent last-n）。Clawith 的 2K 字符与 deepagents 的 4000 字符同量级，属合理区间；若 read_file 结果常被后续 offset 读取替代，建议直接改成「默认返回窗口 + 行号视图」而非全文。
- 对「是否保留尾部」：deepagents 选择头部切片（args 只留前 20 字符），SWE-agent 保留完整最近观测、只占位化更旧的。无官方「头+尾」证据；按 lost-in-the-middle 的提示，保留头 + 关键错误尾 + 路径指引是合理的工程折中，但需自行评测。
- 成功率影响缺直接实验证据——建议在 Clawith 评测集上做截断开关 A/B，再定字符阈值。

---

## 4. 验证门/评价器的成本控制

### 4.1 Anthropic Building Effective Agents：每步的 ground truth 是环境反馈，不是另一个 LLM

- evaluator-optimizer 模式的定义是「生成-评估」循环，但官方给的使用判据是**有清晰评价标准且迭代能带来可测量价值**，而非「每步必评」："This workflow is particularly effective when we have clear evaluation criteria, and when iterative refinement provides measurable value."
- 对自主 agent 循环，官方明确把**环境反馈**作为每步校验信号："During execution, it's crucial for the agents to gain 'ground truth' from the environment at each step (such as tool call results or code execution) to assess its progress."——每步校验 = 工具结果/代码执行，不需要额外 LLM。
- 程序化门（gate）的位置建议在**中间步骤上按需加**："You can add programmatic checks (see 'gate' in the diagram below) on any intermediate steps to ensure that the process is still on track."（prompt chaining 工作流）。
- 对编程 agent 的验证："Code solutions are verifiable through automated tests; Agents can iterate on solutions using test results as feedback"。

> 来源：https://www.anthropic.com/engineering/building-effective-agents

### 4.2 Claude Code：默认用「可执行检查」，每轮 LLM 评价器是可选功能

官方 best-practices 的验证哲学原文："Claude stops when the work looks done. Without a check it can run, 'looks done' is the only signal available, and you become the verification loop." 推荐的四档门（成本从低到高）：

1. 提示词内：给可执行检查（测试/构建/linter）让模型自己跑并迭代；
2. **每轮 LLM 评价器（opt-in）**："set the check as a `/goal` condition. **A separate evaluator re-checks it after every turn** and Claude keeps working until the goal resolves."——说明「每步 LLM 校验」在 Claude Code 里存在，但是**可选目标条件**而非默认；
3. 确定性门：Stop hook 跑脚本检查，不通过就不让轮次结束（"Claude Code overrides the hook and ends the turn after 8 consecutive blocks"）；
4. 第二意见：verification subagent，"a fresh model try to refute the result, so the agent doing the work isn't the one grading it"。

> 来源：https://code.claude.com/docs/en/best-practices （"Give Claude a way to verify its work"）

### 4.3 业界编程 Agent：无「每步 LLM 校验」，验证集中在执行反馈与关键节点

- **SWE-agent**：agent 循环内无 LLM 验证器（本地源码 `sweagent/` 无 verifier/evaluator 模块，grep 核实）；验证发生在**提交节点**——default.yaml 的提交模板要求模型 revert 测试文件改动、"Run the submit command again to confirm"，随后由测试环境判定。来源：https://github.com/SWE-agent/SWE-agent/blob/main/config/default.yaml
- **OpenHands**：agent 主循环无每步 LLM 校验（`software-agent-sdk` 核心无 verifier/evaluator 组件，grep 核实）；验证靠 shell 执行反馈（测试输出）与基准评测器。来源：https://github.com/OpenHands/software-agent-sdk
- **codex（OpenAI Codex CLI）**：会话循环里额外的 LLM 调用只有压缩摘要（§2.4 所述 compaction turn）；`/review` 是用户显式触发的代码评审，不是每步门。来源：https://github.com/openai/codex （codex-rs/core/src/compact.rs、docs/）
- OpenAI Codex 技术报告原想纳入（其 harness 含 session summary 机制），但 arXiv API 本次访问 503、且 2507.13511 编号经核实为另一篇论文（GraphTrafficGPT）——**Codex 论文相关结论标注为未核验**。

### 4.4 校验器输入复用的官方配方（对应方案④「输入瘦身」）

OpenAI prompt caching 文档的「单轮 LLM 评判器」官方示例，正好是校验门的成本控制标准答案：

> "Consider a single-turn LLM judge that determines whether a completed interaction shows evidence that the user is satisfied... **Preserving the prefix:** The fixed rubric and examples come first. Their combined length is deliberately kept just above the model's minimum cacheable length, using material that helps calibrate the judge. The interaction being evaluated comes last. **Caching mode and breakpoint:** Explicit-only caching is enabled, with a breakpoint after the fixed rubric and examples. The user–chatbot conversation being evaluated comes after that breakpoint and **is not written to the cache, avoiding a cache-write charge** for content that is unlikely to be reused."（示意部署命中率约 70%，官方注明为假设值；cache key 用稳定版本号如 `satisfaction_judge_v1`）

配套事实：Anthropic cache read=10% 基价、breakpoint 免费（§1.1）——校验器固定 rubric 前缀缓存在 DeepSeek 上同样可借「公共前缀单元」机制实现（需 rubric 逐字节稳定）。

> 来源：https://platform.openai.com/docs/guides/prompt-caching （"Examples"）

### 4.5 对本方案（④验证门）的启示

- **「每步 LLM 校验」不是业界默认**：SWE-agent / OpenHands / codex 主循环均无此物；Anthropic 明说每步 ground truth 用环境反馈（工具结果、代码执行）；Claude Code 默认用可执行检查，每轮 LLM 评价器仅作为 `/goal` 可选功能存在（且有 8 次连续阻塞后强制放行的保护）。方案④「每模型步后校验完整轨迹 JSON、每步 8.7K 全价」与主流实践相反，成本证据（每步全价重算）也最差。
- **修正为「关键节点校验」**：校验点放在编译/测试失败后、提交/终止前（SWE-agent 提交节点、BEA 的 gate 措辞、Claude Code 的 check 循环）。失败驱动 + 关键节点 = 低频高价值。
- **输入瘦身的官方手段**：校验器输入只给「待评审增量」（本次 diff、轨迹切片、关键输出），rubric/评分标准作为**缓存前缀**且保持在最小可缓存长度以上、评估对象放断点后不写缓存（OpenAI judge 示例）；DeepSeek 上等价做法是 rubric 前缀逐字节稳定 + 公共前缀持久化。
- 若保留「每轮校验」需求，对标 Claude Code `/goal`：用便宜模型、只评目标条件（而非全轨迹 8.7K JSON），并设硬性放行上限。

---

## 总体裁决：方案 4 项逐条

| # | 方案 | 裁决 | 依据摘要 |
|---|------|------|----------|
| ① | 跨 run 历史激进摘要化 / 新 run 前轻量压缩 | **支持（带修正）** | 官方与开源一致背书：deepagents compact 工具明言 "moving on to a completely new, unrelated task" 时压缩；Claude Code 建议任务切换即 /clear；Letta 跨会话记忆外置 system/ + 按需读取。修正：a) 顺序上「最轻量优先」——先清旧工具结果/截断旧参数（Anthropic "safest lightest touch"、deepagents truncate_args），不足再摘要；b) 摘要提示词必须显式保留任务状态/文件状态/失败用例（OpenHands j2、Claude Code CLAUDE.md），按「先 recall 后 precision」调；c) 压缩会重置前缀缓存是两厂确认的固有代价——把系统提示单独断点（Anthropic）、或把压缩点移到 run 边界以降低同 run 内多次重建；d) 需精确回忆早期细节的任务不宜激进摘要（Anthropic less-ideal 用例）。 |
| ② | 工具结果截断（read_file 4.7K→2K 字符） | **支持（带修正）** | 官方与参考实现齐备：Anthropic 服务端 tool result clearing（占位符替换、按时间从旧到新清）、deepagents 头部 4K + path/offset/limit 恢复指引、SWE-agent last-5 观测 + "Old environment output (n lines omitted)"。修正：a) 必须有恢复路径（deepagents 的指引文案可直抄）；b) 错误信息优先保留（SWE-agent keep/remove tags）；c) 只截「旧」结果（阈值 + keep 窗口）。2K 与 deepagents 4K 同量级，合理。**纯截断对成功率的影响无直接一手 A/B 证据**（最接近的实证是 OpenHands 冷凝在 SWE-bench 上「更少钱/token/时间解出更多题」）——建议先在自家评测集做截断开关 A/B 再定阈值。 |
| ③ | 动态块拆分进缓存边界 | **支持（带修正）** | 三厂官方一致：「动态内容放缓存边界之外」是显式推荐——OpenAI "Keep changing content after the breakpoint" + explicit-only 免写费；Anthropic "breakpoint 放在最后一个跨请求不变的 block"（含"Common mistake"反例）；DeepSeek 无断点，靠稳定前缀逐字节一致 + 公共前缀持久化（需 2+ 请求才生效、best-effort）。修正：a) DeepSeek 上无法打标记，「拆分」的实质 = 稳定块绝对不变 + 动态块排尾，并用 prompt_cache_hit_tokens 实测；b) 官方更优模式是动态内容尽量**不内联进消息**：Letta system/ 外置按需读取、OpenAI defer_loading 工具搜索（"preserving earlier reusable content"）、Anthropic memory tool / 子 Agent 蒸馏（1-2K 摘要）——Clawith 动态块可评估改走工具/检索。 |
| ④ | 验证门（每步 LLM 校验全轨迹 8.7K） | **修正** | 「每步 LLM 校验」非业界默认：SWE-agent/OpenHands/codex 主循环均无（源码核实）；Anthropic 明说每步 ground truth 用**环境反馈**（工具结果/代码执行）；Claude Code 默认用可执行检查，每轮 LLM 评价器只是 `/goal` 可选功能。修正：a) 校验放到关键节点（编译/测试失败后、提交/终止前），失败驱动；b) 校验器输入瘦身：只评增量（diff/切片），rubric 作为缓存前缀（保持 ≥ 最小可缓存长度）、评估对象放断点后不写缓存（OpenAI judge 官方示例，读 0.1×）；c) 若确需每轮校验，对标 `/goal`：便宜模型 + 只评目标条件 + 硬性放行上限。 |

**优先级建议**：③（结构不变 + 动态块外移/工具化，收益最大且零语义损失）→ ②（截断 + 恢复路径，先 A/B）→ ①（run 边界轻量压缩，替换同 run 内高频 Thread Compact）→ ④（从「每步全价校验」降为「关键节点 + 缓存 rubric」）。

---

## 附：来源清单

**官方文档**
- Anthropic Prompt Caching：https://platform.claude.com/docs/en/build-with-claude/prompt-caching
- Anthropic Compaction（server-side）：https://platform.claude.com/docs/en/build-with-claude/compaction
- Anthropic Context Editing（tool result / thinking clearing、SDK compaction）：https://platform.claude.com/docs/en/build-with-claude/context-editing
- OpenAI Prompt Caching：https://platform.openai.com/docs/guides/prompt-caching
- OpenAI Compaction：https://platform.openai.com/docs/guides/compaction
- DeepSeek Context Caching：https://api-docs.deepseek.com/guides/kv_cache
- Letta Memory：https://docs.letta.com/agent-sdk/memory/index.md
- Claude Code Best Practices：https://code.claude.com/docs/en/best-practices
- Claude Code Costs（已核对，无截断阈值条目）：https://code.claude.com/docs/en/costs

**一手论文/官方博客**
- Anthropic, Building Effective Agents：https://www.anthropic.com/engineering/building-effective-agents
- Anthropic, Effective Context Engineering for AI Agents：https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents
- MemGPT (Packer et al., 2023)：https://arxiv.org/abs/2310.08560
- OpenHands, Context Condensensation for More Efficient AI Agents：https://openhands.dev/blog/openhands-context-condensensation-for-more-efficient-ai-agents

**开源源码**
- deepagents（summarization / overflow clip / message eviction）：https://github.com/langchain-ai/deepagents/tree/main/libs/deepagents/deepagents/middleware
- LangChain SummarizationMiddleware：https://github.com/langchain-ai/langchain/blob/master/libs/langchain_v1/langchain/agents/middleware/summarization.py
- OpenHands condenser（software-agent-sdk）：https://github.com/OpenHands/software-agent-sdk/tree/main/openhands-sdk/openhands/sdk/context/condenser
- OpenHands condenser 架构文档：https://github.com/OpenHands/docs/blob/main/sdk/arch/condenser.mdx
- SWE-agent history_processors：https://github.com/SWE-agent/SWE-agent/blob/main/sweagent/agent/history_processors.py
- SWE-agent default.yaml：https://github.com/SWE-agent/SWE-agent/blob/main/config/default.yaml
- OpenAI Codex CLI（compact.rs、prompts/templates/compact）：https://github.com/openai/codex

**未核验/未找到一手证据项**
- OpenAI《A practical guide to building agents》：官方站点 Cloudflare 拦截（curl/fetch 403，记忆中有记录），本次未引用。
- OpenAI Codex 技术报告的 session-summary 细节：arXiv API 503 且编号待核，未引用；以 codex-rs 源码替代。
- Claude Code 工具输出截断的具体字符阈值：公开文档未找到，未引用。
- 「纯工具结果截断对任务成功率的对照实验」：未找到，以上下文压缩（OpenHands 冷凝）实证替代。
