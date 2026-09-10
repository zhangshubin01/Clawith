# 全网 Agent 记忆库与资料全景（2026-09-07）

> 定位：延续 `20260907-self-evolving-agents-landscape.md`（自进化 agent）与 OpenViking 研究，本轮聚焦**「记忆」这个更宽的赛道**——库/框架、权威资料、评估基准。
> 证据层级：`[源码]`=本地已 clone 可读实现；`[README]`=读官方 README 一手机制；`[摘要]`=GitHub/firecrawl 检索结果，未深读。本地 clone 目录统一在 `UGit/` 下（memory `reference-projects`）。

## 1. TL;DR

1. **记忆已是独立成熟赛道**，头部库 6.5w★（mem0）~ 3k★（letta）不等，且 2025–2026 持续爆发（HippoRAG 2、ZenBrain、HeLa-Mem、MemReader 都是 2026 年论文）。
2. **「记忆」与「自进化」正在合流**：OpenViking 的「context database + agent evolution 引擎」、memvid 的「单文件记忆层」、agentmemory 的「confidence + lifecycle」都表明——记忆不只是「存+查」，而是**有生命周期、可演化、可审计的状态系统**（呼应 arXiv 2606.06448 的「数据管理视角」）。
3. **本地已有 7 个源码级仓库**（mem0/langmem/letta-code/OpenViking/cognee/TencentDB-Agent-Memory/openhuman），**缺 8 个高价值库**（agentmemory/beads/memvid/HippoRAG/telemem/memoria/LycheeMem/automem）——如需对标应先 clone。
4. **对 Clawith 最相关的一条**：权威综述（arXiv 2606.06448）把 agent 记忆系统拆成 **四模块**（representation/storage、extraction、retrieval/routing、maintenance），评估 12 个系统后结论「**没有任何单一记忆架构在所有场景占优**」——意味着 Clawith 的记忆层不应追求「抄某一家」，而应按自身 workload 瓶颈选型。

## 2. 库/框架全景

### 2.1 本地已 clone（源码级，7 个）

| 库 | stars | 定位 | 机制（memory 记录/已读） |
|---|---|---|---|
| **mem0** | 64.8k | Memory Layer for AI Agents | 跨会话语义记忆，增删改由框架管；skills 目录是 vendor 使用手册（见前文核对）`[源码]` |
| **OpenViking** | 35.9k | Self-evolving Context Database | `viking://` 虚拟文件系统 + L0/L1/L2 三层 + **Agent Evolution 引擎**（cases→trajectories→experiences→session_skills，语义梯度 + 五态 PolicyStatus）+ **多租户** Account→User(ROOT/ADMIN/USER) 物理目录隔离（见 §5）`[源码]` |
| **openhuman** | 39.5k | local-first 个人记忆 AI | 本地优先、个人记忆 `[摘要]`（未深读实现） |
| **cognee** | 30.5k | AI memory platform | 自托管知识图谱引擎，跨会话持久记忆 `[摘要]` |
| **TencentDB-Agent-Memory** | 26.1k | 团队级记忆中枢 | MemoryCore/Knowledge/Panel/Proxy 四件套 + 四级 ACL（team/user/agent + visibility 五态）（见 §5）`[源码]` |
| **langmem** | — | LangChain 记忆 SDK | semantic/episodic/procedural 三分类，MemoryManager 管增删改 `[源码]` |
| **letta-code** | 3.2k | MemGPT 记忆块 + 记忆子代理 | memory block + reflection/recall/history-analyzer 子代理，fail-closed 沙箱 `[源码]` |

### 2.2 本地未 clone（README 层，本轮已抓）

