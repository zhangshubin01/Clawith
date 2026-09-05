---
title: "read-dedup 与空转熔断护栏（Read-Dedup & Stall Guard）"
status: ready-for-agent
created: 2026-09-05
---

# Spec: read-dedup 与空转熔断护栏

## Problem Statement

**用户（平台运维与终端用户）面临的真实问题**：一个 agent run 会陷入「反复读取同一批文件、内容却从未变化」的无进展循环，烧掉大量 token 与时间，直到被用户手动取消。

复盘对象 run `14ba5535-7d34-440e-a7e7-a11fbfb5a918`（Android 工程师 07，goal=「实施优化项授权清单」）的硬证据：

- 413 次工具调用中 83% 是 `read_file`，其中约 226 次是**重复读取内容从未变化的段落**（MainActivity.kt 44 读/1 个内容哈希、CalculatorSettings.kt 42/1、strings.xml 38/1、values-en 34/1、CalculatorViewModel.kt 30/1 等）。
- 模型为 `deepseek-v4-flash`（无 context_window_tokens 配置，弱工作记忆）。
- 压缩是**摘要式**的：按 80% 水位触发，`RemoveMessage(REMOVE_ALL_MESSAGES)` 清空历史 + 注入 ≤8K 摘要 + 保留 ≤8K 最近消息，**丢失文件正文**。实测压缩后模型被迫重读文件，上下文再膨胀（step115 28.3K → step116 9.6K → step119 26.7K），形成「压缩 → 重读 → 再膨胀」循环。
- 已存在一段 `_dedup_file_tool_results` 去重逻辑，但**全仓无调用**（死代码）。
- `max_tool_rounds` / `model_turn_limit` 默认 10000（API 层 clamp 到 10000），本 run 只跑到 123 步就被用户手动取消——**总轮数护栏对「低进展、不报错」的空转完全不构成约束**。

**核心缺失**：没有「进展感知」的护栏。总轮数上限（10000）是给长任务留的物理兜底，拦不住「有轮数、无进展」的空转；同文件重复读也没有任何去重或熔断。

## Solution

**用户视角**：在不砍长任务天花板的前提下，补上长任务的「地板」——让该跑上千步的大任务能跑完，让跑百余步就空转的 run 尽早自行收敛，而不是等用户手动取消。

方案由三个正交的护栏组成，统一建立在一张 `(path, content_hash)` **段级**去重表之上：

1. **单文件软去重**：同一 `(path, content_hash)` 键重复读取达到阈值时，后续读取不再把重复全文喂给模型，改返回「内容未变」占位。
2. **窗口空转熔断**：滑动窗口内「重复读」占比超过阈值时，判定为空转，触发分级收敛动作（提醒 → 强制压缩 → 终止）。
3. **去重表生命周期**：去重表跟随压缩生命周期演进——短期在压缩落地时注入「已读文件清单摘要」，长期升级为跨压缩周期持久的「读/写生命周期清单」。

`max_tool_rounds` / `model_turn_limit` 维持现状（默认 10000）**不变**——它是防真·死循环的物理兜底，服务于长任务；空转由新护栏负责拦截。

## User Stories

1. As a 平台运维，I want 同一文件段在内容未变化时的重复 read_file 被去重，so that 空转 run 不再重复烧 token 与时间。
2. As a 平台运维，I want run 在滑动窗口内陷入空转时自动触发收敛，so that 不必等到用户手动取消才发现问题。
3. As a 终端用户，I want 合法的长任务不被误杀，so that 复杂任务（大重构、多文件实现）能一口气跑完。
4. As a 平台运维，I want 压缩落地后模型能拿到「已读文件清单摘要」，so that 压缩后的模型不必重新读取它已经读过的文件。
5. As a 租户管理员，I want 去重阈值、空转窗口、占比阈值可配置，so that 不同租户的任务复杂度差异可以各自调优。
6. As a 开发者，I want 去重与空转判定是纯函数、无副作用，so that 行为可以被单元测试完整覆盖、可靠回归。
7. As a 平台运维，I want 去重记录在文件被写入后自动失效，so that 「写后校验」的合法重读不会被误拦。
8. As a 平台运维，I want 读不同段落（offset 递进）的大文件分段读取不被误伤，so that 大文件分多次读是正常推进而非空转。
9. As a 平台运维，I want 去重只对「返回给模型的内容」去重、对「文件整体是否变化」另行判断，so that 两种 hash 语义不互相污染导致误判。
10. As a 终端用户，I want 空转收敛是分级递进的（先提醒、再压缩、最后终止），so that agent 有机会自行回到正轨而不是被一刀切杀死。
11. As a 开发者，I want 空转熔断的可观测信号（判定结果、收敛动作、去重计数）写入既有账本/追踪，so that 事后复盘有据可查。
12. As a 平台运维，I want 去重表不产生指向已删内容的活动引用，so that 压缩后不会出现死引用或误导性的「重复读」标注。

