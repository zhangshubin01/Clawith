# 生产级修复方案：自进化缺口闭合二期（Maintainer 门控 → G3 技能沉淀 → G4 评估）

日期：2026-09-05
状态：implemented（核心门控已落地 `aa539999`；Phase 5 code-review 双轴已跑，2 项硬违规已修 `后续 commit`；symlink 偏离按 G-4 拍板记录在案；剩余验收红线项见文末「Phase 5 审查结论」）
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
| **ACP 写工具 IDE 端把关（新增决策点）** | claude-agent-acp `docs/permission-extension.md`（ACP 协议权限模型，2026-09-05 读真实源码） | 标准 ACP 里 Edit/Write 走 `session/request_permission`（客户端渲染 allow-once/reject 确认）；Clawith 自定义 RPC `fs/write_text_file`/`fs/edit_text_file` **直写、无权限请求**，仅 `fs/safe_delete` 走 IDE `requestPermissions` 弹窗——是**对协议的偏离**。据此「写工具裸写」定性为既存缺口、有意为之（§8.3） |
| 多租户授权模型 | bisheng（Clawith 上游同 org，`docs/technical-plans/20260905-bisheng-study.md`）OpenFGA 细粒度授权 | 借鉴「owner>manager>editor>viewer 金字塔」思想，但本期用 **maintainer 名单**而非 OpenFGA 16 类型（最小改动，门控是治理配置非通用授权） |
| HITL 审批 UX | langchain-ai agent-inbox（HumanInterrupt 四开关 accept/edit/respond/ignore，`20260903-agent-inbox-hitl-ux-study.md`）+ langgraph `human_in_the_loop`（interrupt 人工确认） | 审批流对照：agent-inbox 零后端纯前端、四开关比 Clawith 二元 approve/reject 通用；但 Clawith 审批流是**运行态裁决**、门控是**治理配置**，两者归属不同权限主体、不合并（§8.2 雷 3） |
| 命令/工具风险分类 | jcode（Safe/Low/Confirm/Catastrophic 确定性 blast-radius 分类，`20260905-jcode-study.md`） | `execute_command` 已有 `_guard_acp_dangerous_command` 黑名单 = 确定性前置层；门控复用此思想（确定性判定、不引入 LLM 法官）。删 `check_tool_autonomy` **不影响**该黑名单 |
| 所有权边界 | 12-factor-agents（ownership boundary 原则） | 门控 = 把「谁拥有 workspace/skills 写权限」显式化为 maintainer 名单，对齐 ownership boundary，而非堆审批流 |

**无相关参考的类别（明示）**：多租户「非 creator 用户驱动 agent 改文件」的**权限主体语义**，参考清单里无逐字可抄项——bisheng 有 OpenFGA 但授权主体是用户/角色、非「agent-actor」；dify 有租户权限但非 agent-actor 语义。本方案按平台自身事实设计（a2a→NOT_GATED、非 creator 驱动→门控判定），此偏离已在各决策点写明理由。

## 2. 代码基线（2026-09-06 HEAD `49806eb2` 重核，以函数名定位、行号仅作约）

