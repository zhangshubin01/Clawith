# 生产级修复方案：自进化缺口闭合二期（Maintainer 门控 → G3 技能沉淀 → G4 评估）

日期：2026-09-05
状态：ready-for-implementation（8 个 grill 决策点已全部拍板；§8 边界已折入）
前置：`20260902-self-evolution-gap-closure-plan.md`（G1 已取消、G1' 已落地），
      `20260819-agent-maintainers-implementation-plan.md` + `-permission-model.md`（本方案重出核对表，其待确认 A/B/C/D 均已定）。

## 0. 接地数据（2026-09-05 实测，非凭旧报告）

| 维度 | 数据（近 30 天） | 结论 |
|---|---|---|
| 文件改工具调用量 | edit_file 1281 / write_file 904 / delete_file 32 / move_file 17 | **edit_file 是最大缺口**：当前零门控且量最大 |
| run 分布 | heartbeat 670 / chat 312 / trigger 32 / a2a 11（共 1025） | 后台 run 占 2/3，actor 常为 NULL |
| 跨 actor 流量 | **46 个 chat run 由非 creator 用户驱动**、13 个 a2a（actor_agent_id） | 门控有真实受众，非空想 |
| agent/用户 | 18 agent / 6 用户 / 3 个 creator | 多用户协作真实存在 |
| autonomy_policy | 11 个 delete=L3/write=L2，5 个 L1/L1，2 个 L2/L1 | delete 审批在真实使用 |
| approval_requests | 仅 delete_files（14 approved + 3 pending） | **write/edit/move 零记录 = 证实当前零门控** |

结论：Maintainer 门控优先、风险最高（46+13 个非 creator 驱动 run 能无门控改文档）。

## 1. 参考资料对比（reference-check 纪律）

| 决策点 | 参考项目 | 结论 / 偏离理由 |
|---|---|---|
| 「谁可写什么」判定 | letta-code `memory-confinement`（fail-closed、`cross-agent-guard`：可广读宿主、只写自记忆、检测不到沙箱即 throw） | **借鉴其「前缀+名单」思路，但明确偏离**：Clawith 定位「治理层非硬安全」——`execute_code` 经 `sync_back` 可绕过一切文件门控，故 fail-closed 无意义，只做文档工具门控 |
| 门控判定函数形态 | deepagents `middleware`（`_message_eviction`/`_prompt_caching` 中间件钩子） | 借鉴「在唯一执行边界加纯函数判定」，不新建 run kind/服务 |
| 路径防逃逸 | letta-code `memory-git` + Clawith 已有 `safe_agent_path` | 复用 `safe_agent_path`（`.resolve()` + startswith），不新写 |
| G3 技能沉淀 | Anthropic Agent Skills / skill-creator「草稿→评测→改进」+ letta-code memory-v2 自治子代理 | 采用**草稿 + 人工移动**（human-gate，同经验库 propose→publish 已验证模式），不做自动语义聚类 |
| G4 评估 | skill-creator `clawith_runner.py` + `aggregate_benchmark.py` + judge 平台（run_outcome/attempt_count） | 参考资料保留，但**本期不接 judge/A/B**（grill 决策 7）——先做只读门禁健康度周报，harness 留待数据积累后再启用 |

**无相关参考的类别（明示）**：多租户「非 creator 用户驱动 agent 改文件」的权限模型，参考清单里无逐字可抄项（dify 有租户权限但非 agent-actor 语义）——本方案按平台自身事实设计。

## 2. 代码基线（2026-09-05 HEAD `d4a2e081` 重核，以函数名定位、不依赖行号）