## Implementation Decisions

### 决策一：去重键为段级 `(path, content_hash)`，N=3

- 去重键 = 文件路径 + **本次返回内容（段头 + 段内容）的哈希**，而非整文件哈希。`content_hash` 语义为「这一次读返回的那一段没变」。
- 同一键累计读取达到 N=3 次后，第 4 次起软拦截，返回「内容未变」占位而非重复全文。
- 段级哈希天然区分「读不同段」（hash 不同，放行）与「重复读同一段」（hash 相同，拦截），**不误伤大文件分段读**。
- 文件被写入导致该段内容变化时，段哈希随之变化，计数自动清零——「写后校验」的合法重读自然放行，无需额外判断「中间是否发生过写操作」。
- N=3 的实测依据：本 run 中 MainActivity.kt 44 读/1 hash，N=3 即可挡掉约 93% 的重复读，收益已近饱和；再收紧（N=2）只多约 2% 收益却翻倍误伤风险。

### 决策二：窗口空转熔断，总轮数上限维持不变

- 新增「空转熔断」，与「总轮数上限」正交：总轮数上限（`max_tool_rounds` / `model_turn_limit`，默认 10000）维持不变，继续服务于长任务的物理兜底。
- 空转信号：滑动窗口 W 步（默认 20）内，`read_file 且 (path, content_hash) 已见`（=重复读）占比 ≥ 阈值（默认 70%）→ 判定空转。
- 该信号天然区分「长任务在推进」（读新段/新文件，hash 首次出现，不触发）与「原地空转」（反复读同一批未变段，触发）；不会像「连续无写」那样在理解阶段误报。
- 收敛动作分级：注入提醒（「已连续 N 步重复读取未变化的文件，是否偏离目标」）→ 仍不收敛则强制压缩 → 再不行才终止。
- 阈值依据：本 run 约 55% 的调用是「重复读内容未变」，窗口占比阈值 70% 可稳抓且不误伤正常读取。

### 决策三：读/写生命周期清单（一步根治，无短期过渡）

- **根治而非止血**：根因是文件读取结果进了易失的消息历史、摘要式压缩丢正文、模型失忆只能重读。根治 = 把「读/写过的文件状态」从易失消息里拿出来，放进一份**跨压缩周期持久**的结构化清单，压缩时结构化注入——模型压缩后看到即知「读过、没变」，从结构上没有理由再重读。
- **清单内容**：path + 段级 hash + 整文件 hash + 操作类型 + model_step；覆盖 .kt/.xml（**不硬编码扩展名白名单**，吸取 claw-code `collect_key_files` 硬编码漏 .kt/.xml 的教训）。
- **持久化方式**：文件状态放进 LangGraph state 的 `files` channel、随 checkpoint 持久（deepagents `StateBackend` 范式：thread 内持久 + 每步自动 checkpoint），而非放进易失消息历史；Clawith 同栈 LangGraph 应复用 checkpoint，**不新建独立 DB/Redis 存储**（headroom `CcrStore` 的独立 Sqlite 后端是另一范式，仅作对照）。
- **压缩注入范式**：摘要 + 原文 offload 落盘 + 可检索引用（deepagents `SummarizationMiddleware`：老消息 LLM 摘要、完整历史 offload 到 backend 供后续检索），而非纯摘要式丢正文。
- **引用用确定性 hash，不做语义搜索**（headroom 明确「no BM25 search」），避免过度设计。
- **降级语义**：清单缺失/失效时，从「无损+引用」降级为「有损（退回重读）」，不 crash、不产生死引用（headroom「silently converts lossless-with-retrieval into lossy」印证）。
- 软去重（决策一）与空转熔断（决策二）共用这份清单：清单是唯一数据源，二者是消费点。

### 决策四：两种哈希语义严格分离

- **段哈希**（`result_metadata.content_hash` = 对返回 summary 的 sha256）：用于**去重**——回答「这一次读返回的那一段有没有变」。
- **整文件哈希**（storage 层对文件字节的 sha256）：用于**读/写生命周期清单、压缩、写后校验**——回答「整个文件有没有变」。
- 两者语义不同，禁止混用；去重只用段哈希，清单/压缩只用整文件哈希。若混用会导致「改了文件别处、读的这段没变」被误判为文件已变而放行重复读。

### 决策五：事实基线（实现时不可偏离）

