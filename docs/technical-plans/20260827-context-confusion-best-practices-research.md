# 长对话/多 run 共享线程中「模型答非所问」的上下文管理最佳实践调研

- 日期：2026-08-27
- 调研目的：为 Clawith 飞书群聊「同群 = 同 chat_session = 同 LangGraph thread，多 run 消息累积」导致的「新 run 把旧问题当新指令」故障的修复选型（D1 任务锚点 / D2 失败通知降级 / D3 群历史按 run 边界截断）提供一手证据
- 故障背景（供对照）：①上下文里「最近会话消息快照」含几十条群历史 user 消息，最后一条恰是旧问题「NotesApp 项目是干什么的」；②顶部残留更早失败 run 的系统通知「任务执行未完成。错误码：reconciliation_required」；③R1 修复后第 3 模型步起末尾控制消息是固定续接文案（不重放原始指令），模型丢失当前任务锚点
- 抓取方法：mcp fetch + curl（带浏览器 UA）；均为一手来源（官方博客/官方文档/官方仓库源码）。标注「原文」= 逐字摘录，「归纳」= 压缩转述
- 失败源记录：`openai.com` 博客与 `help.openai.com`（Cloudflare/403）不可达，已换用 `learn.chatgpt.com`（ChatGPT/Codex 官方文档站，markdown 直出）与 `platform.openai.com` 文档；LangGraph 旧文档路径（`how-tos/memory/summarize-conversation`）已 404，内容已迁移合并到新「Memory」页（`add-memory`）

---

## 1. 对话压缩/摘要（compaction/summarization）最佳实践

### 1.1 Anthropic《Effective context engineering for AI agents》（2025-09-29）

来源：https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents

- 原文：「Studies on needle-in-a-haystack style benchmarking have uncovered the concept of **context rot**: as the number of tokens in the context window increases, the model's ability to accurately recall information from that context decreases.」
  —— 上下文越大，回忆精度越低，且「this characteristic emerges across all models」。压缩的第一性理由。
- 原文：「LLMs have an "attention budget" that they draw on when parsing large volumes of context. Every new token introduced depletes this budget by some amount.」
- 原文（compaction 定义）：「Compaction is the practice of taking a conversation nearing the context window limit, summarizing its contents, and reinitiating a new context window with the summary. Compaction typically serves as the first lever in context engineering to drive better long-term coherence.」
- 原文（Claude Code 的实现——保留/丢弃策略）：「In Claude Code, for example, we implement this by passing the message history to the model to summarize and compress the most critical details. **The model preserves architectural decisions, unresolved bugs, and implementation details while discarding redundant tool outputs or messages.** The agent can then continue with this compressed context plus the five most recently accessed files.」
  —— 关键：压缩摘要的保留重点是「决策、未决问题、实现细节」，丢弃的是「冗余工具输出/消息」。
- 原文（压缩 prompt 调优方向）：「Start by maximizing recall to ensure your compaction prompt captures every relevant piece of information from the trace, then iterate to improve precision by eliminating superfluous content.」
- 原文（最轻量压缩）：「One of the safest lightest touch forms of compaction is tool result clearing」——清除历史深处的工具调用与结果。
- 原文（替代方案：结构化笔记）：「Structured note-taking, or agentic memory... the agent regularly writes notes persisted to memory outside of the context window. These notes get pulled back into the context window at later times... After context resets, the agent reads its own notes and continues.」
- 原文（替代方案：子代理）：「Each subagent might explore extensively... but returns only a condensed, distilled summary of its work (often 1,000-2,000 tokens).」

### 1.2 Anthropic 官方 API 文档：Server-side compaction

来源：https://docs.claude.com/en/docs/build-with-claude/compaction

- 原文：「It also keeps the active context small: **as a conversation grows, response quality degrades**, so compaction replaces older content with a concise summary.」
  —— 官方文档明确把「对话增长→质量下降」作为压缩的必要性陈述。
- 原文（默认摘要 prompt 的目标）：「The purpose of this summary is to provide continuity so you can continue to make progress towards solving the task in a future context, where the raw history above may not be accessible and will be replaced with this summary. Write down anything that would be helpful, including **the state, next steps, learnings** etc.」
- 原文（压缩后如何重注入指令）：「Use `pause_after_compaction` to pause the API after generating the compaction summary. **This allows you to add additional content blocks (such as preserving recent messages or specific instruction-oriented messages) before the API continues** with the response.」
  —— 官方提供了一等公民机制：压缩暂停后、继续推理前，由应用层把「指令导向的消息」追加回去。这是 D1 的官方背书。
