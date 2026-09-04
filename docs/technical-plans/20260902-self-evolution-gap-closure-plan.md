# 修复方案：自进化缺口闭合（G1 记忆整合 / Maintainer 门控 / G3 技能沉淀 / G4 评估）

日期：2026-09-02
状态：**决策点已拍板（2026-09-02，5/5 全部采纳推荐项），可进入实现**
基线：`docs/technical-plans/20260828-self-evolution-capability-research.md` 的 6 断点已复核——记忆闭环主线（写→门禁→注入）已全部接通，剩 4 个缺口。

## 0. 当前基线（2026-09-02 代码复核，非凭旧报告）

| 断点 | 现状 | 证据 |
|---|---|---|
| reflections 读回 | ✅ 已修 | `8beb4079` 移除开关，无条件注入（agent_context.py:577-583, 668-674） |
| MEMORY_INDEX 白写 | ✅ 已删 | 全库 0 命中；`_MEMORY_MAINTENANCE` 无索引条款（agent_context.py:422-437） |
| skill-creator 评测 | ✅ 已修 | `scripts/clawith_runner.py` 播种 + CLAWITH_EVAL_* 环境变量；seed 含 .pyc 运行产物 |
| 经验层接入 | ✅ 已修 | `a99296e1`，build_experience_hint（agent_context.py:605-611）+ 沉淀 human-gate |
| 待办进 run | ⚠️ 半通 | R1 清单落库 + D-11 任务状态桥（session_task_state.py）——**工作区未提交** |
| **G1 记忆整合层** | ❌ 无 | 无 Archived/consolidate 任何机制；memory.md 注入仍头截 2K（agent_context.py:546-554），只增不减 → context rot |
| **Maintainer 门控** | ❌ 0 代码 | `agent_maintainers`/`MaintainerService`/`resolve_file_modify_permission` 0 命中；write/edit/move 仍无门控（tool_step_service.py:1295 只挡 delete_file） |
| **G3 程序记忆沉淀** | ❌ 无 | 无自动技能沉淀；经验库有人工确认门，技能无通道 |
| **G4 记忆质量评估** | ❌ 无 | skill-creator benchmark 聚合可用，但 `memory_consolidation_skipped` 只是留痕不是反馈信号 |

## 1. 执行序与依赖

```
P0 落地工作区未提交的 D-11 任务状态桥（先 commit 或 stash，避免与后续改动混叠）
 → G1 记忆整合（独立、低风险、慢性劣化止血，先行）
 → Maintainer 门控（G3 的前置：skills/ 写入需要名单判定）
 → G3 程序记忆沉淀（依赖门控 + human-gate）
 → G4 评估闭环（依赖 G1 产生整合前后 diff 数据）
```

依赖理由：G3 允许 agent 产出技能文件，若无 Maintainer 门控，非维护人员驱动的 run 也能把草稿装进 `skills/`（`_load_skills_index` 扫描 `skills/` 下任意含 SKILL.md 的子目录，agent_context.py:121-141），劣质技能直接污染后续所有 run——必须先前置门控。

## 2. G1 记忆整合层（收益 ★★★ 风险低，最该做）

**原则：接通既有循环，不发明新架构**（ADR-0007 废止教训：独立 scheduled 整合 run 被否，因为与门禁职责重叠且另起炉灶）。心跳是既有自演化 run，天然是整合的家。

### 决策点