| 库 | stars | 定位 | 关键机制（README 一手） |
|---|---|---|---|
| **agentmemory**（rohitg00） | 28.1k | Persistent memory for coding agents | "Karpathy LLM Wiki pattern + **confidence scoring + lifecycle + knowledge graphs + hybrid search**"；95.2% R@5；BM25（keyless）+ 本地 embedding + 图融合 `[README]` |
| **beads**（gastownhall） | 26.9k | 分布式图 issue tracker 记忆 | 基于 **Dolt**（版本化 SQL）；`bd remember "insight"` 替代 MEMORY.md；**语义 decay compaction** 压缩旧任务；`relates-to/duplicates/supersedes` 知识图边 `[README]` |
| **memvid** | 16.5k | 单文件记忆层 | **Smart Frames** append-only 不可变单元（时间戳/校验和/元数据），单文件=可回放的记忆时间线；+35% SOTA on LoCoMo；无数据库 `[README]` |
| **HippoRAG 2**（OSU-NLP） | 3.9k | 神经生物学启发记忆 | 海马体索引 + OpenIE；多跳联想检索 + 持续学习（NeurIPS'24 / ICML'25）；比 GraphRAG/RAPTOR/LightRAG 离线索引更省 `[README]` |
| **telemem**（TeleAI-UAGI） | 489 | Mem0 drop-in replacement | 高性能 Mem0 替代 `[摘要]` |
| **memoria**（matrixorigin） | 595 | Secure memory management | 数据完整性、安全记忆 `[摘要]` |
| **LycheeMem** | 1.1k | Lightweight LTM | 轻量长期记忆 `[摘要]` |
| **automem**（verygoodplugins） | 811 | Graph + vector 记忆 | 图 + 向量 store `[摘要]` |

## 3. 权威资料

### 3.1 Curated 清单（研究入口）

- **Awesome-AI-Memory**（IAAR-Shanghai，github.com/IAAR-Shanghai/Awesome-AI-Memory）—— 论文/系统/benchmark 精选，含四模块框架论文 `[摘要]`
- **Awesome-Agent-Memory**（TeleAI-UAGI，624★）—— "systems, benchmarks, and papers on memory for LLMs/MLLMs" `[摘要]`

### 3.2 关键论文（firecrawl 检索，`[摘要]` 层）

| 论文 | 时间 | 要点 |
|---|---|---|
| **Agent Memory: Characterization and System Implications of Stateful Long-Horizon Workloads**（arXiv 2606.06448） | 2026-06 | **数据管理视角**：把 agent 记忆拆四模块（representation/storage、extraction、retrieval/routing、maintenance），评估 12 系统 5 类 workload，结论「无单一架构全场景占优，取决于结构与瓶颈对齐」——**Clawith 记忆层选型最该读的一篇** |
| **A Survey of Self-Evolving Agents**（2507.21046） | 2025 | 自进化四维框架（见前文） |
| **Human-inspired Perspectives: A Survey on AI Long-term Memory** | 2025-01 | 记忆类型/操作（integration/updating/indexing/forgetting/retrieval/compression）系统分析 |
| **EverMemOS: Self-Organizing Memory OS**（2601.02163） | 2026-06 | 结构化长程推理的自组织记忆 OS |
| **Beyond the Context Window: Cost-Performance of Fact-Based Memory vs Long-Context LLMs**（2603.04814） | 2026-03 | fact 记忆 vs 长上下文成本收益 |
| **ZenBrain: 7-layer memory**（2604.23878） | 2026-04 | 神经启发：Hebbian + FSRS 间隔重复 + sleep 巩固 + Bayesian 置信传播；LongMemEval-500 达 oracle 91.3%（1/106 token 预算），9/9 胜 Letta/Mem0/A-Mem |
| **HeLa-Mem**（2604.16839） | 2026-04 | Hebbian 学习 + 联想记忆动态图 |
| **MemReader**（2604.07877） | 2026-04 | 被动→主动提取；GRPO + ReAct 决定「写/延迟/检索/丢弃」，已集成 MemOS |

### 3.3 评估基准

- **LoCoMo**（长期对话记忆）—— memvid/OpenViking/ZenBrain 的通用口径
- **LongMemEval-500**—— ZenBrain 报告
- **tau2-bench**—— agent 经验记忆（OpenViking 报告）
- **MemEye**—— 多模态记忆基准（反文本捷径）

