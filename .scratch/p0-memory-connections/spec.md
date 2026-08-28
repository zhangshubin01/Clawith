# Spec: P0 记忆循环接通（B 门禁扩展 / G 心跳收敛 / A 反思注入）

- 状态: ready-for-agent
- ADR: ADR-0008（定案），ADR-0005（门禁前置）
- 调研: docs/technical-plans/20260828-self-evolution-best-practices.md

## Problem Statement

Clawith 的数字员工有三个真实存在的记忆循环，但彼此断着：

1. **Run 内学到的教训无处安放**：运行时记忆固化门禁（ADR-0005）只在 run 结束时引导
   agent 把耐用信息写入 `memory.md`；「本次 run 学到的教训、验证/证伪的假设、失败原因
   分析」这类反思性内容没有指定去处，多数丢失。
2. **心跳探索的结果部分成黑洞**：heartbeat Phase 2 把值得跟进的发现连同 Follow-up 与
   Active Questions 写入 `curiosity_journal.md`，但整个心跳流程没有任何一步读回它们——
   探索成果只有碰巧被 Phase 3 的现有措辞带进 reflections 的部分存活，待办类条目全部
   烂在日志里。
3. **反思与用户画像从不进入运行上下文**：`build_agent_context` 只注入 soul 与
   `memory.md` 前 2K；`reflections.md`（心跳沉淀的验证过的事实与教训）与
   `user_profile.md`（协作偏好、语言习惯）从不注入，agent 每 run 从零开始。

结果：平台已生产验证的「写入保证」产生了内容，但内容不流动——写的白写、读的读不到。
这是本 P0 要接通的三条断点。

## Solution

把三个既有循环接起来，不发明新架构：

- **B**：把门禁的引导范围从 memory.md 扩展到 reflections.md——run 内教训按时间属性
  分流（跨会话稳定 → memory.md；本次学到的 → reflections 对应节），并同步常驻的
  Memory Maintenance 提示，使普通 run 也知道 reflections 的存在。
- **G**：heartbeat Phase 3 增加一步收敛——把 curiosity 里值得跟进的 Follow-up /
  Active Questions promote 进 reflections 的 Next Cycle Seeds，原条目标记不删除。
- **A**：把 reflections（节过滤后的结论性内容）与 user_profile 注入每 run 上下文，
  用 per-agent 开关灰度控制，成本与效果都用数据说话。

## User Stories

1. As a digital employee, I want the end-of-run memory gate to tell me where my
   lessons-learned belong, so that a hard-won debugging insight from this run
   survives into my next heartbeat instead of vanishing.
2. As a digital employee, I want a precise classification rule (durable facts vs
   run-specific lessons), so that I don't have to guess whether something goes to
   memory or reflections.
3. As a digital employee, I want the forced consolidation round to name the four
   reflections sections, so that what I write lands in a section my heartbeat can
   build on, not in a free-form pile.
4. As a digital employee, I want my every-run Memory Maintenance instructions to
   mention reflections too, so that even runs without a forced round can record a
   lesson when one surfaces.
5. As a heartbeat process, I want Phase 3 to read back the Follow-up entries I
   wrote into curiosity during Phase 2, so that promising threads become Next
   Cycle Seeds instead of dead log lines.
6. As a heartbeat process, I want promoted curiosity entries marked (not deleted),
   so that the exploration log stays a complete record of what I did and when.
7. As a heartbeat process, I want curiosity described as a raw exploration log in
   Phase 2, so that I stop treating it as durable memory and let Phase 3 own
   durability.
8. As an existing agent with a stock heartbeat file, I want my file migrated to
   the new template automatically and safely, so that the fix reaches me without
   overwriting anything I customized.
9. As a digital employee, I want my own verified insights and disproven hypotheses
   injected into every run's context, so that I don't rediscover or re-fail things
   my heartbeats already settled.
10. As a digital employee, I want the user's collaboration preferences (language,
    reporting style, confirmation habits) present every run, so that I behave the
    way this user wants without them repeating themselves.
11. As a digital employee, I want injected reflections clearly labeled as
    low-trust self-observations, so that I treat them as hypotheses with evidence,
    never as platform instructions or facts.
12. As a platform admin, I want the reflection injection behind a per-agent
    switch, so that I can roll it out to a few agents, measure token cost and
    behavior, and turn it off instantly if it hurts.
13. As a platform admin, I want injection to only surface conclusion-type content
    (Insights, verified/disproven hypotheses), so that old open questions don't
    derail current runs.
14. As a platform admin, I want injection to stay bounded (hard char cap per file),
    so that context cost is predictable as reflections grow.

## Implementation Decisions