- 原文：自定义 `instructions` 参数「Completely replaces the default prompt」，示例为 `"instructions": "Focus on preserving code snippets, variable names, and technical decisions."`。
- 原文（示例：保留最近对话原样）：「preserve the prior exchange and the current user message (three messages total) verbatim instead of summarizing them」。
- 原文（压缩块语义）：「The API automatically **drops all content blocks prior to the compaction block**, continuing the conversation from the summary.」
- 原文：系统提示与压缩解耦——加 cache_control 断点后「The system prompt cache remains valid... Only the compaction summary needs to be written as a new cache entry」。系统提示（developer 指令）天然跨压缩存活。

### 1.3 LangGraph 官方文档：Memory（trim / delete / summarize）

来源：https://docs.langchain.com/oss/python/langgraph/add-memory（旧 how-to「Summarizing past conversations」已合并至此页，原路径 404）

- 原文：「With short-term memory enabled, long conversations can exceed the LLM's context window. Common solutions are: Trim messages... Delete messages... **Summarize messages: Summarize earlier messages in the history and replace them with a summary**...」
- 原文（trim_messages 用法）：`trim_messages(messages, strategy="last", token_counter=count_tokens_approximately, max_tokens=128, start_on="human", end_on=("human","tool"))` —— 关键参数：按 token 数截断、保留最近（strategy="last"）、边界对齐到 human/tool 消息。
- 原文（RemoveMessage）：`RemoveMessage(id=m.id)` 删除指定消息，或 `RemoveMessage(id=REMOVE_ALL_MESSAGES)` 清空；并警告「make sure that the resulting message history is valid. Check the limitations of the LLM provider... Most providers require assistant messages with tool calls to be followed by corresponding tool result messages.」
- 原文（summarize_conversation 模式）：State 增加 `summary: str` 键；节点用「Extend the summary by taking into account the new messages above」滚动续写摘要；然后「Delete all but the 2 most recent messages」。
- 原文（langmem SummarizationNode）：`SummarizationNode(token_counter=..., model=..., max_tokens=256, max_tokens_before_summary=256, max_summary_tokens=128)` —— 业界标准参数：触发阈值 + 摘要长度上限。

### 1.4 LangChain 官方文档：Short-term memory

来源：https://docs.langchain.com/oss/python/langchain/short-term-memory

- 原文（对本题最直接的官方论述）：「Even if your model supports the full context length, **most LLMs still perform poorly over long contexts. They get "distracted" by stale or off-topic content**, all while suffering from slower response times and higher costs.」
- 原文：「Because context windows are limited, many applications can benefit from using techniques to remove or "forget" stale information.」
- 原文（中间件形态）：`SummarizationMiddleware(model="gpt-5.4-mini", trigger=("tokens", 4000), keep=("messages", 20))` —— 超过 4000 token 触发摘要，保留最近 20 条消息原文。

### 1.5 OpenAI 官方 API 文档：Compaction（Responses API）

来源：https://platform.openai.com/docs/guides/compaction

- 原文：「To support long-running interactions, you can use compaction to reduce context size while preserving state needed for subsequent turns.」
- 原文（服务端压缩）：`context_management` + `compact_threshold`；「The returned compaction item **carries forward key prior state and reasoning into the next run using fewer tokens**. It is opaque and not intended to be human-interpretable.」
- 原文（独立压缩端点 `/responses/compact`）：「You send a full context window... and the endpoint returns a new compacted context window... **The compacted window generally contains more than just the compaction item. It can also include retained items from the previous window.**」
- 原文（截断安全建议）：「After appending output items to the previous input items, **you can drop items that came before the most recent compaction item** to keep requests smaller... The latest compaction item carries the necessary context to continue the conversation.」
  —— 官方明确：压缩标记之前的旧内容可安全丢弃，因为压缩项已承载必要上下文。这是 D3（旧历史截断）的直接依据。

### 1.6 OpenAI 官方 Cookbook：Building Reliable Agents with Memory and Compaction

来源：https://github.com/openai/openai-cookbook/blob/main/examples/agents_sdk/building_reliable_agents_memory_compaction.ipynb

