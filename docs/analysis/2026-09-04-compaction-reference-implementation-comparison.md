# 参考资料压缩/进度锚点实现对比（代码级，2026-09-04）

- 目的：把「其他项目怎么实现」落实到真实源码，逐项与 `docs/technical-plans/20260903-compaction-progress-anchor.md`（D/A/熔断）对比。
- 实读范围（本地源码，非文档转述）：deepseek-harness（`packages/compaction/*` 四包 + summarizer 全文）、deepagents `middleware/{summarization,_message_eviction}.py`、langchain v1 `agents/middleware/summarization.py`（deepagents 的实际摘要引擎）、how_to_fix_your_context 04/05、mem0 `memory/main.py`（ADD 语义）。OpenHands 本地镜像只剩前端 monorepo（Python 核心不在），不入表。

## 1. 各项目实现事实

### 1.1 deepseek-harness（dsh）—— 三层：pruner → 阈值 → 摘要

**ToolResultPruner（确定性、无模型，`compaction-tool-result-pruner/src/index.ts`）**
- 只对当前 surface 上的 `tool/result` 事件做 head(4096)/marker/tail(1024) 截断，阈值 8192 code points；按 Unicode code point 切片不劈代理对；非文本块原样保留。
- 落点=session 事件流 `append('tool/result', …, {surfaceOp:'replace'})`——**append-only 日志 + surface 投影替换**，原事件不可变，重放靠 `sourceEventSeqs` 恢复替换输入；同步追加 `compaction/prune` shadow-price 事件给 token 计量做减法。
- 无状态判定：**只看尺寸**，不看「该工具是否完成」。

**BasicCompactionEngine（`compaction-basic/src/index.ts` 的 `compactIfNeeded`）**
- pressure 触发 → 先 `pruneSession`（若有 pruner）→ 重新计量 → **裁完低于阈值就直接 return null（跳过摘要）**；仍超才选 `selectCompactableRange(…, retainTokens)` → 摘要替换 → 再计量 → 超阈值则重试至 `compactionRetries` 后抛错。
- overflow 触发（provider 确认 CONTEXT_WINDOW_EXCEEDED）→ prune → 强制以 retain=0 选范围摘要。
- 范围边界要求 **tool-pairing 平衡**（`toolPairingBalancedBefore/After`：assistant tool_calls 必须与结果同边）；并发用 durable `compaction/start` 标记做压缩锁；错误分类 busy/cancelled/changed/summary/commit/persistence。

**summarizer.ts（摘要调用与指令）**
- **摘要调用逐字节重放上次路由请求的 system+tools+消息前缀**，压缩指令作为最后一条 user 消息追加——辅助调用是上次请求的真前缀，吃 provider KV cache（对照 Clawith run_compactor 摘要 cache_read=256 全灭）。
- 指令=固定 8 section 模板（Primary Request and Intent / Key Technical Concepts / Files and Code / Errors and Fixes / Pending Jobs / Current Work / Next Step / Critical Context）：terse bullets、空节写 `(none)`、**绝不丢节**、旧 checkpoint「不得逐字抄写，保留仍真的事实、合并新信息」、禁止提及压缩本身。
- 落点=合成 user 消息，包 `CHECKPOINT_PREAMBLE`（「这是自动生成的 checkpoint…把它当作既定背景…直接继续，不要复述或提及」）+ `<compacted-summary>` 标签；maxTokens 8192，截断=失败。

### 1.2 deepagents / langchain —— 读时投影双层

**核心引擎是 langchain v1 的 `SummarizationMiddleware`（deepagents 只加 offload 与参数截断）**
- 触发：默认 `("fraction", 0.85)`（模型 profile 的 max_input_tokens ×0.85），保留 `("fraction", 0.10)` 或最近 20 条；无 profile 时保守 tokens=170000/keep 6。
- cutoff：token 预算二分 + `_find_safe_cutoff`——**从不劈开 AI/Tool 配对**（孤儿 ToolMessage 会把 cutoff 前移到其 AIMessage）。
- 摘要输入：被裁消息 trim 到 4000 token 后 **XML 序列化全文**喂给摘要模型；指令 4 section（SESSION INTENT/SUMMARY/ARTIFACTS/NEXT STEPS），且明文写「ensure you don't repeat any actions you've already completed」。
- 落点：摘要=带 `lc_source="summarization"` 的 HumanMessage；**state 保留全量消息，每步读时投影** `[summary] + messages[cutoff:]`（`_apply_event_to_messages`）。
- deepagents 增强：①摘要前把被裁消息 **offload 到文件**（`conversation_history/{session}.md` 追加，媒体单独哈希落盘）；②**truncate_args**——预摘要的确定性步骤：对 keep 窗口外的 `write_file/edit_file` 工具调用参数超长（>2000 字符）只留前 20 字符+`...(argument truncated)`。