- 三处执行点：`RuntimeToolStepService.execute_pending`（`agent_runtime/tool_step_service.py:2005`，delete 闸门 `_delete_autonomy_gate` `:1913` 定义、`:2249` 调用）、`execute_builtin_tool_outcome`（`agent_tools.py:5612`，**无自主检查**）、`execute_tool`（`agent_tools.py:6145`，autonomy 检查 `:6193`）+ `check_tool_autonomy`（`agent_tools.py:29649`）。
- **两份 `_TOOL_AUTONOMY_MAP` 遮蔽（bug 1）**：`:2890`（意图版 9 键：write/move/delete/send_feishu/send_message_to_agent/send_file_to_agent/web_search/execute_code/execute_code_e2b，注释已改「用于 policy lookup and notifications / 避免误导通知标题」）+ `:29640`（生效版 5 键：write/edit/delete/execute_code/execute_command）。模块内**同名重复定义**，运行时名字解析到**末次赋值 `:29640`** → **6 个工具完全无自主权检查**：`move_file`、`send_feishu_message`、`send_message_to_agent`、`send_file_to_agent`、`web_search`、`execute_code_e2b`（legacy `execute_tool` 与 ACP `check_tool_autonomy` 两路径都漏）。`edit_file` 在生效版已覆盖、**不缺**。连带后果：`autonomy_policy` 里 `send_*`/`web_search` 键的唯一 enforce 点 `check_and_enforce`（`autonomy_service.py:51`，`:68` `level = policy.get(action_type, "L1")`）由 MAP 驱动 → 这些键的 L2/L3 分级**静默失效**。**另注意**：`:2890` 意图版的「通知标题」目标因被 `:29640` 覆盖而**永不生效**——重复定义的表象已从「两份 MAP」升级为「看似有意、实际被覆盖」。
- **两套自主权机制（勿混）**：legacy `execute_tool` 走 `autonomy_service.check_and_enforce`（**真分级** L1/L2/L3，缺键回退 L1）；ACP 路径走 `check_tool_autonomy`（见 bug 2，废的）。两者**解耦**——删 `check_tool_autonomy` 不影响 legacy 分级。
- **ACP truthy bug（bug 2）**：`check_tool_autonomy` `:29679` `allowed = policy.get(category, True)`，policy 值是字符串 "L1"/"L2"/"L3" 恒真 → ACP 路径永不拦截。且 `args`/`user_id`/`notify` 三参数函数体**未使用**（`:29649-29682` 只用 `tool_name`+`agent_id`）→ 死代码。
- `_PATH_CONVENTION_PARAMS`（`builtin_tool_definitions.py:4103`）已含 write/delete/edit/move 全部路径参数 → 门控助手复用，不造第三份清单。
- `safe_agent_path`（`workspace_collaboration.py:107`）vs `normalize_workspace_path`（`:92`）——门控前缀判定必须走前者（symlink 感知）。
- `_is_group_scoped_workspace_call`（`agent_runtime/tool_step_service.py:704`）现为**三条件**：`_is_group_agent_run && tool in SCOPED_WORKSPACE_TOOL_NAMES && workspace_scope=="group"`（比 08-19 plan 多第三条件，接入时以当前为准）。
- 事件白名单 `models/agent_run_event.py:36` 有 `memory_consolidation_skipped`、**无 `memory_consolidated`**（G1 取消后未补）。
- 迁移 head = **f076**（f072=memory_consolidation_event、f073=read_dedup_n、f074=stall_guard、f075=runtime_activity_enum、f076=no_progress_enum），门控迁移编号 **f077**（f075 已被 runtime_activity_enum 占用）。
- actor 填充点：`heartbeat_runtime.py:198`（=triggered_by_user_id）、`channel_session.py:70`、`persistence.py`（actor_user_id/actor_agent_id 双字段）。
- **基线漂移记录（2026-09-06）**：`tool_step_service.py` 已从 `services/` 移入 `services/agent_runtime/`；`agent_run_event.py` 移入 `models/`；死代码 `_materialize_storage_workspace` 已删（`2a36df02`）。HEAD 曾 `d4a2e081 → 49806eb2`（compactor 文案/摘要修正、workspace flush、model-capabilities、activity/no-progress 枚举补值等 ~10 commit），**均不碰门控文件**；上述行号按 `49806eb2` 重核，漂移幅度 `execute_pending` +12、`execute_tool` +101、`check_tool_autonomy` +101。

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

### 3.2 数据模型 + 迁移（f077）

