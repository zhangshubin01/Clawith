---
title: "上下文瘦身：Clawith vs deepagents 三机制对标（诚实反转）"
status: ready-for-review
created: 2026-09-05
---

# 上下文瘦身：Clawith vs deepagents 三机制对标

## 结论先行（诚实反转）

原设想「按 deepagents 三机制（`memory.py` cache_control / `summarization.py` truncate_args / `_message_eviction.py`）出一版移植方案」，在逐文件核对 Clawith 真实代码后**被推翻**：

> **Clawith 已具备这三机制的全部能力，且在多数维度更先进。真正的缺口不是「移植三机制」，而是两处 deepagents 同样没有、或不可照搬的东西：**
> 1. **重复工具调用熔断**——deepagents 也没有；Clawith 已有 `arguments_hash` 列但零熔断器。→ 已产出实现 spec [[20260905-read-dedup-stall-guard]]。
> 2. **DeepSeek 缓存边界重估**——deepagents 的 `cache_control` 是 Anthropic 专属断点，DeepSeek 无手动断点（命中 = 前缀逐字节一致），不可照搬。→ 独立性能票，见 §5.2。

本章不写「移植」，只做对照 + 指出真缺口，避免把已被证伪的「抄 deepagents」当成方向。

## 1. 三机制对照总表

| deepagents 机制（`libs/deepagents/deepagents/middleware/`） | deepagents 实现 | Clawith 现状（代码级） | 裁决 |
|---|---|---|---|
| `memory.py` `add_cache_control` | AGENTS.md 全量注入 + 打 `cache_control:{"type":"ephemeral"}` 第二断点隔离变化块 | `client.py:701/746` 已有 `cache_control:{"type":"ephemeral"}`；`model_step_service.py:786` `prefix_fp` 指纹覆盖首个 `prefix_cache_break` 之前的消息、`msg_chain` 逐消息定位破坏 KV cache 的早期消息 | **Clawith 更先进**（有指纹定位）；但 cache_control 对 DeepSeek 无效 → 缺口② |
| `summarization.py` `truncate_args` | 完整压缩前对旧 `write_file`/`edit_file` 参数截断（留前 20 字符 + `...(argument truncated)`） | `compression_config.py` Tier1 STRICT_TOOLS（read/write/edit/retrieve **verbatim 不截断**）+ Tier2 LOSSLESS + fold 0.60/0.50 + Layer1 0.85 + `PreRoundBudget` 决策管道；`run_compactor.py` 8 节结构化摘要 + 原子 Tool Exchange 边界 | **Clawith 更先进**（分层无损压缩管线 vs 单点参数截断） |
| `_message_eviction.py` | 超大工具结果 → backend 文件 → head+tail 各 5 行预览 + 文件路径 + `read_file` offset/limit 指引 | `tool_result_store.py` `ToolResultStore`（超大结果 → 私有对象存储，`summary_truncated` + `result_ref` 元数据）；字节级兜底截断 `AGENT_RUNTIME_TOOL_RESULT_INLINE_MAX_BYTES` | **等价 / 更先进**（对象存储 + ref 而非文件 dump） |

三者全部「已具备」，无一需要移植。下面是逐机制细节与唯一两处真缺口的论证。

## 2. 机制一：cache_control —— Clawith 已有，且「断点」对 DeepSeek 无效（缺口②）

- **deepagents 做法**：`memory.py` 把 AGENTS.md 全量注入（仅剥 HTML 注释，无上限无 recency），并用 `cache_control:{"type":"ephemeral"}` 打第二断点隔离「每步变化的块」，让稳定前缀不被后续变化块污染。
- **Clawith 现状（更先进）**：
  - `client.py:253` 已有 `prefix_cache_break: bool` 字段；`:701`/`:746` 已产出 `cache_control:{"type":"ephemeral"}`；`:739` `_with_cache_control_on_message` 已实现。
  - `model_step_service.py:786` 的 `_cache_fingerprints` 返回 `(prefix_fp, full_fp, tools_fp, msg_chain)`：`prefix_fp` 覆盖**首个 `prefix_cache_break` 之前**的消息，`msg_chain` 逐消息哈希，能**定位是哪条早期消息破坏了 KV cache**——deepagents 只有「打断点」，没有「指纹定位破坏者」。
