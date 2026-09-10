# 全网自进化 Agent 横向实证：它们是怎么做的（2026-09-07）

> 定位：与仓库已有的 `20260828-self-evolution-capability-research.md`（Clawith 自身机制清单）和 `20260828-self-evolution-best-practices.md`（SI-Agents 综述 + LangMem/Mem0/Anthropic 方法论）**互补**。本文补的是：**具体开源项目怎么实现「自进化」**——逐个读源码/README 核对其触发信号、沉淀方式、召回、安全门，而非方法论转述。
>
> 证据层级标注：`[源码]`=本地/远程读实现代码；`[README]`=读官方 README 一手机制描述；`[摘要]`=仅 arXiv/GitHub description，未深读实现。

## 1. 核心结论（TL;DR）

1. **「自进化」行业主流 = 无权重更新的「上下文/记忆/技能/工具/工作流」运行时演化**，不是改模型权重。arXiv 综述原文 `[摘要]`：self-evolving agents "treating adaptation as a first-class capability, allowing not only parameter updates but also changes to runtime context, memory, tools, and workflow structures, driven by the agent's own trajectories and feedback signals"（arXiv 2507.21046）。权重更新（self-edits → SFT）是少数路线（见 §5），绝大多数项目进化的是**上下文层**。
2. **「技能自动沉淀」是 2025–2026 爆发的独立赛道**，且已经「产品化」——不再是 Voyager 那种学术 Demo。代表：GenericAgent（14k★）、Acontext（3.7k★）、SkillClaw（2.6k★）、hermes-agent（闭学习环）、OpenViking（36k★，统一 memory/skill）。
3. **触发信号是各项目真正的分水岭**（Voyager 靠环境验证、Acontext/SkillClaw 靠「任务完成/失败」+ LLM 蒸馏、hermes 靠「复杂任务后 + nudge」）。**没有**任何项目靠「agent 在工作流执行中顺手写 skill」——见 §6。
4. **安全门是标配**：hermes-agent 对 agent 自主创建的 skill 有 `dangerous findings` 拦截 + 全量审计账本 + fail-closed 回滚。这是 Clawith 当前最缺的一环（见 §7）。

## 2. 分类框架（arXiv 2507.21046 综述）

`[摘要]` 综述给出四维分类，本文按此组织：

- **What to Evolve**（进化对象）：① Models（Policy/Experience，权重/策略）② Context（Memory Evolution / Prompt Optimization）③ **Tools（Autonomous Discovery and Creation，即技能自动沉淀）** ④ Architecture（单/多 Agent 系统优化）
- **When**：Intra-Test-Time（运行时在线演化）vs Inter-Test-Time（离线/跨会话演化）
- **How**：Reward-based / Imitation & Demonstration / Population-based evolutionary
- **Where**：General domain / Specialized domain（coding、GUI、金融、医疗…）

Clawith 语境关心的「create new skills」落在 **What=Tools、How=Imitation（自轨迹蒸馏）**，是四类里最「工程可实现」的一类（无需 RL、无需权重训练）。

## 3. 路线 A：技能自动沉淀（Tools: Autonomous Discovery & Creation）

与 Clawith README「create new skills」承诺同赛道。逐个核对机制：

### 3.1 GenericAgent（lsdefine，14k★）`[README]`

- 哲学：**"don't preload skills, evolve them"**。核心 3K 行 seed 代码 + 9 原子工具 + 100 行 Agent Loop。
- 机制（README 原文流程图）：
  ```
  [New Task] → [Autonomous Exploration: install deps·write scripts·debug·verify]
            → [Crystallize into Skill: write to memory layer]
            → [Direct Recall on Next Similar Task]
  ```
- 关键：**「每次解决新任务 → 自动把执行路径结晶为 Skill 写回记忆层」**，下次一句话调用。技能树完全从 seed 自生长。
- 触发 = **任务成功执行完毕**（无「重复 N 次」门槛）；无显式安全门描述（README 未提）。论文 arXiv 2604.17091，技术报告仓库 JinyiHan99/GA-Technical-Report。

### 3.2 Acontext（memodb-io，3.7k★）`[README]`

- 哲学：**"Skill is Memory, Memory is Skill"**——把记忆建成普通 Markdown skill 文件，可读可编辑可跨 agent 分享，无 embedding、无 API 锁。
- 机制（Store 管道）：
  ```
  Session messages → [Task complete/failed 触发] → [Distillation: LLM 提炼「什么成功/失败/用户偏好」]
                  → [Skill Agent: 决定存到现有 skill 还是新建 + 按你的 SKILL.md schema 写] → Update Skills
  ```
- 召回：`get_skill` / `get_skill_file` 工具，**渐进披露、agent 按需取**，非语义 top-k 检索。
- 触发 = **任务完成或失败**（agent 报告或自动检测）→ 后台蒸馏，非执行中直写。

### 3.3 SkillClaw（AMAP-ML，2.6k★）`[README]`