- 原文：「**Compaction** lets you support long-running conversations despite finite context windows by carrying forward the state needed for later turns while reducing context size. **Memory** lets future sandbox-agent runs reuse workflow lessons from prior runs without replaying every previous turn.」
- 原文（Memory vs Compaction 对照表）：「What does it summarize? Compaction: The active conversation and working state. / Memory: Patterns, preferences, and process lessons worth reusing.」
- 原文（Best practices）：「**Compact at meaningful workflow boundaries, not after every turn.** Preserve enough working state for the next phase to make sense. Keep cited facts in generated artifacts, not only in compacted conversation state.」

### 1.7 Anthropic Cookbook：Automatic Context Compaction（Claude Agent SDK）

来源：https://github.com/anthropics/anthropic-cookbook/blob/main/tool_use/automatic-context-compaction.ipynb

- 原文（归纳）：`compaction_control` 参数在 token 超过可配置阈值时自动压缩会话历史；其引用「Effective Context Engineering」说明动机是避免「performance degradation and context rot」；并注明 Opus 4.6+ 推荐改用服务端压缩（即 1.2 的 API）。

### 1.8 Anthropic《Building Effective Agents》（2024-12-19）

来源：https://www.anthropic.com/engineering/building-effective-agents

- 原文：「The basic building block of agentic systems is an LLM enhanced with augmentations such as **retrieval, tools, and memory**... determining what information to retain.」
- 归纳：该文重心是 workflow vs agent 选型，未展开压缩细节；其「memory 作为增强组件」的框架与 1.1/1.7 一致。作为压缩实践的一手来源贡献有限。

---

## 2. 指令锚定 / 长上下文迷失（lost-in-the-middle、recency bias）

### 2.1 基础研究：Lost in the Middle

来源：Liu et al., "Lost in the Middle: How Language Models Use Long Contexts", arXiv:2307.03172 — https://arxiv.org/abs/2307.03172

- 原文（摘要）：「**performance is often highest when relevant information occurs at the beginning or end of the input context, and significantly degrades when models must access relevant information in the middle of long contexts**, even for explicitly long-context models.」
  —— 位置效应的权威出处：开头/结尾信息最可靠，中间最易丢。推论：当前任务指令应放在接近末尾处，或干脆放到开头（system 层）且不被中段历史淹没。

### 2.2 Anthropic：context rot 与注意力预算

来源：https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents

- 原文见 1.1（context rot / attention budget 两句）。Anthropic 官方将「上下文增长→回忆精度下降」归因为注意力预算与训练分布（短序列更常见）：「models have less experience with, and fewer specialized parameters for, context-wide dependencies.」

### 2.3 Anthropic 官方提示工程文档：长上下文技巧

来源：https://docs.claude.com/en/docs/build-with-claude/prompt-engineering/long-context-tips

- 原文：「**Queries at the end can improve response quality by up to 30 percent in tests**, especially with complex, multidocument inputs.」（长文档放顶部、查询/指令放末尾的结构。）
- 原文（给运行在「会压缩上下文的 agent harness」里的模型加的样例指令）：「Your context window will be automatically compacted as it approaches its limit... **do not stop tasks early due to token budget concerns. As you approach your token budget limit, save your current progress and state to memory before the context window refreshes.**」
  —— 官方明确：身处压缩环境时，需要在提示词里显式告知模型「压缩会发生、任务锚点会通过 memory 延续」。
- 原文（跨上下文窗口工作流）：「Starting fresh versus compacting: When a context window is cleared, **consider starting with a brand new context window rather than using compaction.** Claude's latest models are extremely effective at discovering state from the local filesystem.」

### 2.4 OpenAI 官方提示工程文档：每轮指令不自动延续

来源：https://platform.openai.com/docs/guides/prompt-engineering

- 原文：「Note that **the instructions parameter only applies to the current response generation request. If you are managing conversation state with the `previous_response_id` parameter, the instructions used on previous turns will not be present in the context.**」
  —— 对本题最直接的一条：多轮对话中，「上一轮的指令」不会自动出现在下一轮上下文里——凡是要跨轮生效的指令/任务目标，必须由应用层每轮显式重新注入。这正是 Clawith「固定续接文案丢失原始指令」的机制性解释，也是 D1 的一手依据。
- 原文（角色优先级表）：developer（应用指令）> user > assistant。
- 原文（结构建议）：「Context: ... This content is usually best positioned near the end of your prompt.」