**deepagents `_message_eviction.py`（超大工具结果驱逐）**
- 逐工具调用（非窗口）：结果超阈值 → 全文写 backend 文件 → 消息替换为 head+tail 各 5 行预览+文件路径+「用 read_file 带 offset/limit 分批读」指引；保留 tool_call_id/name/id/status 等身份字段。

### 1.3 mem0 —— ADD-only 事实累积
- `add` 产出 `event: "ADD"`；增量抽取提示词要求**只追加新事实、不覆盖既有**；与 Clawith A（completed_actions 只追加不重写）同语义，但 mem0 面向跨会话通用记忆，非 run 内工具账本。

### 1.4 how_to_fix_your_context 04/05
- 04 pruning=RAG 检索代替全量装载（pruning 即检索）；05=用小模型（gpt-4o-mini）摘要工具输出再入上下文。实践层佐证：**裁的是「模型看到的」，不是账本**。

## 2. 与 Clawith 方案（D/A/熔断）的逐项对比

| 维度 | dsh | deepagents/langchain | Clawith 现状 | Clawith 方案（D/A/熔断） |
|---|---|---|---|---|
| 确定性裁剪时机 | 压缩触发时 | 摘要触发时（truncate_args）/ 工具摄入时（eviction） | 无（结果全量保留，`omitted_tool_exchanges` 仅预算溢出附注） | **每步结算**（离开 recent 窗口即替换）——参考系中唯一每步做 |
| 裁剪判据 | 尺寸 >8192 chars | 尺寸（消息数/arg 长度） | — | **完成状态**（status 终态）+ 出窗，账本为事实源 |
| 落点 | append-only 事件流 surface 替换 | 消息级替换（eviction 写 checkpoint 消息；truncate_args 读时改 copy） | — | **写 checkpoint 块级原子替换**（tool_calls+结果同帧→单 user 合成消息，id 复用 `message_ids[-1]`） |
| 配对安全 | `toolPairingBalanced*` 强制边界平衡 | `_find_safe_cutoff` 不劈 AI/Tool 对 | compactor 已有 block 语义 | 评审 Q1 已定：整块替换，杜绝 `retry_model` 重发坑（`tool_exchange.py:525/814`） |
| 摘要输入 | 被裁消息全文重放（prune 后） | XML 全文（trim 4000） | **tool_exchange 被替换为 ledger 一句话（edit_file 仅「Replaced N occurrence(s)」）** ← 失忆循环根因 | 不改变摘要输入形态本身（B 条件项才升级内容），改由 A 注入权威事实 |
| 进度事实注入 | 无（靠旧 checkpoint 合并规则） | 无（靠指令「don't repeat」+全文输入） | 无 | **A：completed_actions（ledger 构造、ADD-only、50 条/2KB）**——参考系唯一确定性注入 |
| 摘要后压力复查 | 裁完重计量，低于阈值跳过摘要 | 无（触发即摘要） | 无 | 无（D 常态化降低触发频率，间接同效） |
| 循环检测 | 无（只有压缩重试上限） | 无 | 无 | **熔断：prefix 相邻重现+间隔真实压缩+工具哈希序列相同，逐次计数**——参考系中无先例，属新设计 |
| 摘要落帧措辞 | CHECKPOINT_PREAMBLE「既定背景、直接继续」 | 「Here is a summary…」 | thread_summary + covered_messages | 注意 [[direct-chat-run-boundary-fix]]：user 角色摘要措辞禁祈使——与 dsh preamble 设计一致方向 |

## 3. 对方案的增量结论（可并入实施）

1. **dsh「裁完重计量、低于阈值跳过摘要」值得抄到 D**：D 每步结算后，`compact_if_needed` 入口可先重计量——若结算已把上下文压回阈值下，跳过本次摘要。零新机制（现成计量函数），直接减少摘要次数。
2. **dsh 摘要调用吃前缀缓存的思路对 Clawith 摘要成本有意义**（现状 cache_read=256 全灭）；但 Clawith 摘要走独立 prompt 组装，对齐成本高，列为后续优化非本票。
3. **deepagents truncate_args 是 B（内容级摘要）的确定性廉价替代**：对出窗旧消息的 edit_file 类参数只留前 20 字符+标记，无 LLM 调用。Clawith 撞点仍是 `_short_result` 500 字符截断发生在摄入时、原始参数在 `sanitized_arguments`/`workspace_file_revisions`——若 B 触发，优先评估确定性截断而非 LLM 摘要。
4. **deepagents 消息级 eviction 的「替换消息保留身份字段」与我们的合成消息约束一致**（id/tool_call_id 等必须保留），实为通用纪律。
5. 参考系共同点再次确认根因诊断：**所有实现都让摘要模型看到真实内容（全文或 head/tail 预览），只有 Clawith 给一句话 ledger 摘要**——输入缺事实是 Clawith 独有形态，A/D 直击要害。