- 定位：**collective skill evolution**——skill 从每次真实交互演化，跨 session/agent/device/user 共享。
- 架构两件套（与 Hermes 的 task loop **分离**为 post-task evolution loop）：
  1. **Client Proxy**：本地 API proxy 拦截 agent 请求、记录 session artifacts、管本地 skill library；
  2. **Evolve Server**：可选后台服务，从共享存储读 session 数据，演化/创建 skill 写回。两引擎：
     - `workflow`：固定 3 阶段 LLM 管道（Summarize → Aggregate → Execute）
     - `agent`：OpenClaw-driven agent workspace 直接编辑 skill
- 存储：阿里 OSS / S3 / 本地 FS，格式 SKILL.md。论文 arXiv 2604.08377。
- 核心隐喻：Hermes 会「学」但没人帮它「digest」——SkillClaw 负责 auto-evolve / auto-deduplicate / auto-improve 质量的「消化」环节。

### 3.4 hermes-agent（NousResearch）`[源码]`

- 定位：自称 "the only agent with a built-in learning loop"——**闭学习环**：从经验创建 skill、使用中自改进、周期性 nudge 自己持久化知识、搜索自己的历史对话。
- 本地源码核实到的**完整 skill 生命周期工具链**（`tools/` 下）：
  - `skill_manager_tool.py`：**agent-managed skill creation & editing**——有 `create`/`update`/`delete` 操作（"Create, update, or delete skills — your procedural memory"）；
  - **安全门**：`"Agent-created skill blocked (dangerous findings): …"`（`skill_manager_tool.py:58`）——agent 自主创建有危险内容拦截；
  - `skill_ledger.py`：per-mutation 审计账本（JSONL before/after manifest + sha256 去重 blob）+ 单编辑回滚，注释明言 **"TELEMETRY, NOT A GATE"**，但 `rollback_entry` **FAILS CLOSED**；
  - 配套：`skill_provenance.py`（溯源）、`skill_usage.py`（使用统计）、`skills_tool_dedup.py`（去重）、`skillevaluator_scan.py`（评估）、`skill_linter.py`（lint）。
- 这是本文发现中**最完整的「agent 自主 skill 生命周期 + 安全边界」实现**，且已兼容 agentskills.io 开放标准。

### 3.5 OpenViking（volcengine，36k★）`[README]`

- 定位：self-evolving **context database**——memory / resources / skills 统一成 `viking://` 虚拟文件系统，agent 用 `ls`/`tree`/`find` 浏览自身上下文而非查黑盒向量库。
- 机制：三层 L0 abstract / L1 overview / L2 details，写时即分层、按需加载；**"Sessions become memory"**——session commit 后异步提取用户偏好 + agent 经验入长期记忆；目录递归检索保留浏览轨迹可调试。
- 量化：LoCoMo 用户记忆准确率从原生 24–57% 拉到 80–83%，token 省 34–91%。集成 Claude Code/Codex/OpenClaw/Hermes/Cursor。

### 3.6 Voyager（NVIDIA，arXiv 2305.16291）`[G3 已核对]`

- 经典源头：技能库「code-as-policy」，**触发=单次任务成功（Minecraft 环境验证）→ 程序入库**，无重复门槛。详见 `20260828-self-evolution-best-practices.md` §2.4 与 `20260907-skill-sedimentation-lifecycle-production-plan.md` §2。

## 4. 路线 B：记忆演化（Context: Memory Evolution）

`[源码/README]` 已在本工作区核对过，此处归纳机制差异：

| 项目 | 进化对象 | 机制 | 来源 |
|---|---|---|---|
| LangMem | semantic/procedural/episodic 三分类记忆 | `MemoryManager`（LLM 提取器）管 insert/update/delete，agent 不直写文件 | 本地源码 `extraction.py:185/217` |
| Mem0 | 跨会话语义记忆 | 供应商发布的使用手册 skills + marketplace 安装，无 agent 自主沉淀 | 本地 `skills/README.md` |
| letta-code | 记忆块 + 版本化记忆子代理 | memory-v2/reflection/recall 子代理，frontmatter 即配置，fail-closed 沙箱 | 本地源码 + `20260903-letta-code-study.md` |
| EverOS/MemOS | 持久记忆层 | 本地优先 Markdown 记忆 / 混合检索 | `[摘要]` 未深读 |

## 5. 路线 C/D：提示词/策略优化 + 权重/架构（简述）

- **提示词/策略优化**（What=Context·Prompt Optimization / Models·Policy）`[摘要]`：AgentEvolver（modelscope，1.6k★）、EvoAgentX（ANative-Lab，3.3k★）——LLM-as-optimizer 遗传式迭代优化策略/提示词；Reflexion/Self-Refine——口头强化（verbal RL）。均未深读实现。
- **权重/架构演化**（What=Models/Architecture）`[摘要]`：综述提到 self-edits→SFT 路线（模型自我编辑触发微调，权重持久更新）；EvoMap/evolver（9.1k★）——GEP 基因编程驱动的「可审计演化引擎」；Agent0（aiming-lab）——zero-data 自进化。与 Clawith 语境（不改权重）无关，仅列完整性。