- 三处执行点：`RuntimeToolStepService.execute_pending`（`agent_runtime/tool_step_service.py:1993`，delete 闸门 `_delete_autonomy_gate` `:1901` 定义、`:2237` 调用）、`execute_builtin_tool_outcome`（`agent_tools.py:5511`，**无自主检查**）、`execute_tool`（`agent_tools.py:6044`，autonomy 检查 `:6092`）+ `check_tool_autonomy`（`agent_tools.py:29548`）。
- **两份 `_TOOL_AUTONOMY_MAP` 遮蔽（bug 1）**：`:2802`（意图版：write/move/delete/send_feishu/send_message_to_agent/send_file_to_agent/web_search/execute_code/execute_code_e2b）+ `:29539`（生效版：write/edit/delete/execute_code/execute_command）。模块内重复定义，运行时名字解析到**末次赋值 `:29539`** → **6 个工具完全无自主权检查**：`move_file`、`send_feishu_message`、`send_message_to_agent`、`send_file_to_agent`、`web_search`、`execute_code_e2b`（legacy `execute_tool` 与 ACP `check_tool_autonomy` 两路径都漏）。`edit_file` 在生效版已覆盖，**不缺**（修正旧「漏 edit_file」表述）。连带后果：`autonomy_policy` 里 `send_*`/`web_search` 键的唯一 enforce 点 `check_and_enforce`（`autonomy_service.py:64`）由 MAP 驱动 → 这些键的 L2/L3 分级**静默失效**。
- **ACP truthy bug（bug 2）**：`check_tool_autonomy` `:29578` `allowed = policy.get(category, True)`，policy 值是字符串 "L1"/"L2"/"L3" 恒真 → ACP 路径永不拦截。
- `_PATH_CONVENTION_PARAMS`（`builtin_tool_definitions.py:4103`）已含 write/delete/edit/move 全部路径参数 → 门控助手复用，不造第三份清单。
- `safe_agent_path`（`workspace_collaboration.py:107`）vs `normalize_workspace_path`（`:92`）——门控前缀判定必须走前者（symlink 感知）。
- `_is_group_scoped_workspace_call`（`agent_runtime/tool_step_service.py:694`）现为**三条件**：`_is_group_agent_run && tool in SCOPED_WORKSPACE_TOOL_NAMES && workspace_scope=="group"`（比 08-19 plan 多第三条件，接入时以当前为准）。
- 事件白名单 `models/agent_run_event.py:36` 有 `memory_consolidation_skipped`、**无 `memory_consolidated`**（G1 取消后未补）。
- 迁移 head = **f074**（f072=memory_consolidation_event、f073=read_dedup_n、f074=stall_guard），门控迁移编号 **f075**。
- actor 填充点：`heartbeat_runtime.py:198`（=triggered_by_user_id）、`channel_session.py:70`、`persistence.py`（actor_user_id/actor_agent_id 双字段）。
- **基线漂移记录（2026-09-05）**：`tool_step_service.py` 已从 `services/` 移入 `services/agent_runtime/`；`agent_run_event.py` 移入 `models/`；死代码 `_materialize_storage_workspace` 已删（`2a36df02`）。HEAD 曾前进 `a42d0167 → d4a2e081`（read-dedup 三连 + 上下文瘦身，`2dd3d08d`/`d4a2e081`），**均不碰门控文件**，上述行号已重核零漂移。

---

## 3. ① Maintainer 权限门控（G3 前置，优先级最高）

### 3.1 决策点

| # | 决策 | 状态 |
|---|---|---|
| M-1 | creator 隐式维护人员、不可移出；存量 agent 名单初始值 = creator | ✅ 已拍板（09-02 §7） |
| B | 非维护人员直接拒绝（`tool_permission_denied`），废弃 delete/modify 的 approval_request 流 | ✅ 已拍板（08-19 permission-model） |
| C | 门控 `workspace/`+`skills/`；`memory/` 放行；`soul.md`/`enterprise_info/` 走既有机制 | ✅ 已拍板 |
| D | 管理员 = `platform_admin` + `org_admin`（两者均可，API 鉴权 + 前端 canManage 复用） | ✅ 已拍板（grill 决策 5） |
| G-1 | a2a（`actor_agent_id` 非空）→ `NOT_GATED`：agent 间调用视为发起 agent 已授权，不在文件门控范围 | ✅ 已拍板（grill 决策 2） |
| G-2 | heartbeat/trigger 的 actor 回退 creator 放行（后台 run 本就不该被非 creator 驱动） | ✅ 已拍板（grill 决策 3） |
| G-3 | agent 自主删自己临时文件**不做例外**：硬拒 + 周报观察误伤率，超标再回退/调边界 | ✅ 已拍板（grill 决策 4） |
| G-4 | 门控定位 = 治理层（接受 `execute_code` 经 `sync_back` 可绕过） | ✅ 已拍板（grill 决策 1） |