| # | 决策 | 推荐 | 理由 |
|---|---|---|---|
| G1-1 | 执行机制 | **心跳 piggyback**：`_build_heartbeat_instruction`（heartbeat.py:62）按阈值追加整合指令段，不新建 run kind | 复用既有调度与 reflection 循环；ADR-0007 的「两阶段强制 + 归档区」设计全部可平移，只换载体 |
| G1-2 | 触发 | memory.md 正文 > **20K 字符**且距上次整合 ≥24h（沿用 ADR-0007 Q6 实测校准：7 agent 当时最大 12.4K、中位 ~2K） | 按膨胀程度治理，总成本最低 |
| G1-3 | 操作 | 软删除：合并重复/泛化/标注过时，移入文件底部 `## Archived` 节；**禁止物理删除** | LangMem reconcile 保守变体；workspace_file_revisions 已天然留档 |
| G1-4 | 验收留痕 | 新事件 `memory_consolidated`（CHECK 白名单 + Alembic，延续 f072 模式），payload 记 before/after 字符数、合并/归档条数；未改动发 `memory_consolidation_skipped`(skip_reason="no_consolidation_needed") | 可审计、是 G4 的数据地基 |
| G1-5 | 强制语义 | 提示词用条件义务措辞（"if the file exceeds… then reconcile…"），**不写成祈使目标句**；整合失败不阻塞心跳 | R1 循环教训（repair-spec D1a）+ 票 07 措辞纪律 |

### 实现形态（代码 seam，全部已核实存在）

- `heartbeat.py::_build_heartbeat_instruction`：追加「若 memory.md 超过 20K 字符：read 全文 → 输出整合方案 → edit 落盘合并 + 归档 + 头部保持高价值条目」段（两阶段强制语义沿用 ADR-0007 §实现形态）。
- 阈值与最小间隔状态放 `SystemSetting`（复用 `context_inject_reflections_*` 同款 key-value 基建，读端在 build/heartbeat 已有 DB 会话）；不做新表。
- 事件：`agent_run_event.py` CHECK 白名单 + `checkpoint_side_effects.py::_record_lifecycle_events` 发射（复用 f072 幂等键模式 `checkpoint:{id}:memory_consolidated`）。
- 头部价值序：整合后 `memory.md` 正文按「最近使用优先」排序，使既有 2K 头截断（agent_context.py:546-554）读到的是热数据——**不改读侧代码**，零上下文装配改动。

### 测试 / 回滚

- 测试：阈值判定（超/未超/间隔内不重复）、prompt 常量含归档区与条件义务措辞、事件 payload 计数、CHECK 白名单 + 迁移 schema（沿用 `test_memory_consolidation_migration.py` 模式）、门禁互操作回归（整合写计入 memory_writes，不触发强制轮）。
- 回滚：代码 revert + SystemSetting 置空即停；事件类型 Alembic downgrade 前先删该类型行（f072 同规约）。

### 范围外

- 物理删除、检索打分（G5）、memory.md 结构化 schema——都不做（见 20260827 repair spec §Out of Scope 与 best-practices §4）。

## 3. Maintainer 权限门控（G3 前置）

**不重设计。** 两份既有 plan 已完成逐代码核对，本方案只补「执行」部分：

- `docs/technical-plans/20260819-agent-maintainers-implementation-plan.md`（v2，代码级核对，状态：待实施）
- `docs/technical-plans/20260819-agent-maintainers-permission-model.md`（设计草案，核心决策已确认，剩「待确认 A」）

### 实施清单（从 implementation-plan 直接摘取，不重抄全部细节）

1. 新表 `agent_maintainers`（UNIQUE(agent_id, user_id)）+ Alembic 迁移；admin 增删名单的 API + 测试。
2. 门控判定复用 `_PATH_CONVENTION_PARAMS`（builtin_tool_definitions.py 现有映射，含 move_file 的 source_path/destination_path）提取路径；前缀判定走 **`safe_agent_path` 语义**（resolve 后判前缀，防 `memory/link→workspace/x` symlink 绕过）。
3. 三处接入点（implementation-plan §1.5 已核实）：
   - durable runtime：`RuntimeToolStepService.execute_pending`（tool_step_service.py:1295）——现在只挡 delete_file，补 write/edit/move；
   - durable 执行器：`execute_builtin_tool_outcome`（agent_tools.py:3071）——本身无自主检查，需在唯一执行边界加判定；
   - legacy：`execute_tool`（agent_tools.py:3519）→ `check_and_enforce`（:3562）；ACP：`check_tool_autonomy`（agent_tools.py:24990）。