## 4. 分类框架（做对标用）

**记忆类型**（langmem 口径 + 综述补充）：
- semantic（事实/概念）、episodic（事件/轨迹）、procedural（程序/技能）—— 三分类是行业共识
- 短时（working/上下文内）vs 长时（跨会话持久）

**记忆系统四模块**（arXiv 2606.06448）：
1. **representation & storage**（怎么存：SQLite/向量/图/单文件/记忆块/虚拟 FS）
2. **extraction**（怎么提取：LLM 蒸馏、被动/主动、GRPO 决策）
3. **retrieval & routing**（怎么召回：top-k/BM25/混合/图递归/目录递归/渐进披露）
4. **maintenance**（怎么维护：consolidation/forgetting/decay/生命周期/去重）

## 5. 多租户与 ACL 维度（OpenViking vs TencentDB，源码级）

> 补记（2026-09-07 深读）：此前横向对比时把「团队多租户」当成 TencentDB-Agent-Memory 的差异点——**这是错的**。OpenViking 同样有团队多租户，且是源码级实打实。本节把两边的多租户/ACL 实现都对齐到源码层，纠正该误判。

### 5.1 OpenViking 多租户（AGPLv3 / Python）`[源码]`

- **租户模型 Account→User**：`UserIdentifier(account_id, user_id)`（`openviking_cli/session/user_id.py`）——每个用户必属一个 account；`Role` 内置 `ROOT/ADMIN/USER` 三级 + `Role.register(name, rank)` 自定义角色带权限排名（`server/identity.py`）。
- **物理硬隔离**：`storage/viking_fs/_access.py` 把 `viking://{...}` 前缀替换为 `/local/{account_id}/{...}`，**所有 URI 落在 account 目录下**；同文件「Writing the account root requires an administrator」。
- **跨租户隔离有专门测试**：`examples/multi_tenant/admin_workflow.py` 第 10f 节「ADMIN cross-account isolation」验证 ADMIN 不能注册/查看/删除其他 account 的 user，只有 ROOT 跨租户。
- **三级命名空间**（`core/directories.py` + `namespace.py`）：`viking://resources`（account 共享资源域）/ `viking://user/{user_id}`（user 私有：memory/sessions/skills/peers）/ `viking://agent/skills`（全局 agent 技能根）。
- **ACL**（`storage/acl.py`）：principal 仅 `user:<id>` / `group:<id>` / `user:*`，level=`read/write/manage` 位掩码，direct + inherited 两级；**但 ACL 只作用于 `viking://resources`**，user 域是 user_id 硬隔离 + peer 归属，不开放 ACL。

### 5.2 TencentDB-Agent-Memory 权限模型（MIT / Node）`[源码]`

- **资产归属二元组 (team_id, owner)**：`MemoryCore/src/core/skill/skill-permission.ts` 的 `assertOwner`——`(teamId, agentId)` 唯一确定 ownership（注释明说「不同 team 下可能出现相同 agent_id」）；`assertTeamMatch` 不匹配返回 404（`SKILL_TEAM_MISMATCH`，防存在性侧信道）。
- **显式五态 visibility**：`MemoryCore/src/metadata/types.ts` 的 `AssetVisibility = "private" | "team" | "restricted" | "agent" | "task"`。
- **三段式判定**（`metadata/service/permission-checker.ts`）：`owner → 成员 → visibility → 角色默认 → ACL → deny`；角色默认 admin=`read/write/assign/share`，member=`read`。
- **agent 是一等 ACL 主体**：`acl.subject_type` ∈ {`user`, `team_role`, `agent`}，`effect=allow`，配合 `owner_agent_id`。

### 5.3 维度对比