### 3.2 数据模型 + 迁移（f075）

- 新表 `agent_maintainers`（`id` uuid PK / `agent_id` FK→agents / `user_id` FK→users / `created_by` / `created_at` / `updated_at`），`UNIQUE(agent_id, user_id)`，`CASCADE`，无 tenant_id（靠 agent_id 隐式隔离）。
- creator **不落表**、运行时隐式判定（`actor_user_id or agent.creator_id`），零回填成本。
- **autonomy_policy 键显式迁移清理（grill 决策 6）**：门控接管 delete/edit/write/move 后，`autonomy_policy` 里的 `delete_files` / `write_workspace_files` 键不再参与这四类工具判定——**在 f075 里显式迁移**（把这 11 个 agent 的这两个键归一为哨兵值或删除），不「保留但忽略」（否则 `check_and_enforce` 死代码 + 周报口径双混乱）。`read_files`/`send_*`/`web_search`/`execute_code` 等非文件键**保留不动**（仍走 `check_and_enforce` 分级）。
- DDL-only 迁移 + inspector 守卫 + 对称 downgrade（沿 `backend/alembic/AGENTS.md` 70-78 规范；C5 张力写进 commit message）。

### 3.3 `MaintainerService` + 判定助手（新 `maintainer_service.py`）

```python
class FileModifyDecision(Enum):
    GATED_ALLOWED / GATED_DENIED / NOT_GATED / DEFER

async def resolve_file_modify_permission(db, *, tool_name, arguments, agent,
    actor_user_id, is_group_scoped) -> FileModifyDecision
```

判定顺序（全部有代码依据）：
1. `actor_agent_id` 非空（a2a 驱动）→ `NOT_GATED`（grill 决策 2）。
2. `is_group_scoped` 为真 → `NOT_GATED`（组路径短路）。
3. `tool_name` ∉ {delete_file, edit_file, write_file, move_file} → `NOT_GATED`。
4. 用 `_PATH_CONVENTION_PARAMS` 取该工具全部路径参数，逐个 `safe_agent_path` resolve 后判前缀：
   - 任一落 `workspace/` 或 `skills/` → 进入门控；全落 `memory/` → `NOT_GATED`；命中 `soul.md`/`tasks.json`/`enterprise_info/` → `DEFER`；其余 → `NOT_GATED`。
5. `actor_user_id` 空 → 回退 `agent.creator_id`（heartbeat/trigger 的后台 run 回退 creator 放行，grill 决策 3）。
6. `is_maintainer(actor)` → `GATED_ALLOWED` / `GATED_DENIED`。

**关键**：`move_file` 的 `source_path` + `destination_path` **两路径都要判**（`move(memory/x → workspace/secret)` 只判源会漏）。

### 3.4 三处接入 + 两个 bug 一并修

| 调用点 | 改为 |
|---|---|
| `execute_pending`（durable，`:2237` 闸门前） | 对 delete/edit/write/move 统一调 `resolve_file_modify_permission`；`GATED_DENIED`→`tool_permission_denied` 结果；`DEFER`→走既有 soul/tasks/enterprise 拒绝（见下）；`NOT_GATED`→放行；`_delete_autonomy_gate` 的 L3 审批移除 |
| `execute_tool`（legacy，`:6092`） | `check_and_enforce` 替换为同一助手；补 edit_file（现漏） |
| `check_tool_autonomy`（ACP，`:29578`） | **修 truthy bug**（`policy.get(category, True)` → 先解 `level in ("L1","L2")` 才放行）+ 走同一助手；补 move_file |

**`DEFER` 分支的真实落点（雷 2，勿假设 modify_soul 审批）**：`modify_soul` 仅是 `DEFAULT_AUTONOMY_POLICY` 里的键（`agent.py:30`），**零执行点**——soul.md 的实际保护靠 delete/move 工具描述拒绝（`builtin_tool_definitions.py:188`「Cannot delete soul.md or tasks.json」/ `:205`「Cannot move soul.md…enterprise_info/」）。故 `DEFER` 分支**落到这条工具描述拒绝路径**，不新增、也不假设存在 `modify_soul` 审批流。`soul.md` 的门控后续若要硬执行，另行立项，不在本期。