## 6. 关键横向对比（怎么做 = 触发 × 沉淀 × 召回 × 安全）

| 项目 | 触发信号 | 沉淀方式 | 召回 | 安全门 | 任务/演化 loop 分离 |
|---|---|---|---|---|---|
| Voyager | 单次成功（环境验证） | code-as-policy 入库 | 按需 | 环境反馈兜底 | 否（执行内） |
| GenericAgent | 新任务执行完毕 | 结晶执行路径写记忆层 | 一句话调用 | README 未提 | 否（执行后即时） |
| Acontext | 任务完成/失败 | LLM 蒸馏→Skill Agent 写 SKILL.md | get_skill 渐进披露 | 未提 | **是**（post-task 蒸馏） |
| SkillClaw | post-task（任意交互） | 3 阶段 Summarize→Aggregate→Execute | 共享库 | 未提 | **是**（独立 evolve server） |
| hermes-agent | 复杂任务后 + nudge | agent 自主 create/update/delete | 跨会话召回 | **dangerous findings + ledger + fail-closed 回滚** | **是**（post-task nudge） |
| OpenViking | session commit | 异步提取偏好/经验 | 目录递归检索 | 未提 | **是**（异步） |

**两条跨项目规律**：

1. **触发信号分水岭**：可靠触发要么靠「环境验证」（Voyager，最硬但依赖可验证环境），要么靠「任务完成/失败 + LLM 蒸馏」（Acontext/SkillClaw/hermes，通用）。**没有任何项目让 agent 在任务执行中途直写 skill 目录**——这正是 Clawith 现状 `write_file` 授权做的事。
2. **任务 loop 与演化 loop 分离**是成熟项目的共同模式（Acontext 的 post-task 蒸馏、SkillClaw 的独立 evolve server、hermes 的 post-task nudge、OpenViking 的异步 commit）。演化放在任务执行**之外/之后**，既避免污染执行上下文，又能集中做去重/评估/质量把关。

## 7. 对 Clawith 的启示（诚实负结论）

1. **Clawith 的 README 承诺不是空头支票，是行业已有成熟实现的赛道**。hermes-agent 是真在做的「agent 自主 create/update/delete skill + 闭学习环」，且已兼容 agentskills.io 标准——Clawith 的「create new skills」有直接对标物。
2. **hermes-agent 的「安全门 + 审计账本 + fail-closed 回滚」直接补上了 Clawith 缺的安全边界**。对比：Clawith 现状是 `write_file` 授权直写 `skills/` + 无账本 + 门控只拦非维护用户（`maintainer_service.py` 对 agent 自主写恒放行）——而 hermes 对 agent 自主创建同样有 `dangerous findings` 拦截 + 每 mutation 记 JSONL 账本 + 回滚 fail-closed。这是 `20260907-skill-sedimentation-lifecycle-production-plan.md` 之外**新增的一条可迁移资产**（当时只对齐了「生命周期状态机」维度，未对齐「agent 自主写时的安全门 + 账本」维度）。
3. **「任务/演化 loop 分离」印证了既有修复方案的方向**：方案把沉淀导向 `workspace/skill-drafts/` + 人工移动（而非让 agent 直写 `skills/`），本质就是「演化 loop 移出任务执行 loop」——与 Acontext/SkillClaw/hermes 的架构选择一致。
4. **触发信号可选**：Clawith 无可靠环境验证（Voyager 路线走不通，G3 已论证），但「任务完成/失败 + LLM 蒸馏」路线（Acontext/SkillClaw/hermes）是通用的、不依赖沙箱的，可作 HEARTBEAT 条件义务触发（「≥3 次重复 → 写草稿」）之外的**备选触发源**（P2 观察项）。
5. **诚实负结论**：本文对 EverOS/MemOS/AgentEvolver/EvoAgentX/evolver/Agent0 等仅凭 GitHub description `[摘要]`，**未深读实现**；对 GenericAgent/Acontext/SkillClaw/OpenViking 是 `[README]` 层（一手机制描述，未逐行验源码）；仅 hermes-agent 走了 `[源码]` 层。如需把某项目纳入 Clawith 迁移清单，应先 clone 本地做源码级核对（记忆 `reference-projects` 纪律）。

## 8. 引用

- 综述：A Survey of Self-Evolving Agents（arXiv 2507.21046）
- GenericAgent：github.com/lsdefine/GenericAgent（README，arXiv 2604.17091）
- Acontext：github.com/memodb-io/Acontext（README）
- SkillClaw：github.com/AMAP-ML/SkillClaw（README，arXiv 2604.08377）
- hermes-agent：github.com/NousResearch/hermes-agent（本地源码 `tools/skill_manager_tool.py`、`skill_ledger.py` 等）
- OpenViking：github.com/volcengine/OpenViking（README）
- Voyager：arXiv 2305.16291（G3 已核对）
- 既有：`20260828-self-evolution-best-practices.md`、`20260828-self-evolution-capability-research.md`、`20260907-skill-sedimentation-lifecycle-production-plan.md`