- 新表 `agent_maintainers`（`id` uuid PK / `agent_id` FK→agents / `user_id` FK→users / `created_by` / `created_at` / `updated_at`），`UNIQUE(agent_id, user_id)`，`CASCADE`，无 tenant_id（靠 agent_id 隐式隔离）。
- creator **不落表**、运行时隐式判定（`actor_user_id or agent.creator_id`），零回填成本。
- **autonomy_policy 键显式迁移清理（grill 决策 6）**：门控接管 delete/edit/write/move 后，`autonomy_policy` 里的 `delete_files` / `write_workspace_files` 键不再参与这四类工具判定——**在 f077 里显式迁移**（把这 11 个 agent 的这两个键归一为哨兵值或删除），不「保留但忽略」（否则 `check_and_enforce` 死代码 + 周报口径双混乱）。`read_files`/`send_*`/`web_search`/`execute_code` 等非文件键**保留不动**（仍走 `check_and_enforce` 分级）。
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
| `execute_pending`（durable，`:2249` 闸门前） | 对 delete/edit/write/move 统一调 `resolve_file_modify_permission`；`GATED_DENIED`→`tool_permission_denied` 结果；`DEFER`→走既有 soul/tasks/enterprise 拒绝（见下）；`NOT_GATED`→放行；`_delete_autonomy_gate` 的 L3 审批移除 |
| `execute_tool`（legacy，`:6193`） | `check_and_enforce` 替换为同一助手（edit_file 已在生效版 MAP，不缺） |
| `check_tool_autonomy`（ACP，`:29649`） | **删除**（死代码：truthy bug + `args`/`user_id`/`notify` 三死参数），连带删 `tool_bridge.py` 3 处调用（`:1557`/`:1977`/`:2129`）与动态塞 MAP 键逻辑（`:1543-1556`）；ACP 写工具把关交 IDE 端 + 版本控制（§8.3）。**注意保留 `_TOOL_AUTONOMY_MAP` 生效版供 legacy `:6193` 用**（见下 bug 修复） |

**`DEFER` 分支的真实落点（雷 2，勿假设 modify_soul 审批）**：`modify_soul` 仅是 `DEFAULT_AUTONOMY_POLICY` 里的键（`agent.py:30`），**零执行点**——soul.md 的实际保护靠 delete/move 工具描述拒绝（`builtin_tool_definitions.py:188`「Cannot delete soul.md or tasks.json」/ `:205`「Cannot move soul.md…enterprise_info/」）。故 `DEFER` 分支**落到这条工具描述拒绝路径**，不新增、也不假设存在 `modify_soul` 审批流。`soul.md` 的门控后续若要硬执行，另行立项，不在本期。

**bug 修复（独立小票，先于门控落地；权威 spec = `20260905-p0-autonomy-map-shadowing-fix-plan.md`，已按数据面二次收窄）**：收敛两份 `_TOOL_AUTONOMY_MAP` 为一份——删 `:2890` 意图版死定义、保留 `:29640` 生效版 5 键为唯一 `_TOOL_AUTONOMY_MAP`、**不补齐 6 个被遮蔽工具映射**（该 6 工具中 send_feishu_message/web_search/execute_code_e2b 30 天零调用、move_file/send_file_to_agent 走 typed 路径、send_message_to_agent 走 A2A settle，补齐是惰性动作——宪法 II 禁止投机式加固）。**同时删 `check_tool_autonomy`（ACP truthy 死代码，连带删整条 ACP 自主权 stub：`_handle_autonomy_blocked`/`_AUTONOMY_STOP_THRESHOLD`/`_autonomy_counts`/`WORKSPACE_WRITE_TOOLS` 导入）+ `tool_bridge.py` 3 处调用 + 动态塞键逻辑**；删它不影响 legacy 分级（两套机制解耦），也不影响 `execute_command` 的 `_guard_acp_dangerous_command` 黑名单。**陷阱**：不能把 `:29640` 一起删——它是 legacy `:6193` 的唯一 MAP 来源。**另发现 A3**：`send_external_message` 孤儿键（16 agents 配 L1/L3、零代码读取）→ 另立配置清理小票，不在本票修。

### 3.5 埋点 / 可观测性