**bug 修复（独立小票，先于门控落地）**：收敛两份 `_TOOL_AUTONOMY_MAP` 为一份，**补齐被遮蔽的 6 个工具映射**（move_file→write_workspace_files；send_feishu_message / send_message_to_agent / send_file_to_agent / web_search / execute_code_e2b → 各自 action_type），删除 `:2802` 遮蔽定义。运行时 `:6092` 与 `:29561` 都取模块末次赋值 `:29539`，故这 6 个键从未生效——收敛后 legacy 与 ACP 两路径恢复这些工具的自主权检查（连带恢复 autonomy_policy 里 send_*/web_search 的 L2/L3 分级）。

### 3.5 埋点 / 可观测性

- 拒绝路径走既有台账：`agent_tool_executions` 已记录每次工具调用结果（含 error_code），`tool_permission_denied` 会进入台账 + Langfuse tool span，无需新埋点。
- 新增**零事件**（治理层拒绝不是 run 生命周期事件，是工具结果）——避免 whitelist 膨胀。
- 周报脚本只读聚合 `agent_tool_executions` 中 `tool_permission_denied` 的 error_code 计数 + actor 分布，用于验证「非维护人员被正确拦截、维护人员零误伤」。

### 3.6 测试（TDD，denial 走真实 executor/mutation 边界）

1. `resolve_file_modify_permission` 纯函数：move 双路径、组短路、soul/tasks/enterprise DEFER、symlink 绕过被 `safe_agent_path` 挡、memory/ 放行、actor 空回退 creator。
2. durable 路径（`execute_pending`）：非维护人员 delete/edit/write/move → `tool_permission_denied`；维护人员/creator → 放行。
3. legacy 路径（`execute_tool`）：同上，补 edit_file 场景。
4. ACP 路径（`check_tool_autonomy`）：truthy bug 回归——`"L3"` 现在正确拦截。
5. 迁移：`agent_maintainers` schema + downgrade 对称（沿 `test_memory_consolidation_migration.py` 模式）。

### 3.7 回滚

- 行为级：无开关（治理层是硬契约）。代码级 revert；`agent_maintainers` 表可留（无消费方则删迁移，f072 同规约）。
- **产品契约变更提醒**：非维护人员驱动时 workspace 写被硬拒，属行为变更，需在 commit/release note 明示。

---

## 4. ② G3 程序记忆沉淀（依赖 ①，风险中高）

### 4.1 现状与决策

- 通道已拍板：**草稿 + 人工移动**（G3-1，09-02 §7）。最小实现 = 提示词义务 + human-gate，零新工具、零新状态机。
- **关键前置未达**：全库 reflections 的 Insights 仅个位数条目，「同一成功流程在 Insights 出现 ≥3 次」的触发前提当前不可达。

### 4.2 实现形态

1. `HEARTBEAT.md` Phase 3 追加一步（条件义务措辞，沿 D1a 纪律）：若某「成功流程」在 reflections Insights 中出现 ≥3 次 → 写 `workspace/skill-drafts/<skill-name>.md` 草稿（**不在 `skills/` 下**，避免被 `_load_skills_index` 扫进目录），草稿自带 `## 来源` 节（引 reflections 条目），heartbeat 总结里提示用户审核。
2. 人工审核 = 维护人员把草稿移入 `skills/<name>/SKILL.md`——此时 ① 门控恰好保证只有维护人员驱动下才写得进 `skills/`。
3. 模板迁移：改 `backend/app/templates/HEARTBEAT.md` + `agent_template/HEARTBEAT.md`（保持双模板一致），跑 `migrate_legacy_heartbeat_template.py` 按 SHA 迁移存量 agent。

### 4.3 决策：**G3 挂起观察，不急于开工**（grill 决策 8 已拍板）

理由：触发前提（≥3 次）在真实数据下不可达（全库 reflections Insights 仅个位数条目），先做等于写一条永不触发的提示词义务（重蹈 G1 原案「20K 阈值永不触」的覆辙）。**顺序定为**：先落地 ① 门控 + ③ 评估，让 reflections Insights 通过真实工作积累，待单 agent Insights 条目 ≥ 若干再启动 G3（数据驱动触发，不拍脑袋定时间）。