### 2.5 业界做法：末尾重放当前指令 vs 固定续接文案

- Codex CLI `/goal`（官方文档）：https://learn.chatgpt.com/docs/long-running-work
  - 原文：「The goal text becomes both the first prompt and the completion criteria for the task.」「Codex **keeps the goal attached to the active chat while work continues**.」
  - 归纳：Codex 用独立的持久 goal 字段（上限 4000 字符，可指向文件）承载任务锚点，与滚动历史解耦——等价于「每轮末尾附锚点」的产品化实现，而非把目标埋在历史里。
- Anthropic 官方 compaction 文档 `pause_after_compaction`（见 1.2）：原文明确用途包括「preserving... specific instruction-oriented messages」——官方推荐压缩后由应用层重新注入指令消息。→ 支持「重放原始指令」而非「固定续接文案」。
- Claude Code 官方文档（memory 页，Troubleshoot「Instructions seem lost after /compact」）：https://code.claude.com/docs/en/memory
  - 原文：「**If an instruction disappeared after compaction, it was given only in conversation**, lives in a nested CLAUDE.md that hasn't reloaded yet... Add conversation-only instructions to CLAUDE.md to make them persist.」
  - 原文（context-window 页「What survives compaction」）：https://code.claude.com/docs/en/context-window
    - 「Project-root CLAUDE.md and unscoped rules: **Re-injected from disk**」「System prompt and output style: Unchanged; not part of message history」「Files Claude read or edited: Claude Code re-reads up to five, most recently modified first」。
    - 归纳：Claude Code 的设计原则是——持久指令放在「压缩后自动重注入」的层（system prompt / CLAUDE.md），而不是会话历史里；会话历史一律被摘要化。
- OpenHands 压缩提示词（源码，见 4.2）：压缩摘要的第一节固定为 `USER_CONTEXT: (Preserve essential user requirements, goals, and clarifications in concise form)`——「压缩时保留用户目标」是压缩提示词的硬性要求。→ D1 在「压缩环节」的镜像做法。
- ChatGPT/Codex 官方 memories 文档：https://learn.chatgpt.com/docs/customization/memories
  - 原文：「Keep required team guidance in `AGENTS.md` or checked-in documentation. **Treat memories as a helpful recall layer, not as the only source for rules that must always apply.**」
  - 归纳：记忆/摘要属「软层」，必须始终生效的规则/目标要放在每会话重注入的硬层（AGENTS.md）——与 Claude Code 同一原则。

**取舍小结（基于以上一手来源）**：权威做法不是「在续接文案里写一句固定话」，而是 ①把任务目标做成结构化锚点（system 层或每轮末尾显式重放原始指令），②把跨轮必须遵守的规则放在压缩后重注入的层。固定续接文案的问题正是 OpenAI 2.4 所述「instructions only applies to the current request」的变体：一旦固定文案不再包含目标本身，模型就失去目标。

---

## 3. 多轮/多 run 历史如何呈现（业界 CLI 实践）

### 3.1 Claude Code

来源：https://code.claude.com/docs/en/context-window 与 https://www.anthropic.com/engineering/claude-code-best-practices

- 原文（context-window 页）：「At the end: `/compact` **replaces the conversation with a structured summary.**」
- 原文：「**Compact with a focus**: run `/compact` with instructions, like `/compact focus on the auth bug fix`, before starting a long new task. The summary keeps what you choose instead of what the automatic pass guesses is important.」
- 原文：「`/clear` between tasks: **Old conversation crowds out the files you need next and costs tokens on every message.**」
- 原文（best-practices 博客）：「Most best practices are based on one constraint: **Claude's context window fills up fast, and performance degrades as it fills.**」
- 原文：「When the context window is getting full, Claude may start **"forgetting" earlier instructions or making more mistakes.**」
- 原文（失败尝试污染上下文）：「**If you've corrected Claude more than twice on the same issue in one session, the context is cluttered with failed approaches. Run `/clear` and start fresh** with a more specific prompt that incorporates what you learned. A clean session with a better prompt almost always outperforms a long session with accumulated corrections.」
  —— 这是「失败/纠正类历史进后续轮次有害」的最直接官方表述，直接支持 D2/D3。
- 原文（任务边界）：「Once the spec is complete, **start a fresh session to execute it.**」
- 原文（压缩保留内容可定制）：「Customize compaction behavior in CLAUDE.md with instructions like "When compacting, always preserve the full list of modified files and any test commands" to ensure critical context survives summarization.」