4. 顺手修已核实的两个 bug：ACP `allowed = policy.get(category, True)` 把 `"L3"` 当真值 → 永远放行；两份 `_TOOL_AUTONOMY_MAP`（:1591/:24981）互相矛盾（一个漏 edit_file、一个漏 move_file）。
5. 门控范围：`workspace/` + `skills/` 门控；`memory/`、`soul.md` 放行；组工作区短路（`_is_group_scoped_workspace_call`，tool_step_service.py:461）；`execute_code` 明确不门控（治理层非硬安全边界，permission-model §2）。
6. 非维护人员：直接拒绝 `tool_permission_denied`，**废弃** delete/modify 的 approval_request 流（已确认 B）。

### 决策点待拍板

| # | 决策 | 推荐 |
|---|---|---|
| M-1 | 待确认 A：creator 是否始终隐式维护人员且不可移出 | **是**（避免 creator 丧失对自己 agent 的控制） |
| M-2 | 存量 agent 名单初始值 | creator 即名单（与 M-1 一致），零迁移成本 |

### 测试 / 回滚

- 按 backend/AGENTS.md Enforcement 要求：denial 测试必须走真实 executor/mutation 边界（tool_step_service 与 agent_tools 两条执行路径各测非维护人员拒绝、维护人员放行、memory/ 与 soul.md 放行、symlink 绕过被 safe_agent_path 挡住、组工作区短路）。
- 回滚：代码 revert；表可留（无消费方则删迁移）。

## 4. G3 程序记忆沉淀（收益 ★★★ 风险中高）

**最小实现 = 提示词义务 + human-gate，零新工具、零新状态机**（经验沉淀的「草稿→人工确认」是仓库内已验证的模式，同构复用）：

1. HEARTBEAT.md Phase 3 追加一步：若某「成功流程」在 reflections 的 Insights 中出现 ≥3 次，把 SKILL.md 草稿写入 **`workspace/skill-drafts/<skill-name>.md`**（不在 `skills/` 下——`_load_skills_index` 会扫进目录，agent_context.py:121-141），并在 heartbeat 总结里提示用户审核。
2. 人工审核 = 维护人员把草稿移入 `skills/<name>/SKILL.md`（此时 Maintainer 门控恰好保证只有维护人员驱动下才写得进 skills/）。
3. 草稿文件自带 `## 来源` 节（引 reflections 条目），供审核对照。
4. 措辞为条件义务（"only when a workflow has proven itself ≥3 times…"），沿用 D1a 纪律。

### 决策点待拍板

| # | 决策 | 推荐 |
|---|---|---|
| G3-1 | 沉淀通道 | **草稿 + 人工移动**（零新机制）；备选：新增 `propose_skill_draft` 工具（不推荐——新公共工具需真实消费者，且 human-gate 已够） |
| G3-2 | 「重复三次」的判定载体 | **reflections Insights 的显式条目计数**；不做自动语义聚类（超出最小实现） |

### 测试 / 回滚

- 模板内容断言（Phase 3 含草稿步 + 条件义务措辞 + 来源节要求）；无运行时改动则不涉及 executor 测试。
- 回滚：revert 模板 + 迁移脚本按 SHA 回滚（G3 沿用 heartbeat 模板迁移先例 `migrate_legacy_heartbeat_template.py`）。

## 5. G4 记忆质量评估（收益 ★★ 风险低）

1. **信号侧**：`memory_consolidation_skipped` 与新增 `memory_consolidated` 事件聚合为每周趋势报告（复用 `scripts/` 运维脚本模式，如 `check-inflight-runs.sh` 同款只读查询；不做新服务）。
2. **A/B 侧**：复用现已平台化的 skill-creator benchmark harness（`clawith_runner.py` + `aggregate_benchmark.py`）跑「同任务有 memory vs 无 memory / 整合前后」二次执行对比——**先脚本化手工跑**，不进常驻基建（AGENTS.md 公开选择原则：没有当前消费者就不建平台功能）。
3. 达标线先行：固化率（`memory_consolidation_skipped` 占比）与整合率（consolidated/触发数）两条曲线，只读观测，不做自动动作。