**草稿发现机制（挂起期间仍落地，低成本）**：heartbeat 总结里提示用户「存在待审草稿」，配套 P2 一个「待审草稿列表」视图（只读列出 `workspace/skill-drafts/` 内容）——不写草稿逻辑、不做自动移动，仅把发现权交给人。

### 4.4 测试 / 回滚

- 模板内容断言（Phase 3 含草稿步 + 条件义务措辞 + 来源节）；回滚 revert 模板 + SHA 回滚迁移。

---

## 5. ③ G4 记忆质量评估（收益 ★★ 风险低；**本期只做门禁健康度周报，不接 judge**——grill 决策 7）

### 5.1 信号源修正（G1 取消后的锚点）

09-02 原 G4 依赖「新增 `memory_consolidated` 事件」，但 G1 已取消、该事件从未落地。**修正**：不新增事件，用已有信号：

| 信号 | 来源 | 口径 |
|---|---|---|
| 固化率 | `agent_tool_executions`（write/edit 到 `memory/` 前缀）÷ 有 workspace 写的 run | 只读聚合，零新埋点 |
| 跳过率 | `memory_consolidation_skipped` 事件 ÷ 完成 run 数 | 已存在 |
| 拒绝率（新增观察） | `agent_tool_executions` 中 `tool_permission_denied` 计数 + actor 分布 | 验证 ① 门控效果 |

### 5.2 实现形态（最小脚本化，不进常驻基建）

1. `scripts/memory_quality_report.py`（只读周报，沿 `check-inflight-runs.sh` 同款模式）：输出三条曲线 + 门控拒绝 Top agent/actor。
2. **本期明确不做**：judge 评估 / `clawith_runner.py` A/B 基准 / 达标线自动化——这些留待门控落地、数据积累后再立项（grill 决策 7 收窄 G4 为「门禁健康度周报」，验证 ① 是否误伤，而非评估记忆质量本身）。

### 5.3 测试 / 回滚

- 脚本只读查询 + 输出格式断言；无迁移、无回滚面。

---

## 6. 执行序 + 已拍板汇总

```
P0 先 commit/stash 并行会话的工作区改动（agent_context.py 有未提交 read-dedup 改动，勿混叠）
 → bug 修复票：收敛两份 _TOOL_AUTONOMY_MAP（补齐 6 个被遮蔽工具映射）+ ACP truthy bug（独立、低风险、先行）
 → ① Maintainer 门控（f075 迁移[含 autonomy_policy 键清理] → MaintainerService → 三处接入 → API/前端 P2）
 → ③ G4 门禁健康度周报脚本（只读，随时可做，不阻塞）
 → ② G3 技能沉淀（挂起；草稿发现机制=heartbeat 提示 + P2 待审列表）
```

**grill 拍板汇总（8 项，均已折入正文）**：
1. 门控定位 = 治理层（接受 `execute_code` 可绕过）→ §3.1 G-4。
2. a2a（`actor_agent_id` 非空）→ `NOT_GATED` → §3.3 判定 1。
3. heartbeat/trigger 回退 creator 放行 → §3.3 判定 5。
4. 自产临时文件不做例外（硬拒 + 周报观察）→ §3.1 G-3 + §7 风险。
5. 管理员 = `platform_admin` + `org_admin` → §3.1 D + §8 雷 3。
6. autonomy_policy 键显式迁移清理 → §3.2。
7. G4 本期只做门禁健康度周报（不接 judge）→ §5。
8. G3 挂起；草稿发现 = heartbeat 提示 + P2 待审列表 → §4.3。

## 7. 全局护栏 + 风险