- **为什么不可照搬（缺口②的本质）**：
  - deepagents 的 `cache_control` 是 **Anthropic 专属**断点语义。DeepSeek **没有手动断点**——命中规则是「后续请求与某个 cache prefix unit **逐字节全量匹配**」（见 [[20260826-context-slimming-best-practices]] §1.3）。
  - 因此 Clawith 唯一的杠杆是「稳定前缀**绝对不变** + 动态块排尾」，依赖 DeepSeek 公共前缀持久化（需 2+ 次请求才生效、best-effort）。
  - 而 `model_step_service.py:1531` 当前对每轮动态块打 `prefix_cache_break=True`，**每步强制 prefix 失效**——09-01 分析实测日志 `[Token Cache] Low hit rate ratio=100%` 高频出现，即缺口②的现场。
- **结论**：机制本身 Clawith 已有且更先进；真正的活是「DeepSeek 缓存边界重估」（§5.2），不是移植 cache_control。

## 3. 机制二：truncate_args —— Clawith 分层压缩管线更先进

- **deepagents 做法**：`summarization.py` 的 `truncate_args_settings` 在完整压缩前对**出窗旧消息**的 `write_file`/`edit_file` 参数做确定性截断（留前 20 字符），「often reclaiming enough context to skip summarizing」——本质是「最轻量压缩优先」。
- **Clawith 现状（更先进）**：
  - `compression_config.py` 有完整分层：`TIER1_STRICT_TOOLS`（`:15`，read/write/edit/retrieve **verbatim 不截断**）、`TIER2_LOSSLESS_TOOLS`（`:27`，list/find/search 仅无损）、fold 高低水位 `0.60/0.50`（`:211-212`）、Layer1 emergency `0.85`（`:213`）、`adaptive_min_ratio`（`:106`）、`PreRoundBudget` 决策管道（`:191`）。
  - `run_compactor.py` 的 Thread Compact：8 节结构化摘要（Primary Request and Intent / Key Technical Concepts / Files and Code / Errors and Fixes / Pending Jobs / Next Step / Critical Context，`:65-86`），空节写 `(none)`、**绝不丢节**（`:62-63`），原子 Tool Exchange 边界。
  - 另有 `lossless_compaction.py` / `smart_crusher.py` / `content_router.py` / `context_compressor.py` 等模块。
- **结论**：deepagents 的「对旧参数截断」是 Clawith Tier2 无损压缩的一个子集；Clawith 用「分层 + 水位 + 预算」替代了「单点截断」，无需移植。

## 4. 机制三：_message_eviction —— Clawith ToolResultStore 等价 / 更先进

- **deepagents 做法**：逐工具调用，结果超阈值 → 全文写 backend 文件 → 消息替换为 head+tail 各 5 行预览 + 文件路径 + 「用 `read_file` offset/limit 分批读」指引，保留 tool_call_id 等身份字段。
- **Clawith 现状（等价 / 更先进）**：
  - `tool_result_store.py` `ToolResultStore`（`:194`）：超大工具结果 → **私有对象存储**，消息侧只留 `summary_truncated`（`:39`）+ `result_ref`（`:207`）元数据，模型可回读。
  - 字节级兜底：`AGENT_RUNTIME_TOOL_RESULT_INLINE_MAX_BYTES`（默认 8192）超限截断 + `summary_truncated` 标记。
- **结论**：形态等价（大结果外置 + 预览 + 回读指引），Clawith 用对象存储 + ref 元数据，比 deepagents 的裸文件 dump 更干净；无需移植。

## 5. 两个真正缺口

### 5.1 缺口①：重复工具调用熔断（deepagents 也没有）

- **证据**：09-01 复盘 `be39c1ad`「做 1、2、3、5」——124 步 LLM、232 次 `read_file` 中 **159 次重复**（12 个文件各读 7–15 次），`deepseek-v4-flash` + `model_turn_limit=10000`，最终被取消，$1.15。
- **现状**：`agent_tool_execution.py:81` 已有 `arguments_hash` 列（`String(128)`），但**全仓无任何熔断器**——它只被飞书审批 `feishu_approval_authorization.py` 用于 args 去重，从不用于「同 run 重复调用检测」。讽刺的是 `context_compressor.py:948` 还躺着一份 `_dedup_file_tool_results` 死代码（无调用）。
- **解决方案**：已单独产出 spec [[20260905-read-dedup-stall-guard]]——段级 `(path, content_hash)` 去重（N=3）+ 滑动窗口空转熔断（占比 70%）+ 读/写生命周期清单，全部只读检测、零副作用，纯函数判定层 `ReadDedupDecider`。**注意**：该 spec 的核心洞察与本方案一致——Clawith 不是缺基建，是缺熔断层；它进一步用**更精确的段级 `content_hash`（对返回内容的哈希）**作去重键，而非直接复用 `arguments_hash`（对调用参数的哈希），因为「同一批文件反复读、内容从未变」正是 `read_file` 类浪费的要害。
- 对应 09-01 分析 #3（P1）。

