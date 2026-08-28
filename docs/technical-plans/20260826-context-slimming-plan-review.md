# 上下文瘦身方案审查（记忆参考项目 + 全网一手最佳实践）

> 日期：2026-08-26 ｜ 审查对象：`20260826-context-token-profile.md` 中的 4 项瘦身方案
> 证据三源：① 本地参考仓库源码（deepagents/LangChain/gemini-cli/codex/SWE-agent）② 仓库既有设计文档（20260818 主计划 / 20260819 多 run 污染 / 20260820 backlog）③ 全网一手调研 `20260826-context-slimming-best-practices.md`（44 处引用）
> 配套修正：`20260826-context-token-profile.md` 两处归因错误已在本文件 §0 修正。

## §0 画像修正（新证据推翻的旧结论）

| 旧结论 | 新证据 | 修正 |
|---|---|---|
| 基础前缀 26.6K 中「跨 run 历史 ~24.5K」 | 工具 schema 实测 34 工具 = 5,961 token；系统提示 1,974；run 起始链仅 4 条消息（线程历史 ~4-5K） | 基础前缀 ≈ 工具 schema ~6-8K（运行时含内置文件工具）+ 系统 2K + 线程历史 ~4-5K + 其余 ~10-14K（疑为 DeepSeek 公共前缀 unit 与更早请求匹配，需 provider 侧日志确证）。**「跨 run 历史 24.5K」不成立，方案①的量级从「最大杠杆」降为中杠杆。** |
| 验证门每步 8.7K 全价重算 | 整个 run（36 步）**node:verify 只出现 1 次**（14:08:53 终止步）；26 次 node:model、2 次 compact 摘要 | 验证门已是「关键节点校验」（业界标准形态），每次 run 一次性 ~8.7K+4s。**方案④从「高频大头」降为「低频微调」。** |
| 缓存命中率 ~6% | 步间 cache_read 26.6K→57.1K 稳定增长 | 命中率 90%+，缓存机制健康。 |

**修正后的真实成本结构（36 步任务实测）**：
- 每步：未命中 0.4-5.3K token（动态块+终控+新工具结果），模型步 2-16s（随上下文增长）；
- 压缩后重建步：30-67s（每次压缩后 2-3 步）；
- Thread Compact ×2：摘要调用 18.3s/30.4s；
- 验证门 ×1：~4s。

## §1 方案① 跨 run 历史治理 —— 支持，但降级并采用仓库既有设计

**仓库既有先例**：`20260819-multi-run-context-pollution-fix.md` 已定稿 L1 方案（run 边界硬左边界 + 上一 run 模板化单句摘要，对齐 LangChain `trim_messages(start_on=...)` 语义），尚未部署。

**外部证据**：
- deepagents `compact_conversation` 工具官方措辞 "Use it when moving on to a completely new, unrelated task"——跨 run 压缩有官方背书；
- Anthropic《Effective context engineering》："最轻量压缩=清工具结果……one of the safest lightest touch forms of compaction is tool result clearing"——**先清旧工具结果/截断旧参数，不足再摘要**；
- Claude Code："A clean session with a better prompt almost always outperforms a long session with accumulated corrections" + 建议任务间 `/clear`；
- Letta：历史/状态外置为文件，按需读取（不进上下文）。
- MemGPT：70% 警告 / 100% flush、逐出 50%、递归摘要。

**裁决**：**支持（量级下调 + 顺序修正）**。执行 = 落地仓库自己的 L1；动作顺序按「最轻量优先」：清上一 run 工具结果 → 模板化单句摘要（零模型成本）→ 必要时 LLM 摘要。摘要提示词必须显式保留任务状态/文件状态/失败用例（OpenHands j2 模板先例）。

## §2 方案② 工具结果截断 —— 支持（本计划实测收益最大项），先 A/B

**外部证据**（官方+参考实现齐备）：
- Anthropic 服务端 `clear_tool_uses`：旧工具结果替换为占位符，"no longer needed once Claude has processed them"；
- deepagents `_overflow_clip`：read_file 结果**头部 4K 字符 + 恢复指引**（"The full content is at {path}. Use read_file with offset and limit..."），其余工具整体外置 `/large_tool_results/{id}` + head/tail 预览桩；只对「超出 keep 预算的尾部批量」动手，小结果不截；
- SWE-agent `LastNObservations(n=5)`：旧观测替换为 "Old environment output: (n lines omitted)"，`always_keep_output_for_tags` 保错误输出，`polling` 批量删除摊薄缓存重建；
- gemini-cli `summarizeToolOutput`：shell 输出 LLM 摘要 ≤2000 token，**错误栈/告警完整保留**（`<error>/<warning>` 标签）。
- 成功率影响：**无一手对照实验**（调研明确标注）；最接近实证=OpenHands 冷凝在 SWE-bench「更少钱/token/时间解出更多题」。

**裁决**：**支持，三要件**：①恢复路径（path+offset/limit 指引，deepagents 文案可直抄）；②错误信息优先保留；③只截旧结果（keep 窗口保护近期）。阈值 2K 与 deepagents 4K 同量级合理；**先在自己的评测集做截断开关 A/B 再定阈值**（本仓库有 agent-evaluation/benchmark 基建可用）。