- 拒绝路径走既有台账：`agent_tool_executions` 已记录每次工具调用结果——**注意该表无 `error_code` 列**（2026-09-05 实核 schema），拦截证据在 `status`/`result_summary`/`result_metadata`；`tool_permission_denied` 以 result 形态进入台账 + Langfuse tool span，无需新埋点。
- 新增**零事件**（治理层拒绝不是 run 生命周期事件，是工具结果）——避免 whitelist 膨胀。
- 周报脚本只读聚合 `agent_tool_executions` 中 `tool_permission_denied` 的拦截计数（按 `status`/`result_summary` 判定，表无 error_code 列）+ actor 分布，用于验证「非维护人员被正确拦截、维护人员零误伤」。

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
P0 先 commit/stash 并行会话的工作区改动（开工前 git status 核对，勿混叠；2026-09-06 现状 = 仅 1 个 M：`docs/technical-plans/20260829-compaction-production-fix.md`，read-dedup 已提交）
 → bug 修复票：收敛两份 `_TOOL_AUTONOMY_MAP`（删 `:2890` 死定义 + 补齐 6 个被遮蔽工具映射）+ 删 ACP `check_tool_autonomy` 死代码（truthy bug；独立、低风险、先行）
 → ① Maintainer 门控（f077 迁移[含 autonomy_policy 键清理] → MaintainerService → 三处接入 → API/前端 P2）
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
| 4 | group-scoped workspace 短路 | `tool_step_service._is_group_scoped_workspace_call`（`:704`） | 组内调用不走门控 |
| 5 | ACP 路径 `check_tool_autonomy` | `agent_tools.py:29649` | truthy bug 死代码 → **本期删除**；ACP 写工具把关交 IDE 端 + 版本控制（§8.3 显性记录裸写缺口） |
| 6 | `execute_code` 沙箱 + `sync_back` 回写 | runtime 沙箱 | shell 逃逸面——门控定为「治理层」的依据 |

**新增门控（①）的定位**：接管第 1/2 层对 `delete/edit/write/move` 四类**文档工具**的判定，替换为「维护人员名单」；第 3/4/6 层**不动**，第 5 层（ACP `check_tool_autonomy`）**删除**（死代码，见 §8.3）。`check_and_enforce` 保留给非文件 action（`send_*`/`web_search`/`execute_code` 等）。

### 8.2 三个雷 + 折入处理

| 雷 | 现状（核实） | 折入处理 |
|---|---|---|
| **雷 1：3 条 pending delete_files 审批变孤儿** | 决策 B 废弃 delete 审批流后，现 3 条 pending `ApprovalRequest(action_type=delete_files)` 无人消费 → 对应 run 悬挂 | `resolve_approval` **保留 delete_files 分支不删**；f077 迁移时对存量 pending delete_files 审批**按 creator 直接 resolve**（reject + resume run 告知「门控已接管」），不新增审批流。新 delete 调用不再产生审批（走门控硬拒） |
| **雷 2：`modify_soul` 空头支票** | `modify_soul` 仅是 `DEFAULT_AUTONOMY_POLICY` 键（`agent.py:30`），**零执行点**；soul.md 实际保护 = 第 3 层工具描述拒绝 | 门控 `DEFER` 分支落到第 3 层工具描述拒绝路径（§3.4 已写），**不假设存在 modify_soul 审批**；`modify_soul` 硬执行另行立项 |
| **雷 3：两套「谁能批」判定并存** | `resolve_approval` 只认 creator+platform_admin；门控管理 API 拟用 platform_admin+org_admin（决策 D） | **文档化并存**：审批流（历史机制）仍 creator+platform_admin；门控管理 API（`GET/POST/DELETE /api/agents/{id}/maintainers`）用 platform_admin+org_admin。两者不合并——审批流是运行态裁决，维护人员管理是治理配置，归属不同权限主体，写进 API 文档 + release note |
| **雷 4：`send_external_message` 孤儿键**（2026-09-05 新发现） | `DEFAULT_AUTONOMY_POLICY`(:29) + 16 agents 政策（L1/L3）+ 测试 `ALL_ACTIONS` 均含 `send_external_message`，但**零代码读取**（无任何 `policy.get("send_external_message")`） | 与 `modify_soul` 同类「声明但零执行点」，**另立配置清理小票**：确认产品语义→若废弃则从 DEFAULT 删 + 迁移政策键 + 更新 `test_agent_default_autonomy_policy.py` 的 `ALL_ACTIONS`；不在本票混入（爆炸半径远超 bug 修复，违宪法 II） |

