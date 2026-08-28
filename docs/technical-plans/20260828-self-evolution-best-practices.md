# 自进化 Agent 全网最佳实践调研：记忆固化与自我改进（2026-08-28）

> 问题：Clawith 这类「运行时记忆固化」自进化方式，全网做好的方式是什么？我们差在哪？
> 方法：SI-Agents 综述（312 篇）定坐标系 → LangMem/Mem0/Anthropic 工程实践定标准 → 对照 Clawith 现状 → 差距清单。
> 结论先行：**方向正确（Scaffolding/Memory 分支 + 运行时钩子哲学），但缺「记忆整合」「程序记忆」「评估闭环」三块；热路径强制轮是有依据的取舍，不必改向后台。**

## 1. 方法论坐标系：SI-Agents 综述

权威综述 [Self-Improvements in Modern Agentic Systems](https://selfimproving-agent.github.io/)（312 篇，2026，KAUST+Schmidhuber，arXiv:2607.13104）把自进化分为三大支：

- **1. Foundation Model Improvement（77 篇）**：改模型本身（自合成数据微调/自奖励 RL）。慢、贵、训练向。
- **2. Scaffolding Improvement（176 篇）**：改模型外层的 prompt（39）/ **memory（65）** / tool（51）/ full scaffolding（21）。快、便宜、可逆。
- **3. Evaluation & Benchmarking（59 篇）**：怎么测量改进（metric/judge）+ 怎么基准化——**独立成支，说明「可测量」是自进化系统的必备部件而非可选**。

Memory 支下分三子层：**memory object**（存什么：事实/经验/规则）、**memory structure**（怎么组织：层次/图谱/Zettelkasten）、**memory processing**（怎么处理：写入/整合/遗忘/检索）。

[Yohei Nakajima《Better Ways to Build Self-Improving AI Agents》](https://yoheinakajima.com/better-ways-to-build-self-improving-ai-agents/) 给出自进化的严格定义：①行为随时间改变（非多次采样）②改变由 agent 自身经验/反馈驱动（非人工标注）③机制集成在 agent 循环内。六大机制：反思回路（Reflexion/Self-Refine）、自生成数据课程、自适配模型、自改代码 agent（Gödel Agent/MOSS）、具身、**验证-安全-控制**（防自进化跑偏）。

**定位：Clawith 票 07 门禁 = 2.2 Memory 支 processing 层的「运行时保证的写入控制」，且借了 2.4 Full Scaffolding 的运行时钩子哲学。方向在全网坐标系的正中央。**

## 2. 工程标准：LangMem / Mem0 / Anthropic

### 2.1 记忆类型分层（LangMem 官方概念指南）

[LangMem conceptual guide](https://langchain-ai.github.io/langmem/concepts/conceptual_guide/)（LangChain 长期记忆 SDK）沿用认知科学三分：

| 类型 | 内容 | 存储 |
|---|---|---|
| Semantic | 事实/知识（用户偏好、领域知识） | profile 或 collection |
| Episodic | 过往成功/失败经验（含情境与思维过程） | collection |
| Procedural | 行为规则/技能（系统行为、core personality） | prompt 规则或 collection |

要点：procedural memory 通过 [prompt optimizers](https://www.langchain.com/blog/langmem-sdk-launch) 从交互反馈更新系统提示；episodic 保存「为什么这样有效」的完整轨迹做 few-shot。**我们只有 declarative semantic（memory.md）——这是最大类型缺口。**

### 2.2 形成时机：热路径 vs 后台（LangMem）

LangMem 明确定义两种形成方式：

- **Active（conscious，热路径）**：对话中即时写入。优点即时；LangMem 原文警告「adds perceptible latency」且「adds one more obstacle to the agent's ability to satisfy the user's needs」。
- **Background（subconscious，后台）**：交互后/空闲时反思提取，「without slowing down the immediate interaction」。

**Clawith 的强制轮是热路径方案，但场景不同**：LangMem 针对聊天助手（主任务就是对话，插入即干扰）；Clawith 是任务型 run，门禁在 finish 收尾处、至多一轮、实测代价 ~30–40s 且换回 1/3 的固化率（e0fb663d 真实写入，4af61b58/12911994 留痕）。**热路径在此场景是站得住的取舍**，但可作为 A/B 项（见 G2）。

### 2.3 记忆生命周期：整合与召回（LangMem + Mem0 + Anthropic）

- LangMem 的 **enrichment/consolidation**：新信息必须与旧记忆 reconcile——更新/删除/泛化合并；明确警告 over-extraction 降检索精度、under-extraction 降召回。召回 = 语义相似度 × **重要性 × 强度（recency/frequency）**。
- Mem0（本地仓库 README 核实，论文 arXiv:2504.19413）：single-pass LLM 提取 + 实体链接 + 多信号检索融合（语义/BM25/实体）。生产系统的做法是**写入轻、检索重**。
- Anthropic [Effective context engineering](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents)：**context rot**——上下文越长，注意力预算摊薄，检索精度下降；note-taking（agent 主动记笔记）「excels for iterative development with clear milestones」，compaction 适合长程对话。**记忆文件本身进入上下文也要成本，无限膨胀 = 慢性 context rot。**

**我们缺整合层**：memory.md 只增不减不合并，MEMORY_INDEX 是索引不是整合；无重要性/强度打分，模型 read 全文件（无检索）。

### 2.4 程序记忆：Voyager 式技能积累

Voyager（2023，综述 quick-start 代表作）：成功技能自动入 skill library（code-as-policy），后续任务直接路由复用。Clawith 的 `skills/` 目录机制与此同构但**只人工维护，agent 不能自动沉淀**。经验层（experience layer）是 [Yohei](https://yoheinakajima.com/better-ways-to-build-self-improving-ai-agents/) 的总结性判断：**最大收益来自把交互轨迹变成可复用结构**。

### 2.5 验证与安全控制

Yohei 第六机制 + 综述 2.4 支（MOSS/AgentDevel）共识：自进化的每一层都要有 release-engineering 式护栏（版本、回滚、验证、红队）。票 07 的「至多一轮 + 留痕事件 + 幂等键」正是此哲学的最小实现 ✓。

## 3. Clawith 现状对照

| 维度 | 全网最佳实践 | Clawith 现状 | 差距 |
|---|---|---|---|
| 记忆类型 | semantic/episodic/procedural 三分 | 只有 semantic（memory.md） | **缺 episodic、procedural** |
| 写入保证 | 热路径（即时机）或后台，二选一有依据 | 热路径强制轮（票 07）| 基本对齐；可补后台通道 |
| 写入控制 | 门禁/去重/防跑偏 | 运行时门禁 + 留痕 ✓ | 对齐 |
| 整合 | consolidate/update/delete、防 over/under-extraction | **只增不整合** | **缺整合层** |
| 召回 | 检索打分（相似度×重要性×强度） | 模型 read 全文件 | 缺打分检索 |
| 程序化 | Voyager 技能库 / prompt optimizers | skills 目录仅人工维护 | **缺自动技能沉淀** |
| 评估 | 独立分支：metric/judge + benchmark | agent-evaluation 基建已有、Langfuse 留痕 | 缺「记忆质量」评测集 |
| 安全 | release-engineering 护栏 | 幂等键/留痕/至多一轮 ✓ | 对齐（浅层） |

## 4. 差距与建议（收益 × 风险排序）

**G1 记忆整合层（收益 ★★★，风险低）——最该做**
定期（心跳触发，复用留痕事件与 heartbeat 基建）对 memory/ 做 LangMem 式 consolidation：合并重复条目、更新过时事实、删除无效、标注重要性/recency。直接收益：抑制 memory.md 膨胀导致的 context rot（这是当前最确定的慢性劣化源）。手段成熟（LangMem/Mem0 都有现成 prompt 模式），风险低。

**G2 后台固化通道（收益 ★★，风险中）——A/B 而非替代**
现状强制轮实测有效（1/3 真写 + 2/3 留痕），代价 ~30–40s 延迟。补一条后台通道：run 结束后由心跳异步反思「该 run 是否有可固化知识」，与热路径门禁 A/B 对比（指标：固化率、任务完成时长、memory 质量）。若后台方案固化率不低于热路径且延迟降为 0，再迁移。

**G3 程序记忆固化（收益 ★★★，风险中高）**
让 agent 把「重复三次以上的成功流程」沉淀为 skill 文件（Voyager 式，Clawith skills 目录天然支持）。收益最大（从「知道」到「会做」的质变），风险在质量门禁——必须经过人工审核或严格的 bench 验证才入 skills/，否则劣质技能污染后续所有 run（需要 release-engineering 护栏，见 2.5）。

**G4 记忆质量评估闭环（收益 ★★，风险低）**
用 agent-evaluation 基建建「记忆质量 benchmark」：同一任务 A/B（有 memory vs 无 memory / 整合前后）对比二次执行的表现差（SI-Agents 3.2 分支）。memory_consolidation_skipped 留痕事件同时转 Langfuse score 做趋势告警——把「留痕」升级成「反馈信号」。

**G5 召回打分检索（收益 ★，风险低）**
memory 文件超阈值后按 importance×recency 分块注入而非全量 read（Anthropic just-in-time 引用法：轻量标识 + 运行时按需加载）。排在 G1 之后顺路做。

**不建议**：FM 微调自进化（成本/风险远超收益）；full-scaffolding 自改平台代码（MOSS/AgentDevel 路线需要完整 release engineering，我们现阶段留痕护栏还不够深）。

## 5. 引用

- [Self-Improvements in Modern Agentic Systems: A Survey](https://selfimproving-agent.github.io/)（arXiv:2607.13104，312 篇 taxonomy）
- [LangMem conceptual guide](https://langchain-ai.github.io/langmem/concepts/conceptual_guide/) + [LangMem SDK blog](https://www.langchain.com/blog/langmem-sdk-launch)
- [Anthropic: Effective context engineering for AI agents](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents)（context rot / note-taking vs compaction）
- [Yohei Nakajima: Better Ways to Build Self-Improving AI Agents](https://yoheinakajima.com/better-ways-to-build-self-improving-ai-agents/)（六大机制 + experience layer）
- Mem0 本地仓库（/Users/shubinzhang/Documents/UGit/mem0）README 核实 + 论文 arXiv:2504.19413
- 经典方法：Reflexion (arXiv:2303.11366)、Self-Refine (arXiv:2303.17651)、Voyager (arXiv:2305.16291)、SAGE (arXiv:2409.00872)、Agent Workflow Memory (arXiv:2409.07429)、ReasoningBank (arXiv:2509.25140)