| 维度 | OpenViking | TencentDB-Agent-Memory |
|---|---|---|
| 租户主体 | account_id，物理目录 `/local/{account}/` | team_id 字段 + 防侧信道 404 |
| 角色 | ROOT(全局)/ADMIN(account内)/USER + 自定义角色带 rank | admin/member + system_admin |
| Agent 是否一等主体 | ❌ peer 只是记忆归属维度，ACL principal 无 agent | ✅ subject_type=agent + owner_agent_id |
| 权限粒度 | user/group + read/write/manage，仅 resources 域 | user/team_role/agent + read/write/assign/share/use，全资产 |
| visibility 语义 | 无显式字段（命名空间 + ACL 表达） | 显式五态 private/team/restricted/agent/task |
| 判定顺序 | 命名空间隔离 → 角色门控 → ACL | owner → 成员 → visibility → 角色默认 → ACL → deny |

### 5.4 结论

两者维度互补而非替代：**OpenViking 强在 account 物理隔离 + ROOT/ADMIN 运维角色**（平台运维级）；**TencentDB 强在 visibility 五态 + 三段式判定 + agent 一等 ACL 主体**（资产权限语义级）。对 Clawith 的启示：Clawith 目前是「agent 自主写恒放行 + 门控只拦非维护用户」的二元模型（见 `20260907-skill-sedimentation-lifecycle-production-plan.md`），既没有 OpenViking 的 account 物理隔离，也没有 TencentDB 的 visibility 五态 + agent 一等 ACL 主体——若要对齐团队级多租户，两者都可作参照，但 **agent 一等主体 + visibility 语义是 TencentDB 更直接可迁移的点**。

## 6. 对 Clawith 的启示（诚实负结论）

1. **Clawith 现有记忆层（reflections/Insights/Hypotheses + Focus）落在「extraction + retrieval」两模块，缺「representation 生命周期」和「maintenance」**。对照版图，最直接的可迁移资产：
   - **agentmemory 的「confidence + lifecycle」**：给每条记忆打置信度 + 生命周期，正是 Clawith 缺的「记忆质量治理」；
   - **OpenViking 的五态 PolicyStatus（draft/staging/production/deprecated/archived）+ 语义梯度**：见前文，是最完整的「记忆/技能可演化 + 可审计」实现；
   - **beads 的「语义 decay compaction」**：旧任务记忆随时间衰减，可对标 Clawith 的 run_compactor；
   - **HippoRAG 2 的多跳联想检索**：Clawith 现在是语义 top-k 注入，可考虑补「联想/图」召回。
2. **诚实负结论**：openhuman/cognee 仍是 `[摘要]` 层（本地 clone 但未深读实现）；agentmemory/beads/memvid/HippoRAG 是 `[README]` 层；论文是 `[摘要]` 层（firecrawl 返回的 abstract，未读全文）。**源码级已核对的**：OpenViking（前文 + §5 多租户/ACL）、TencentDB-Agent-Memory（§5 多租户/ACL）、langmem/mem0/letta-code（前几轮）。要落地任何一条迁移点，需先 clone + read 源码。
3. **最该补读的一篇**：arXiv 2606.06448（四模块 + 12 系统对比）——它直接回答了「Clawith 记忆层该选什么架构」，且结论是「没有银弹，按瓶颈对齐」，比任何单库宣传都更该作为选型基线。

## 7. 引用

- 库：github.com/{mem0ai/mem0, volcengine/OpenViking, topoteretes/cognee, tinyhumansai/openhuman, TencentCloud/TencentDB-Agent-Memory, letta-ai/letta-code, rohitg00/agentmemory, gastownhall/beads, memvid/memvid, OSU-NLP-Group/HippoRAG, TeleAI-UAGI/telemem, matrixorigin/memoria, LycheeMem/LycheeMem, verygoodplugins/automem}
- 资料：github.com/IAAR-Shanghai/Awesome-AI-Memory、github.com/TeleAI-UAGI/Awesome-Agent-Memory
- 论文：arXiv 2606.06448 / 2507.21046 / 2601.02163 / 2603.04814 / 2604.23878 / 2604.16839 / 2604.07877
- 关联：`20260907-self-evolving-agents-landscape.md`、`20260828-self-evolution-best-practices.md`、`20260828-self-evolution-capability-research.md`