### 测试 / 回滚

- 脚本只读查询 + 输出格式断言；无迁移、无回滚面。

## 6. 全局护栏（四项共用）

1. 措辞纪律：所有新增 prompt 用条件义务，禁祈使目标句（R1 循环教训）。
2. 留痕纪律：可观察事件走 CHECK 白名单 + 幂等键（f072 模式），不发明第二套。
3. 最小实现：优先复用既有 seam（心跳指令装配、SystemSetting、human-gate、benchmark 脚本），不新增 run kind、服务或公共工具；每项改动带正负测试。
4. 文档同步：定案后补 ADR（Maintainer 门控取代 L1/L2/L3 属运行时语义变更；G1 心跳整合属模型可见契约变更），技术计划 + ADR + 提交信息三处一致。

## 7. 决策点汇总（2026-09-02 已拍板）

| # | 决策 | 定案 |
|---|---|---|
| G1-1 | 执行载体 | ✅ 心跳 piggyback（_build_heartbeat_instruction 追加整合段） |
| G1-2 | 触发阈值 | ✅ 正文 > 20K 字符且距上次整合 ≥ 24h |
| M-1 | creator 隐式维护人员 | ✅ 是，且不可移出；存量 agent 名单初始值 = creator |
| G3-1 | 沉淀通道 | ✅ 草稿写 workspace/skill-drafts/ + 人工移动入 skills/ |
| G4 | 评估形式 | ✅ 最小脚本化（只读周报 + clawith_runner 手工 A/B） |
## 8. 评审修订（2026-09-02，结合真实执行日志）

对照 21 个 agent 的真实记忆文件（memory.md 最大 1561B、次大 596B、其余 61B 模板；reflections 最大 1387B）与
`docs/analysis/2026-09-01-task-step-inflation.md`、`docs/technical-plans/20260826-context-slimming-best-practices.md`，
本方案 G1 一节作如下修订：

1. **G1 根因错位，原案取消**：memory.md 无任何文件逼近 20K 阈值，归档整合机制（SystemSetting + memory_consolidated 事件 + 迁移）在今日数据下永不触发，属多余建设。真实上下文劣化源是「reflections 旧 ✅/❌ 结论行无裁剪 + 每步全量注入 + prefix_cache_break」（09-01 分析根因 B、08-26 slimming 调研）。
2. **G1 替换为 G1'（注入块瘦身）**：扩展 `_extract_reflections_injection`（agent_context.py:40-80，现无数量上限）——Hypotheses 结论行加 recency/数量上限（如最近 N 条），Insights 按时间倒序；配合 08-26 slimming 的缓存边界结论。零新机制、零迁移。memory.md 归档推迟到实测逼近阈值再评估（YAGNI）。
3. **Maintainer 门控资料需重核**：08-19 plan 符号全部存活但行号全部漂移（`_delete_autonomy_gate` 1295→1876、`execute_builtin_tool_outcome` 3071→5248、`check_and_enforce` 3562→5838、`check_tool_autonomy` 24990→29285、`_TOOL_AUTONOMY_MAP` 1591/24981→2569/29276、`_is_group_scoped_workspace_call` 461→693）；ACP 真值 bug 与两份 MAP 矛盾仍在（agent_tools.py:29315/2569/29276）。实施前按新行号重出核对表，并先审计 `context.actor_user_id` 在 direct/trigger/heartbeat/group/channel 各入口的填充情况（缺省回退 creator 可能使门控空转）。
4. **执行序调整**：P0（D-11 落地）→ G1'（注入瘦身，接 09-01 根因 B）→ Maintainer 门控（含产品影响确认：非维护人员驱动时 workspace 写被硬拒，属产品契约变更）→ G3/G4 视数据再定（当前全库 Insights 仅个位数条目，G3 的「≥3 次」前提暂不可达）。
5. **已拍板决策受影响**：G1-1（心跳 piggyback）与 G1-2（20K/24h）随 G1 原案取消而作废；其余三项拍板不变。