### 3.2 OpenAI Codex CLI

来源：https://learn.chatgpt.com/docs/developer-commands?surface=cli 与 https://learn.chatgpt.com/docs/config-file/config-reference

- 原文（`/compact`）：「Codex **replaces earlier turns with a concise summary, freeing context while keeping critical details.**」
- 原文（`/clear`）：「Reset the visible UI and chat context together when you want a fresh start.」「`/new`：Start a new chat inside the same CLI session... Reset the chat context without leaving the CLI.」
- 原文（`/goal`）：「Give Codex **a persistent target to track while a larger task runs.**」
- 原文（配置项，源码级线索）：
  - `model_auto_compact_token_limit`：「Token threshold that triggers automatic history compaction (unset uses model defaults).」
  - `model_auto_compact_token_limit_scope`：「Controls whether the auto-compaction threshold counts the full active context (`total`, the default) or only growth after the carried compaction-window prefix (`body_after_prefix`).」——即压缩前缀被「携带」（carried）进新窗口，阈值只算前缀之后新增部分。
  - `compact_prompt`：「Inline override for the history compaction prompt.」
  - Hook 事件含 `PreCompact` / `PostCompact`——压缩是生命周期一等事件。
- 官方 long-running-work 文档（https://learn.chatgpt.com/docs/long-running-work）：「**Use separate chats when independent tasks can run in parallel**, and avoid giving two tasks write access to the same connected source.」——任务=会话的边界原则。

### 3.3 Google Gemini CLI

来源：https://geminicli.com/docs/reference/commands 与 https://geminicli.com/docs/cli/rewind

- 原文（`/compress`）：「**Replace the entire chat context with a summary.** This saves on tokens used for future tasks while retaining a high level summary of what has happened.」
- 原文（`/clear`）：「Clear the agent conversation context (active conversation history) and start a new session.」
- 原文（`/rewind`，Key considerations）：「**Compression: Rewind works across chat compression points** by reconstructing the history from stored session data.」
  —— 证明 Gemini CLI 存在自动「chat compression points」，且压缩后的可回放性由会话存储保证。
- 归纳：Gemini CLI 亦有 `/resume`（恢复历史会话）、`/restore`（还原文件），与「按会话隔离上下文」同一模式；未在其现行文档站找到自动压缩阈值的专门页面（旧 auto-compact 文档页已下架）。

### 3.4 ChatGPT / Codex 跨会话记忆

来源：https://learn.chatgpt.com/docs/customization/memories 与 https://learn.chatgpt.com/docs/personalize

- 原文（memories 如何生成）：「After you enable memories, Codex can turn useful context from eligible prior chats into local memory files. **Codex skips active or short-lived sessions**, redacts secrets from generated memory fields, and **updates memories in the background instead of immediately at the end of every chat.**」
- 原文：「Codex waits until a chat has been idle long enough **to avoid summarizing work that's still in progress.**」
- 归纳：ChatGPT 系的做法是「会话内历史随窗口增长被 /compact 压缩；跨会话知识经后台离线摘要进入 memories，且有意不即时、不覆盖进行中会话」。即：**跨会话记忆是异步下采样，不是把上一个 run 的原始消息原样塞进下一个 run**——与 Clawith「几十条群历史原样进新 run」形成对照。

---

## 4. 失败通知/系统事件进入后续上下文的实践

### 4.1 Claude Code：hook 注入内容与系统事件的压缩命运

来源：https://code.claude.com/docs/en/context-window

- 原文（「What survives compaction」表中）：「Context that hooks added earlier: **Summarized with the rest of the conversation**」；「SessionStart hooks that match the compact source: Claude Code runs them and adds their output to the compacted context.」
  - 归纳：Claude Code 明确把「会话中途注入的系统内容」（hook 输出、错误通知等）与普通对话一同摘要化——事件不原样存活，只以摘要残影存在；需要重新生效的系统内容由 SessionStart hook 在压缩后重放。
- 原文（Anthropic 服务端压缩，见 1.2）：「The API automatically drops all content blocks prior to the compaction block」——压缩块之前的所有内容（包括任何系统通知）被丢弃，只由摘要代表。

### 4.2 OpenHands：condensation（事件历史压缩的原型实现）