### 8.3 ACP 写工具裸写缺口（既存，本次显性化——grill 决策 1 的 ACP 延伸）

**事实链（2026-09-06 HEAD `49806eb2` 核实）**：
1. 写工具 schema（`acp_write_file`/`acp_edit_file`/`acp_delete_file`/`acp_refactor_rename`/`acp_move_file`/`acp_reformat_code`）**无 `requires_approval` 参数**；全 backend 仅 `acp_execute_command` 有（`tool_hooks.py:198`），且后端**不消费**它（纯模型提示，默认 false）。
2. IDE 端弹窗证据（`tool_bridge.py:798-803` `_timeout_for_acp_method`）：仅 `fs/safe_delete` 用 120s permission 超时（对齐 IDE `requestPermissions`）；`fs/write_text_file` 60s / `fs/edit_text_file` 30s 普通超时 → 写工具**无 `requestPermissions` 弹窗**。
3. `check_tool_autonomy` truthy bug（`:29679`）→ 后端永不拦截。
4. **结论**：写工具（write/edit/refactor/move/reformat/optimize/convert）在 ACP 路径 = **裸写**（无人肉确认、无分级）。

**参考对比**：标准 ACP 协议（claude-agent-acp `permission-extension.md`）里 Edit/Write 走 `session/request_permission`（客户端渲染 allow-once/reject）；Clawith 自定义 RPC `fs/write_text_file`/`fs/edit_text_file` 直写是**对协议的偏离**（简化实现）。

**决策（对齐 grill 决策 1「治理层非硬安全」）**：删 `check_tool_autonomy`（死代码），ACP 写工具把关 = IDE 端 + 版本控制兜底。**有意为之、非事故**。缓解：`execute_command` 保留 `_guard_acp_dangerous_command` 黑名单（`tool_bridge.py:1205`，`:1965`/`:2105` 调用）不受影响；写工具裸写**无独立拒绝信号**（直写不产生 `tool_permission_denied`），观察手段 = 版本控制/undo 兜底 + 遗留项的 IDE 弹窗加固，不设独立周报曲线（§5 周报只覆盖门控拒绝率）。

**遗留（不阻塞本期）**：若要写工具恢复人肉确认，方向 = IDE 插件把 `fs/write_text_file`/`fs/edit_text_file` 也接 `requestPermissions`（仿 `fs/safe_delete`），后端可先行补 `requires_approval`——对齐标准 ACP 协议，属独立加固项，另行立项。

**验收红线**：① 落地后 `approval_requests` 不再新增 delete_files 记录；3 条 pending 已 resolve；`agent_maintainers` 管理 API 鉴权含 org_admin；非维护人员 edit_file（最大量、现零门控）被 `tool_permission_denied` 拦截。

---

## 9. Phase 5 审查结论（code-review 双轴，2026-09-05 已跑）

双轴并行子代理审查实现 diff（`bf69441e` + `aa539999`）对照本方案，结论与处置如下。

### 9.1 Standards 轴

| # | 发现 | 定性 | 处置 |
|---|---|---|---|
| 1 | `execute_tool` 门控 catch 把裸异常 `{e}` 灌入模型上下文 | 硬违规（backend/AGENTS.md 模型契约） | **已修**：`logger.exception` 记日志，模型只见通用话术 |
| 2 | 门控两处 `except Exception` 兜整个 resolve（含开 session） | 判断项 | 保留（fail-closed 边界，故意兜底） |
| 3 | 模型未声明 `agent_id` 索引，f077 只在 `_exists()` 后建 `ix_agent_maintainers_agent_id` → 全新环境 `001 create_all` 建表无索引、f077 早退 → 索引永不建 | **硬违规/真 bug**（模型与迁移分歧） | **已修**：模型 `agent_id` 加 `index=True`，auto 名 `ix_agent_maintainers_agent_id` 与迁移一致，新旧环境均得索引 |
| 4 | 两 seam（execute_tool / tool_step_service）拒绝话术措辞漂移 | 判断项（模型可见措辞=行为） | 保留（execute_tool 是字符串返回给模型、tool_step_service 是 outcome.error_code，语义一致、形态本就不同） |
| 5 | `_maintainer_file_gate` 返回 `tuple[outcome, JsonObject|None]` 但第二元素恒 None | 判断项（投机通用性） | 保留（对齐 `_delete_autonomy_gate` 返回形态，调用点对称） |
| 6 | `_delete_autonomy_details` 早退后紧跟 `if is_group_delete:` 冗余 | 判断项 | 保留（冗余但自文档化块边界，扁平化收益低） |
| 7 | `_classify_path` 返回裸字符串而模块已有 `FileModifyDecision` 枚举 | 判断项（Primitive Obsession） | 保留（bucket 是路径分类内部词汇，与门控决策枚举语义不同层） |