- 措辞纪律：G3 提示词用条件义务，禁祈使目标句（R1 循环教训）。
- 留痕纪律：门控拒绝走工具结果 error_code（`tool_permission_denied`），不发明第二套事件。
- 最小实现：复用 `_PATH_CONVENTION_PARAMS` / `safe_agent_path` / 既有台账 / 既有 harness，不新增 run kind、服务、公共工具。
- **前置审计（开工 ① 前必须做，09-02 §8 遗留，但已收窄）**：逐入口审计 `context.actor_user_id` 在 direct/trigger/heartbeat/group/channel 的填充（heartbeat/trigger 常为 NULL → 回退 creator，grill 决策 3 已定）。a2a 已从审计清单移出——`actor_agent_id` 非空即 `NOT_GATED`（grill 决策 2），无需再问「驱动 agent 的 creator 是否维护人员」。审计产出只影响「actor 空值回退」是否真能兜住后台 run，不改变门控判定结构。
- **产品契约变更**：① 落地后非维护人员 workspace 写被硬拒，属行为变更，需 release note + 用户告知。
- 风险：门控可能误伤「agent 自主删自己临时文件」（08-19 事故根因）——已拍板**不做例外**（grill 决策 4）：memory/ 放行 + creator 隐式维护人员兜底，观察周报拒绝率若误伤偏高，回退或调路径边界。

---

## 8. 与既有权限控制的边界（新增门控不破坏现状，3 雷折入）

### 8.1 既有 6 层权限控制（2026-09-05 代码核实）

| # | 机制 | 位置 | 现状 |
|---|---|---|---|
| 1 | `autonomy_policy` L1/L2/L3 分级 | `autonomy_service.check_and_enforce` | 按 action_type 自动执行/通知/审批 |
| 2 | `approval_requests` 审批流（L3） | `autonomy_service.resolve_approval`（`:168`，仅 creator+platform_admin 可批） | 现仅 delete_files 在用（14 approved + **3 pending**） |
| 3 | 工具描述拒绝 soul/tasks/enterprise | `builtin_tool_definitions.py:188/205` | 靠 LLM 遵守描述，无硬执行点 |
| 4 | group-scoped workspace 短路 | `tool_step_service._is_group_scoped_workspace_call`（`:694`） | 组内调用不走门控 |
| 5 | ACP 路径 `check_tool_autonomy` | `agent_tools.py:29548` | IDE 插件端，**truthy bug 致永不拦截** |
| 6 | `execute_code` 沙箱 + `sync_back` 回写 | runtime 沙箱 | shell 逃逸面——门控定为「治理层」的依据 |

**新增门控（①）的定位**：接管第 1/2 层对 `delete/edit/write/move` 四类**文档工具**的判定，替换为「维护人员名单」；第 3/4/5/6 层**不动**。`check_and_enforce` 保留给非文件 action（`send_*`/`web_search`/`execute_code` 等）。

### 8.2 三个雷 + 折入处理

| 雷 | 现状（核实） | 折入处理 |
|---|---|---|
| **雷 1：3 条 pending delete_files 审批变孤儿** | 决策 B 废弃 delete 审批流后，现 3 条 pending `ApprovalRequest(action_type=delete_files)` 无人消费 → 对应 run 悬挂 | `resolve_approval` **保留 delete_files 分支不删**；f075 迁移时对存量 pending delete_files 审批**按 creator 直接 resolve**（reject + resume run 告知「门控已接管」），不新增审批流。新 delete 调用不再产生审批（走门控硬拒） |
| **雷 2：`modify_soul` 空头支票** | `modify_soul` 仅是 `DEFAULT_AUTONOMY_POLICY` 键（`agent.py:30`），**零执行点**；soul.md 实际保护 = 第 3 层工具描述拒绝 | 门控 `DEFER` 分支落到第 3 层工具描述拒绝路径（§3.4 已写），**不假设存在 modify_soul 审批**；`modify_soul` 硬执行另行立项 |
| **雷 3：两套「谁能批」判定并存** | `resolve_approval` 只认 creator+platform_admin；门控管理 API 拟用 platform_admin+org_admin（决策 D） | **文档化并存**：审批流（历史机制）仍 creator+platform_admin；门控管理 API（`GET/POST/DELETE /api/agents/{id}/maintainers`）用 platform_admin+org_admin。两者不合并——审批流是运行态裁决，维护人员管理是治理配置，归属不同权限主体，写进 API 文档 + release note |

**验收红线**：① 落地后 `approval_requests` 不再新增 delete_files 记录；3 条 pending 已 resolve；`agent_maintainers` 管理 API 鉴权含 org_admin；非维护人员 edit_file（最大量、现零门控）被 `tool_permission_denied` 拦截。