来源（源码，tag 0.51.0，主分支已重构为 TS，Python 版即本路径）：
- https://github.com/OpenHands/OpenHands/blob/0.51.0/openhands/memory/condenser/condenser.py
- https://github.com/OpenHands/OpenHands/blob/0.51.0/openhands/memory/condenser/impl/llm_summarizing_condenser.py
- https://github.com/OpenHands/OpenHands/blob/0.51.0/openhands/memory/condenser/impl/recent_events_condenser.py
- https://github.com/OpenHands/OpenHands/blob/0.51.0/openhands/memory/condenser/impl/conversation_window_condenser.py

- 原文（condenser.py 文档串）：「Condensers take a list of `Event` objects and reduce them into a potentially smaller list... Agents... call the `condensed_history` method... and use the results instead of the full history.」
- 原文（llm_summarizing_condenser.py 摘要提示词——事件如何降级为摘要）：
  ```
  Track:
  USER_CONTEXT: (Preserve essential user requirements, goals, and clarifications in concise form)
  COMPLETED: (Tasks completed so far, with brief results)
  PENDING: (Tasks that still need to be done)
  CURRENT_STATE: (Current variables, data structures, or relevant state)
  ```
  被遗忘的事件范围由 `CondensationAction` 携带（`forgotten_events_start_id/end_id` + `summary`），在历史中以一条 `AgentCondensationObservation` 取代该区间——**中间事件（含任何失败/通知类事件）整体降级为一条摘要事件**。
- 原文（recent_events_condenser.py）：`RecentEventsCondenser(keep_first: int = 1, max_events: int = 10)`——头部保 1 条、尾部保最近 10 条，中间全部丢弃。
- 原文（conversation_window_condenser.py，归纳）：区分「essential events」（必须保留的事件）与可遗忘事件，保留 essential + 最近一半。
  —— 这给出了「系统事件降级」的通用形态：**不是逐条改写，而是按区间压缩 + 显式保留少数 essential 事件**。Clawith 的「reconciliation_required 通知」若要保留语义，应进 essential 集合或摘要，而非原样横幅。

### 4.3 LangGraph：显式删除消息（含系统消息）

来源：https://docs.langchain.com/oss/python/langgraph/add-memory

- 原文：`RemoveMessage(id=m.id)` 可删任意指定消息（含系统通知类消息）；删除后须保证历史合法性（见 1.3 的 provider 约束）。
- 归纳：框架层支持「从 state 里精准摘除某类消息」，D2 在 LangGraph 上无技术障碍；但框架不负责「降级为摘要」——摘要化需在删除前用模型生成（LangGraph 官方模式见 1.3 summarize_conversation）。

### 4.4 直接先例的诚实结论

- **未找到**「失败通知→时间戳+一句摘要」这一精确形态的官方一手文档（Anthropic/OpenAI/Google 官方文档均未出现针对失败通知的专门降级指南）。
- 最接近的先例是：①Claude Code 把 hook 注入的系统内容与对话一起摘要化（4.1）；②OpenHands 把事件区间整体压缩为结构化摘要、仅显式保留 essential 事件（4.2）；③Anthropic 服务端压缩把压缩块之前的所有内容（含任何通知）一律替换为摘要（4.2 同引）。
- 另有一个「失败历史有害」的官方侧证：Anthropic Claude Code best-practices（见 3.1）「corrected Claude more than twice... context is cluttered with failed approaches → /clear」。

---

## 5. 对 D1/D2/D3 的证据支持度小结