## §3 方案③ 动态块拆分 —— 支持（三厂官方一致），DeepSeek 落地要点不同

**外部证据**：
- Anthropic："Place the breakpoint on the last block that stays identical across requests"，并单列反例警示 "Common mistake: Breakpoint on content that changes every request"；
- OpenAI："Keep changing content after the breakpoint" + explicit-only 模式对断点后内容**零写费**；
- DeepSeek：**无断点机制**，命中=与 cache prefix unit 逐字节全匹配，公共前缀需 2+ 请求才持久化、best-effort——"拆分"的实质只能是**稳定块绝对字节一致 + 变化块排尾**，必须用 `prompt_cache_hit_tokens` 实测回归；
- 更优模式（官方有据）：动态内容尽量不进消息流——Letta `system/` 外置按需读取、OpenAI `defer_loading`、Anthropic memory tool。

**裁决**：**支持**。实现：把动态块内**每步稳定**的部分（thread_running_summary、session_context_snapshot、company/relationships/memory、当前用户）移入 `prefix_cache_break` 之前的稳定区；仅真正每步变化的部分（当前时间、current_run 状态、pending 消息）留在尾部动态块。预期：每步未命中 2-5.3K → ~1K 量级。零语义损失，优先实施。

## §4 方案④ 验证门 —— 修正撤销：已是业界标准形态，仅剩微调

**证据推翻原方案前提**：验证门仅在终止步执行一次（trace 实锤），「每步 8.7K 全价重算」不成立。

**外部证据**（作为「降本手段」参考）：
- Anthropic BEA：每步 ground truth = 环境反馈（工具结果/代码执行），无需额外 LLM；
- SWE-agent/OpenHands/codex 主循环均无 LLM 验证器（源码核实）；Claude Code 每轮 LLM 评价器是可选 `/goal` 且带 8 次阻塞强制放行；
- OpenAI judge 官方示例：**固定 rubric 作缓存前缀（≥最小可缓存长度）+ 评估对象放断点后不写缓存**，示意命中率 70%。

**裁决**：原「每步校验」问题不存在。剩余微调（低优先）：①轨迹 JSON 内工具结果复用方案②的截断/摘要（当前 8.7K 里大半是重复的旧工具结果）；②rubric 前缀缓存（OpenAI judge 模式）。

## §5 审查新增项（仓库 backlog + 调研合并）

| 新增 | 来源 | 说明 |
|---|---|---|
| ⑤ 工具描述精简 | backlog T1#1（本计划漏了） | schema 5,961 token 中 description+参数描述占 44-51%；精简可省 ~1.5-3K 窗口预算与注意力 |
| ⑥ 压缩模型分级 | backlog T2#4 | 当前 compact 摘要用 agent 主模型（实测 18.3s/30.4s 两次）；改便宜/快模型（group 模式已有 `resolve_multi_agent_compact_model` 先例），须 benchmark 守摘要质量 |
| ⑦ 压缩阈值 A/B | backlog T2#5 | deepagents 0.85/0.10、OpenHands 触发后压到一半、MemGPT 70%/100%；当前 0.8 与 deepagents 同量级，问题不在阈值而在历史增速——②③ 落地后重测 |
| ⑧ 工具输出 LLM 摘要 | gemini-cli 模式 | build 日志类大输出用便宜模型摘要 ≤2000 token、错误栈完整保留——是②的增强形态，可作 A/B 变量之一 |

## §6 最终裁决与执行顺序

| # | 方案 | 裁决 | 优先级 |
|---|---|---|---|
| ② | 工具结果截断（+恢复路径/错误优先/只截旧） | 支持，先 A/B | **P0**（历史增速 +3.3K→~1.7K/步，压缩周期翻倍；压缩重建 30-67s 步随之消失） |
| ③ | 动态块拆分（稳定段入前缀） | 支持，hit_tokens 实测 | **P0**（每步未命中 2-5K→~1K，零语义损失，改动面小） |
| ① | 跨 run 历史治理（落地仓库 L1，轻量优先） | 支持（降级为中杠杆） | **P1**（基础前缀 -4~5K） |
| ⑤⑥ | 工具描述精简 + 压缩模型分级 | 采纳 backlog 项 | P1（⑥直接砍两次 18-30s 摘要调用） |
| ④ | 验证门输入瘦身（轨迹复用②机制 + rubric 缓存前缀） | 修正为微调 | P2 |
| ⑦ | 压缩阈值 A/B | 待②③后重测 | P2 |

**执行约束**（调研 + 仓库红线）：
1. 所有缓存相关改动上线后用 `prompt_cache_hit/miss_tokens` 做真实回归（DeepSeek 公共前缀 best-effort，不能假设命中）；
2. 截断类改动先 A/B 守任务成功率（无一手对照实验证据）；
3. 摘要类提示词按「先 recall 后 precision」调参，显式保留任务/文件/失败状态；
4. 压缩必然重置前缀缓存（两厂官方确认）——系统提示已字节稳定，重建范围只限对话部分，现状数据已印证（cr 地板在压缩后仍保留）。