### 9.2 Spec 轴

| # | 发现 | 处置 |
|---|---|---|
| S1 | autonomy_policy 的 delete_files/write_workspace_files 键未清理（grill 决策 6 / §3.2 原写「f077 显式迁移」） | **故意移出迁移**（backend/alembic/AGENTS.md §2 禁数据操作）→ 转 out-of-band 脚本，**待办** |
| S2 | 3 条 pending delete_files 审批未 resolve（雷 1 / §8.2） | ✅ **已 resolve**（`20260907-resolve-orphaned-delete-approvals-fix-plan.md`，脚本 `resolve_orphaned_delete_approvals.py`，commit `5ea06022`；2026-09-07 `--apply` 生产对账：3 条全 rejected、0 pending） |
| S3 | `agent_maintainers` 管理 API（GET/POST/DELETE `/api/agents/{id}/maintainers`，鉴权含 org_admin）未实现（雷 3 / 验收红线） | ✅ **已实现**（`20260907-agent-maintainers-management-api-production-plan.md`，commit 见该方案 Phase 5）——`is_maintainer` 分支可达、名单可写、鉴权含 org_admin |
| S4 | 前缀判定用 `normalize_workspace_path`（非 symlink 感知）替换了 §2/§3.3/§3.6 要求的 `safe_agent_path` | **拍板偏离**（grill 决策 1 / G-4：治理层非硬安全，symlink 需先经 `execute_code` 种入=已接受绕过面），代码 docstring 已记录；本表（S4）即为 spec↔代码同步记录 |
| S5 | legacy `execute_tool` 门控只传 `actor_user_id`、缺 `actor_agent_id` → a2a 规则在 legacy/ACP 链式路径不生效 | durable runtime 路径（`_maintainer_file_gate`）已正确传 `actor_agent_id`；legacy seam 无该参数，且 a2a 文件写走 durable runtime，**记待办**（若 legacy seam 复用于 a2a 再补参） |
| S6 | 前端「维护人员」tab 未实现（名单管理 UI） | **待办（P2）**。**阻塞性前置依赖**：依赖 S3 管理 API 的 admin 角色门控；**禁复用 `canManage`**（`canManage` ⊃ admin 含 creator，会与 admin-only 后端错位→creator 看到 tab 却 GET 403）——tab 必须按 **admin 角色**（platform_admin/org_admin）门控。S3 已交付，本票可开做 |

核对无误：creator 隐式维护者 ✓、actor-None/heartbeat 回退 creator ✓、group-scoped delete 保留 L3 ✓、A3 正确延期（零实现）✓。

### 9.3 验收红线对照（诚实口径）

| 红线子项 | 状态 |
|---|---|
| 落地后 approval_requests 不再新增 delete_files | ✅ 已达成（非 group delete 短路 + 门控接管） |
| 3 条 pending 已 resolve | ✅ 已达成（`20260907-resolve-orphaned-delete-approvals-fix-plan.md` §5.1，commit `5ea06022`） |
| agent_maintainers 管理 API 鉴权含 org_admin | ✅ 已达成（`20260907-agent-maintainers-management-api-production-plan.md`，S3） |
| 非维护人员 edit_file 被 tool_permission_denied 拦截 | ✅ 已达成（`_maintainer_file_gate` GATED_DENIED → error_code） |