| 方向 | 支持它的资料条目 | 反对/风险条目 |
|---|---|---|
| **D1：续接文案/末尾控制消息加「当前指令短引用」（任务锚点）** | ① OpenAI 提示工程文档：`instructions` 仅作用于当前请求，跨轮必须显式重注入（2.4，原文）；② Anthropic 长上下文技巧：「Queries at the end can improve response quality by up to 30%」（2.3，原文）——锚点放末尾有位置优势；③ Anthropic 压缩文档 `pause_after_compaction` 专门用于压缩后追加「instruction-oriented messages」（1.2，原文）；④ Codex `/goal`：目标作为持久字段挂在会话上（2.5，原文）；⑤ OpenHands 压缩提示词首节 `USER_CONTEXT: Preserve essential user requirements, goals...`（4.2，原文）；⑥ Claude Code memory 页：「instruction disappeared after compaction... given only in conversation」（2.5，原文）——锚点必须放重注入层；⑦ ChatGPT/Codex memories：「AGENTS.md... not as the only source for rules that must always apply」（2.5，原文） | ① Claude Code「What survives compaction」：会话内给的指令会被摘要化吞掉——D1 若只加在续接文案（属于消息历史）里，将来被压缩时同样会丢（2.5）；正确落点是 system 层或压缩后重注入层；② 每次模型步都重放长指令会消耗 token/注意力预算（1.1「attention budget」）——锚点应是「短引用」而非全文重放；③ OpenAI 压缩项是不透明 token（1.5），锚点不能指望压缩器自动保留，必须在应用层显式放置 |
| **D2：失败 run 终止通知降级（时间戳+摘要/标记为历史）** | ① Claude Code context-window 页：hook 注入的系统内容在压缩时「Summarized with the rest of the conversation」（4.1，原文）——系统事件与对话同等摘要化是既定形态；② Anthropic 服务端压缩：「drops all content blocks prior to the compaction block」（1.2，原文）——旧系统通知一律被摘要取代；③ OpenHands：事件区间压缩为一条 `CondensationObservation`，仅显式保留 essential 事件（4.2，原文）——「保留语义用摘要、不保留原样横幅」；④ Claude Code best-practices：「context is cluttered with failed approaches → /clear」（3.1，原文）——失败类历史有实测有害性陈述；⑤ LangGraph `RemoveMessage` 支持精准删除某类消息（4.3，原文） | ① **未找到「失败通知→时间戳+一句摘要」的精确官方先例**（4.4），属合理推断而非直接背书；② OpenHands conversation_window_condenser 保留「essential events」——若 reconciliation 类通知本身是下游必需信号（如 run 结算触发），完全删除有功能风险，需先确认消费方；③ LangGraph 文档警告：删消息必须保持 provider 要求的消息结构合法（1.3，原文） |
| **D3：recent_session_messages 按 run 边界降级/截断（旧问答对不进新 run 窗口）** | ① LangChain 官方：「most LLMs still perform poorly over long contexts. They get "distracted" by stale or off-topic content」（1.4，原文）——D3 最直接的官方动机陈述；② LangGraph `trim_messages(strategy="last", start_on="human", end_on=("human","tool"))`（1.3，原文）——现成截断机制与边界对齐参数；③ OpenAI compaction 文档：「you can drop items that came before the most recent compaction item」（1.5，原文）——旧内容可安全丢弃、由压缩项承载；④ Claude Code「/clear between tasks: Old conversation crowds out...」（3.1，原文）；Codex「Use separate chats when independent tasks can run in parallel」（3.2，原文）；⑤ Anthropic「Starting fresh versus compacting」（2.3，原文）——新任务宁开新窗不压缩；⑥ ChatGPT memories：「Codex skips active or short-lived sessions」+ 后台异步摘要（3.4，原文）——跨会话知识经下采样进入，非原样注入 | ① LangGraph 官方同时警告：「trimming or removing messages... you may lose information」——纯截断有信息损失，官方更推荐「摘要替代」（1.3，原文）；② OpenAI cookbook：「Preserve enough working state for the next phase to make sense」（1.6，原文）——截断不得伤及跨 run 需要的状态（如 Clawith 的 run 结算/上下文）；③ 用户可能期望跨 run 记忆（ChatGPT 记忆、OpenHands 摘要都刻意保留跨会话知识）——D3 应与「按 run 生成滚动摘要」结合，而非无脑清空；④ 截断边界必须对齐消息结构（同 D2 风险③） |

### 综合结论（供选型参考，非本文任务但直接由证据导出）

1. 三方向都有充分的一手证据支持，且**互不替代**：D1 解决「锚点在哪」；D2 解决「系统事件如何进新 run」；D3 解决「旧问答对的体积与干扰」。
2. 证据最强的组合是「**D1（system 层/每 run 首尾显式重放当前指令短引用）+ D3（旧历史按 run 边界截断，并用滚动摘要承接必要状态）+ D2（失败通知降级为摘要条目，确需保语义的进 essential 白名单）**」——对应 Anthropic 压缩文档的「摘要 + pause 后重注入指令」、OpenAI 的「压缩项 + 丢弃旧项」、OpenHands 的「USER_CONTEXT 摘要 + essential events」三套官方机制。
3. 唯一需要工程验证而非文档背书的是 D2 的精确形态（时间戳+一句摘要），无官方先例；其余均有官方文档或官方产品行为背书。
