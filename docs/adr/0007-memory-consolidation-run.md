# ADR-0007: 记忆整合层（Memory Consolidation Run）

> **已废止（2026-08-28）**：方向错误——发明新架构（独立 scheduled 整合 run）而非接通
> 既有记忆循环，且与票 07 门禁职责重叠。替代方案见 ADR-0008（P0 记忆循环接通）。

- **状态**: 已废止（2026-08-28，被 ADR-0008 取代）
- **前置**: ADR-0005（运行时记忆固化门禁，已部署已生产验证）
- **调研**: docs/technical-plans/20260828-self-evolution-best-practices.md

## 背景

票 07 门禁解决了「写入保证」（write-side guarantee），但记忆的**读侧与生命周期**仍缺一层：
`memory/memory.md` 只增不减不合并，无整合、无过期、无重要性标注。按 Anthropic
context rot 论述，记忆文件进入上下文也要消耗注意力预算，无限膨胀是慢性的确定性劣化；
按 LangMem 的 enrichment 原则，新记忆必须与旧记忆 reconcile（合并/更新/泛化），否则
over-extraction 降检索精度、under-extraction 降召回。Clawith 现有 D6 提示词只约束
「合并写入」，不约束整合——与票 07 的教训同构（纯提示词义务会被模型无视），需要运行时保证。

本 ADR 定案「整合」如何以运行时机制落地。设计原则：**接 LangMem 的思想（整合 prompt
模式），不接它的依赖与存储**——文件式记忆是 Clawith 已生产验证的核心资产，不换成
LangGraph store 黑盒。

## 决策（grill 定案，Q1–Q7）

| # | 决策 | 定案 | 全网最佳实践对应 |
|---|---|---|---|
| Q1 | 执行机制 | **独立整合 run**（单一目的 scheduled run，非热路径、非心跳 piggyback、非软提示词） | LangMem background formation（后台、不影响主交互）；票 07 教训（提示词义务被无视） |
| Q2 | 触发 | **阈值触发 + 最小间隔**（非固定频率） | Anthropic context rot（按膨胀程度治理，精准且总成本最低） |
| Q3 | 操作权限 | **软删除 + 归档区**：合并/泛化/更新过时 + `[deprecated]` 标记；无效内容移入文件底部归档区，禁止物理删除 | LangMem reconcile/update/delete 的保守变体；Mem0 update 阶段 |
| Q4 | 护栏 | A 自动留档（workspace_file_revisions 已有）+ B 两阶段强制（read 全部→输出方案→edit）+ C 失败重跑（本周期重试 1 次，再败跳过留痕）+ D 验收留痕（新事件，见下） | SI-Agents 3.x 评估分支（自进化必须可测量）；票 07 留痕哲学 |
| Q5 | 预算 | 整合 run `model_turn_limit=6`；每周期至多 2 次尝试 | 有界成本 |
| Q6 | 阈值 | `memory.md` 正文 >20K 字符触发；归档区 >正文 1/3 触发；同一 agent 两次整合 ≥24h | 实测分布校准：7 个 agent 当前最大 12.4K、中位 ~2K |
| Q7 | 与门禁交互 | 无冲突不特判：整合 run 写 `memory/` → `memory_writes>0` → 门禁不强制；整合 run 没写 → 无 workspace 写 → 直通 | 票 07 门禁计数天然兼容 |

## 实现形态

- **run kind**：`system_role="memory_consolidation"`（现库 `system_role` 仅
  NULL/`group_planning`，新值无冲突；群聊/委托不 carve-out 与票 07 一致）。
- **调度**：周期性扫描（复用 heartbeat scheduler 基建，如每 60 分钟），对每个满足
  Q6 阈值与最小间隔的 agent 创建一个整合 run；goal 为平台生成模板（含该 agent 的
  memory 路径），非用户输入。
- **模型**：agent 默认模型（改写记忆是高风险操作，质量优先，不上便宜模型）。
- **两阶段强制**：system prompt 强制「①read memory/memory.md + MEMORY_INDEX.md 全文
  → ②输出整合方案（合并重复/泛化/标注过时/列出归档项）→ ③edit 落盘并同步 INDEX」；
  归档区位于文件底部 `## Archived` 节。
- **验收留痕**：新事件类型 `memory_consolidated`（CHECK 白名单 + Alembic 迁移
  `v1_11_4_f073_*`），payload 记 before/after 字符数、合并条目数、归档条目数；run 结束
  运行时校验 memory.md 确实被 edit 才发该事件，否则发 `memory_consolidation_skipped`
  （复用票 07 事件，skip_reason="no_consolidation_needed"）。
- 整合 run 复用 `_MEMORY_MAINTENANCE`（D6）的「同步 MEMORY_INDEX」义务；整合后的
  归档区条目保留原文可检索，退出正文以控制每次注入上下文的体量。

## 测试

- 调度：超阈值创建 run、未超不创建、最小间隔内不重复创建、失败重试 1 次上限。
- 整合 run：两阶段强制（prompt 含强制序列）、归档区格式、INDEX 同步、turn_limit 生效。
- 验收：`memory_consolidated` 事件（payload 计数正确）；未改动时发 skipped 事件；
  CHECK 白名单 + 迁移 schema 断言（沿用票 07 迁移测试模式）。
- 门禁互操作回归：整合 run 不触发强制轮；整合写计入 memory_writes。
- 全量 pytest + `scripts/arch-guard.sh`。

## 回滚

代码 revert 即可；无既有数据改造（不迁移任何现有 memory 文件，归档区在首次整合时自然
生成）。`memory_consolidated` 事件类型的 Alembic downgrade 前需先删该类型行（与票 07
f072 同规约）。

## 后果

- 正：记忆膨胀受控（context rot 抑制）；过时信息被标注/归档而非残留误导；整合与门禁
  形成完整闭环（写保证 + 生命周期治理）；`memory_consolidated`/`skipped` 双事件构成
  G4 评估的地基（整合前后 diff 统计可基准化）。
- 负：整合 run 消耗少量 LLM 预算（阈值触发 + turn_limit 有界）；软删除不回收物理空间
  （文件只增不减的问题以「正文/归档分离」缓解而非根治——根治需检索打分（G5 后续）或
  物理删除加人工审核，暂不做）。
- 中性：新 system_role 值与事件类型为显式枚举，后续演进需迁移；群聊/委托 agent 统一
  适用（与票 07 一致，无 carve-out 成本）。

## 后续（非本 ADR 范围）

- G2 后台固化通道（与票 07 热路径 A/B）；G3 程序记忆（成功流程沉淀 skill，需人工
  审核门禁）；G4 记忆质量 benchmark（复用 agent-evaluation 基建 + 本 ADR 的事件信号）；
  G5 记忆检索打分（importance×recency）。