- `read_file` 是**分段读**：`offset`（默认 0）+ `limit`（默认 2000 行），超 2000 行需多次 offset 递进；返回头标注 `lines X-Y of N`。
- 返回结果另有字节级兜底截断：超过 `AGENT_RUNTIME_TOOL_RESULT_INLINE_MAX_BYTES`（默认 8192）时截断并标记 `summary_truncated`，全文归档到 `result_ref`。
- 去重键基于「返回给模型的内容」，而非原始文件字节。

### 决策六：唯一新建 seam——纯函数判定层

- 全部判定逻辑（去重决策、空转判定、清单摘要生成）收敛进一个纯函数模块（暂名 `ReadDedupDecider`），零副作用。
- 输入：工具执行记录流（path、offset、limit、content_hash、tool_name、model_step）+ 配置（N、W、占比阈值）。
- 输出：决策（放行全文 / 去重占位 / 空转收敛信号 / 清单摘要）。
- 复用两个现有接线点（不算新 seam）：工具结果归一化处（去重决策在此决定返回完整 summary 还是占位）、压缩落地处（压缩时注入清单摘要）。

## Testing Decisions

- **原则**：只测外部行为，不测实现细节——重复读同段 → 返回占位；读不同段 → 返回全文；文件被写后重读 → 放行；窗口内空转 → 产生收敛信号；压缩落地 → 注入清单摘要。
- **被测模块**：`ReadDedupDecider` 纯函数（核心）；两个接线点（集成）。
- **Prior art**：复用现有测试的组织方式——工具结果归一化（`tool_execution` 相关测试）与摘要式压缩（`run_compactor` 相关测试）均已有一批行为级测试，新测试沿用相同风格与 fixtures。
- **关键回归用例**（对应该「不误伤」的承诺）：
  - 大文件 offset 递进分段读（0-2000 / 2001-4000）不被去重；
  - 同一段 `limit` 不同或文件总行数变化导致的段头变化，方向为「漏识别」（宁漏勿错），不得误伤；
  - 文件被写入后对**改动段**的重读放行、对**未改动段**的重复读仍被去重。

## Out of Scope

- **前缀缓存对齐**（DeepSeek 前缀缓存命中率优化）：独立性能票，另立，不在本 spec。
- 调整 `max_tool_rounds` / `model_turn_limit` 的默认值或 clamp 上限：维持现状，不做。
- 引入外部 issue tracker 或新的工作流工具链：本项目以本地文件式 spec/ticket 推进。
- 模型侧改进（换模型、加 context_window_tokens 配置等）：不在本护栏范围。

## Further Notes

- 依赖关系：读/写生命周期清单（决策三）是地基；软去重（决策一）与空转熔断（决策二）都建立其上（均阻塞清单）；前缀缓存对齐（Out of Scope）独立。
- **参考项目对比（源码级，按优先级分层）**：
  - **deepagents（T0 技术栈本体，最优先级）**：`StateBackend` 文件状态入 graph state `files` channel 随 checkpoint 持久（thread 内持久 + 自动 checkpoint）；`SummarizationMiddleware` 老消息 LLM 摘要 + 完整历史 offload 到 backend 可检索；`_message_eviction` 超大结果落盘 + head/tail preview + 引导 read_file offset/limit 分段读；`_prompt_caching` 前缀缓存中间件（印证 Out of Scope 项是已知独立工程项）。
  - **headroom（T0 在用）**：`CcrStore` 原文落盘（Sqlite 生产默认持久）+ prompt 留 hash marker + 类型感知 offload + `estimate_bloat` 门控；明确「no BM25」、缺 key 降级为 lossy。
  - **software-agent-sdk / OpenHands V1（T1 最高）**：`CondenserBase` 可插拔压缩器（`LLMSummarizingCondenser`）——压缩器可插拔，Clawith 在「压缩接缝」上把摘要式压缩演进为清单注入。
  - **codex（T1）**：remote compaction——压缩是 model-facing 上游请求，带 payload/checkpoint 证据——注入的清单要被模型真正消费。
  - **letta-code（T1）**：MemFS「filesystem-backed memory」文件即记忆、git 化 + deferred 索引——「文件状态即持久记忆」范式同源。
  - **gemini-cli / claw-code（T1/T2）**：`ToolOutputDistillationService` 豁免 read_file 蒸馏、`deduplicatePathsByFileIdentity` 用 dev+inode 做文件身份、`collect_key_files` 硬编码扩展名漏 .kt/.xml——分别印证「读状态保护」「文件身份」「不硬编码白名单」。
- 三决策点与「两个哈希语义分离」是此前复盘对话中与用户逐项拍板的结论，此处固化。
- 本 spec 尊重 `.specify/memory/constitution.md` 的「Evidence Before Claims / Minimal Scoped Changes / Contract and State Ownership / Tests Prove Behavior / Preserve Existing Work」五原则；Problem Statement 中的根因数据均来自 `agent_tool_executions` 账本的聚合查询与源码级核实。