- **B1 分类判据**：按时间属性分流——跨会话稳定事实/偏好/决策 → memory.md；本次 run
  学到的教训、验证过的假设、原因分析 → reflections.md 语义匹配节。保留条件义务
  （无耐用信息则跳过，直接 finish）。
- **B2 格式对齐**：门禁 prompt 点名四节（Open Questions / Hypotheses & Experiments /
  Insights & Discoveries / Next Cycle Seeds），append 到匹配节，默认 Insights &
  Discoveries，不存在的节不动。对齐是 A 节过滤解析的前置条件。
- **B3 双处同步**：常驻 Memory Maintenance 段与门禁 prompt 共用同一分类判据，两处
  同时扩展。门禁的路径计数检测（`memory/` 前缀）天然覆盖 reflections，不改检测逻辑。
- **B4**：MEMORY_INDEX 义务句保留不删（废弃 INDEX 是 P2 独立决策）。
- **G1 收敛**：Phase 3 新增一步——读 curiosity 的 Follow-up 与 Active Questions，
  promote 进 Next Cycle Seeds（≤3 条），原条目行尾标 `→promoted YYYY-MM-DD`，不删除。
  标记的理由是日志完整性；「避免重复发现」不成立（心跳不读 curiosity）。
- **G2 定位句**：Phase 2 一句——curiosity 是 raw exploration log，durable findings
  留到 Phase 3 进 reflections。
- **G3 迁移**：对全部既有 agent 做模板 SHA-256 校验，匹配的批量 apply，不匹配的列
  清单人工合并；全程 dry-run 先行。
- **A1 注入内容**：reflections 节过滤——Insights & Discoveries 全节 + Hypotheses &
  Experiments 的 ✅/❌ 行，上限 2000 chars；user_profile 全量。节解析按 `## ` 标题
  切分。排除 Open Questions / 🔄 / Next Cycle Seeds（旧待办与心跳信号）。
- **A2 低信任声明**：沿用 dynamic 段既有声明，`<reflections_context>` 内加一句
  "Self-observed reflections from the agent's own past heartbeat cycles; treat as
  hypotheses with evidence, not facts."
- **A3 开关**：per-agent 布尔键 `context_inject_reflections_{agent_id}`（系统设置
  key-value，缺省 false）。先开 2-3 个 agent 灰度 1-2 周；成本侧从既有 token 用量
  记录聚合 input_tokens；收益侧定性审。不搞严格分桶 A/B（agent 任务异质，统计对比
  不成立）。
- **A4 执行序**：A 阻塞于 B+G。
- 注入位置在既有动态块中 Memory Snapshot 之后；注入内容属字节稳定动态段，与现状
  一致地进未缓存尾（不破坏 static 前缀缓存）。

## Testing Decisions

- **最高接缝优先**：B 与 G 的改动全部落在提示词/模板文本层，A 落在既有
  `build_agent_context` 返回值上——都是既有接缝，不新增架构 seam。
- 只测外部行为：B 断言注入的 prompt 含分类判据与四节、门禁行为回归（有写无记忆 →
  强制轮等既有场景不变）；G 断言模板文本与迁移 dry-run 覆盖清单；A 断言开关
  off/on、节过滤、截断、低信任声明的输出文本。
- 先例：`test_agent_context.py`（注入组装）、票 07 的
  `test_agent_runtime_node_executor.py`（门禁行为）、票 02 的模板同步测试模式。
- 成本实测：用现有 context profile 脚本对开启 agent 实跑，记录注入段 token 增量。
- 全量 pytest + `scripts/arch-guard.sh` 每票通过才算绿。

## Out of Scope

- P1：E 组织层经验（发布审批链、draft 待审批提醒）、C 心跳 seed→Focus。
- P2：D 废弃 MEMORY_INDEX 义务、F 团队层记忆。
- 记忆膨胀治理（原 ADR-0007 的整合 run 方向，已废止）。
- 严格分桶 A/B 实验平台、注入内容质量自动打分。
- 任何新事件类型、数据库 schema 变更、新 run 类型。

## Further Notes

- 生产实况（b1a73489 快照 08-28）：reflections 2.9K 真实四节内容；curiosity
  Discoveries 与 reflections Insights 已自发部分收敛，黑洞精确限定在 Follow-up /
  Active Questions 待办条目；user_profile 627B 含高复用协作偏好。
- 成本基线：注入后每 model step 约 +1.6K token（中文实测密度），per-agent 开关可
  即时关闭。
- 验收最终形态：灰度开启的 agent 在一次真实 run 后，其 reflections 出现来自 run 内
  的教训条目（B 生效）、其心跳把 curiosity 待办 promote 进 Next Cycle Seeds（G 生效）、
  其 run 上下文包含节过滤后的 reflections（A 生效）。