### 5.2 缺口②：DeepSeek 缓存边界重估（deepagents 的 cache_control 不可照搬）

- **问题**：`model_step_service.py:1531` 每轮动态块 `prefix_cache_break=True` 使 DeepSeek 前缀缓存每步失效（09-01 实测 `ratio=100%`），成本翻倍、每步更慢、模型更易失忆。
- **重估方向（不与 deepagents 挂钩）**：
  1. 动态块是否必须放在 `prefix_cache_break` 之后？能否把「每步不变的稳定部分」提进缓存边界、把「每步变化的动态块」整体排尾，使 `prefix_fp` 覆盖的稳定前缀逐字节一致（对齐 [[20260826-context-slimming-best-practices]] §1.3/§1.5 的「稳定块绝对不变 + 动态块排尾」）。
  2. 动态块能否**尽量不内联进消息**：走工具/检索（OpenAI `defer_loading`、Letta `system/` 按需读取），而非每条消息内联 1.8–5.3K。
  3. 上线后用 `prompt_cache_hit_tokens` / `prompt_cache_miss_tokens` 实测回归，不假设命中（DeepSeek best-effort）。
- 对应 09-01 分析 #5（memory/reflections 增量注入 + 重新评估 prefix_cache_break）的缓存边界半部。

## 6. 与 09-01 分析 P0/P1 未竟项对照

| 09-01 项 | 内容 | 状态 | 与本方案关系 |
|---|---|---|---|
| #1 (P0) | execute_code 沙箱发布路径纳入 workspace manifest 刷新（消除 WorkspaceFlushConflict） | 未竟 | 独立，正确性 bug，非瘦身域 |
| #2 (P0) | ~~turn_limit 降为 60–100~~ | **已拒（用户决定）** | 维持 10000，由缺口①空转熔断兜底 |
| #3 (P1) | 重复工具调用熔断 | **→ 缺口①**，已出 spec | 本方案 5.1 |
| #4 (P1) | android_compile gradlew 权限 + 多 task 参数校验 | 未竟 | 独立，工具侧 bug |
| #5 (P1) | memory/reflections 增量注入 + 重新评估 prefix_cache_break | **→ 缺口②**（缓存边界半部） | 本方案 5.2 |
| #6 (P2) | Android 类 agent 模型路由（v4-flash → 更强） | 未竟，需用户拍板 | 直接缓解失忆，降低缺口①触发率 |

**注意**：G1'（reflections 注入瘦身）review 后已确认——「reflections 16K+ 字符」根因不成立（`7aca03f8` 起就有 2000 截断，真实 reflections.md 最大 1387B），G1' 的 recency 部分近乎 YAGNI，唯一真实收益是模板噪音剔除。故 #5 的「memory/reflections 增量」分量应重新评估，勿把被证伪的「16K reflections」当依据。

## 7. 结论与下一步

1. **不移植 deepagents 三机制**：对照表（§1）+ 逐机制（§2–4）证明 Clawith 压缩/缓存/工具结果基建已 ≥ deepagents。
2. **缺口①（重复调用熔断）**：已落地为 spec [[20260905-read-dedup-stall-guard]]，可直接进入实现评审。
3. **缺口②（DeepSeek 缓存边界）**：独立性能票，需先做 `prefix_cache_break` 动态块定位实验（哪些内容每步变、能否外置/排尾），再定方案。
4. **审核纪律**：按 [[plans-compare-reference-materials]]，本方案需过一轮 grill-me 或 code-review 再定稿；代码级改动前继续 read_file 核对真实函数名/行号。

## 参考

- [[20260826-context-slimming-best-practices]]（Anthropic/OpenAI/DeepSeek 缓存与压缩一手结论）
- [[20260905-read-dedup-stall-guard]]（缺口①实现 spec）
- [[20260904-deepagents-in-action-study]]（课程层对标）
- `docs/analysis/2026-09-01-task-step-inflation.md`（be39c1ad 复盘 + P0/P1 未竟项）
- `docs/analysis/2026-09-04-compaction-reference-implementation-comparison.md`（dsh/deepagents/mem0 代码级对比，compaction-progress-anchor 线程）
- deepagents 0.7.13 源码：`libs/deepagents/deepagents/middleware/{memory,summarization,_message_eviction}.py`
