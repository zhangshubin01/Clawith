# Phase 4 七角度评审裁决：Maintainer 门控 + 2 个真 bug 修复

日期：2026-09-05
对象：`20260905-maintainer-gate-g3-g4-production-plan.md`（四件套之①②③已齐，本文补第④件）
基线：HEAD `18d489aa` 实读重核（并行会话已推进 waiting 终态化 18d489aa / PG 僵尸连接 8f01ac8f / f076 迁移 49806eb2 等）

## 裁决（结论前置）

**有条件通过**——7 问全部通过，无阻断项；3 项已知风险（均有缓解、已折入方案 §7/§8）：
1. **误伤** agent 自主删自己临时文件（grill 决策 4：硬拒 + 周报观察，memory/ 放行 + creator 兜底）。
2. **雷 2 遗留**：`modify_soul` 硬执行未做（DEFER 落到工具描述拒绝路径，另行立项）。
3. **ACP 写工具裸写**是既存缺口（§8.3 有意为之，靠 IDE + 版本控制兜底，不在本期修）。

开工前置：§7 的「`context.actor_user_id` 填充」逐入口审计须先完成；实现后必须跑 Phase 5 `code-review` 对照本方案复核 diff，否则不算闭环。

---

## 0. 基线刷新（HEAD 18d489aa 重核，以函数名定位、行号仅作约）

| 代码级事实 | 方案旧标注 | 重核后（18d489aa） |
|---|---|---|
| HEAD / alembic head / 门控迁移编号 | 8b2ac1d4(过时) / f076 / f077 | 18d489aa / f076 / **f077 ✓**（f076=no_progress_enum 已占用） |
| `_TOOL_AUTONOMY_MAP` 意图版（9 键） | :2890 | :2890 ✓（write/move/delete/send_feishu/send_message_to_agent/send_file_to_agent/web_search/execute_code/execute_code_e2b） |
| `_TOOL_AUTONOMY_MAP` 生效版（5 键） | :29640 | :29640 ✓（write/edit/delete/execute_code/execute_command） |
| `check_tool_autonomy` truthy bug | :29679 `allowed=policy.get(category, True)` | :29679 ✓（`args`/`user_id`/`notify` 三死参数） |
| `execute_tool` autonomy 检查（legacy seam） | :6193 | :6193 ✓ |
| `execute_pending`（durable seam） | :2005 | :2005 ✓ |
| `_delete_autonomy_gate` 定义 / 调用 | :1913 / :2249 | :1913 / :2249 ✓ |
| `_is_group_scoped_workspace_call` | :704 | :704 ✓（三条件：`_is_group_agent_run && tool∈SCOPED_WORKSPACE_TOOL_NAMES && workspace_scope=="group"`） |
| `_PATH_CONVENTION_PARAMS` | :4103 | :4103 ✓（write_file→path / delete_file→path / move_file→(source_path,destination_path) / edit_file→path） |
| `safe_agent_path` / `normalize_workspace_path` | :107 / :92 | :107 / :92 ✓（`.resolve()` + `startswith`） |
| `DEFAULT_AUTONOMY_POLICY` / `modify_soul` | :24 / :30 | :24 / :30 ✓（`modify_soul` 仅键存在、零执行点） |
| `resolve_approval` 谁能批 | autonomy_service:168 | autonomy_service:168 ✓（:185 `creator_id != user.id and user.role != "platform_admin"`） |
| `check_tool_autonomy` 3 处调用 / 动态塞键 | 1557/1977/2129 / 1543-1556 | ✓ 全确认（写工具 :1557、execute_command 阻塞 :1977、terminal-streaming :2129） |
| `execute_builtin_tool_outcome` | :5612 | **:5619**（漂移 +7，函数名不变） |
| `node_executor.py:212 execute_pending` | 未提 | **新发现：`RuntimeToolStepService` Protocol 接口声明，非执行点** |

### 两处基线修正（均不改变方案 seam 的正确性）

1. **`execute_builtin_tool_outcome` 不是独立执行点**，而是 durable 路径的**下游 executor**（`tool_step_service.py:1007` 作为默认 `tool_executor` 注入）。它"无自主检查"是设计使然——闸门在上游 `execute_pending:2249`。方案 §2 把它列为独立执行点略有误导，但门控 seam（:2249）仍准确。
2. **`node_executor.py:212` 是 Protocol 声明**，不是执行路径——**无新增第三路径**，方案「三处接入」仍准确：durable :2249 / legacy :6193 / ACP 删死代码。

### 本轮新证实的两条耦合（Q4/Q5 直接依据）

- **动态塞键污染 legacy**：`tool_bridge.py:1543-1556` mutate 的是 `agent_tools` 模块级 `_TOOL_AUTONOMY_MAP` **同一 dict 对象**（非拷贝），会把 `refactor_rename`/`reformat_code`/`optimize_imports`/`convert_java_to_kotlin`/`safe_delete` 等 ACP 专用键写进 legacy `:6193` 也能读到的 dict。但 legacy 工具名集合里这些键**永不出现**（legacy 只接 builtin 工具名），故 inert；删掉塞键后 legacy 更干净、零副作用——**佐证方案 §3.4「删动态塞键」是对的**。
- **legacy `execute_tool` 仍是活路径**：被 ACP `tool_hooks.py:66`（`_chained_execute_tool`）、`:940`（`_acp_aware_execute_tool`）以及 `execute_builtin_tool_outcome:6067`（untyped/dynamic 回退）三处调用。故 :6193 **必须**接门控，「只 gate durable 路径」的更简方案不成立（见 Q5）。

---

## Q1 根因找得是否正确？

**裁决**：通过。

**正向依据**（源码为主、双源已闭环）：
- **bug 1（MAP 遮蔽）**：模块内同名 `_TOOL_AUTONOMY_MAP` 定义两次（:2890 意图版 9 键 / :29640 生效版 5 键），运行时名字解析到末次赋值 :29640 → 6 工具（move_file / send_feishu_message / send_message_to_agent / send_file_to_agent / web_search / execute_code_e2b）在 legacy `:6193` 拿到 `action_type=None`，不进 `check_and_enforce` → 这些工具的 L2/L3 分级静默失效。证据：:29640 实读仅 5 键。
- **bug 2（ACP truthy）**：`check_tool_autonomy:29679` `allowed = policy.get(category, True)`，policy 值是字符串 "L1"/"L2"/"L3" 恒真 → ACP 永不拦截。证据：:29679 实读 + 三调用点 :1557/1977/2129 均仅用 `_INLINE_IMPORTED.get("check_tool_autonomy")`。
- **门控缺口（需求根因）**：46 chat + 13 a2a 非 creator 驱动 run 可无门控改文档，edit_file 1281 次零门控（approval_requests 仅 delete_files 14+3，write/edit/move 零记录 = 铁证现零门控）。证据：§0 接地数据（PG 台账 30 天聚合）。

**负向探针（反例测试 + 对立假设）**：
- 反例：「若根因是 MAP 遮蔽，则 6 工具的 `_TOOL_AUTONOMY_MAP.get(tool_name)` 应返回 None、不进 check_and_enforce」——我核了 :29640 末次赋值只含 5 键、确实不含 execute_code_e2b/send_*，**证实**。
- 对立假设：「这 6 工具的自主动权可能在别处 enforce」——我 grep 了 `check_and_enforce` 全部调用点，仅 legacy:6202（MAP 驱动）与 delete gate:1937（硬编码 `delete_files`）两处，无 send_*/web_search/execute_code_e2b 的独立 enforce 点 → 对立假设**不成立**，分级静默失效为真。
- 反例：「若 bug 2 是 truthy bug，则 policy=execute_code=L3 时 `check_tool_autonomy("execute_command",…)` 应返回 None（放行）」——核 :29679 `allowed="L3"`（truthy）→ `if allowed: return None` → **证实**永不拦截。

三条根因均解释双源里全部关键证据、且追到了最深一层（MAP 遮蔽是「两份定义」的表象，更深一层是「运行时取末次赋值」的 Python 语义；门控缺口的深层是「autonomy_policy 只分级 creator 设定的 agent 自主度、与『谁驱动 run』无关」）。

---

## Q2 根治方案是否正确？

**裁决**：通过。

**正向依据**：方案改的是 Q1 定出的根因本体：
- 收敛两份 MAP 为一份 + 补齐 6 工具映射 → 直接修「末次赋值遮蔽」。
- `resolve_file_modify_permission` 按 actor 判定 → 直接修「autonomy_policy 与驱动者无关」。
- 删 `check_tool_autonomy` → 移除 truthy 死代码（非止痛药）。

**负向探针（删除测试，逐条）**：
- 「删掉『收敛 MAP + 补齐 6 映射』→ send_*/web_search 的 L2/L3 分级会否复失效？」**会**（末次赋值仍是 5 键）→ 是根治。
- 「删掉 `resolve_file_modify_permission` → 非 creator 驱动能否回到无门控改文档？」**会**（现状 46+13 run 即如此）→ 是根治。
- 「删掉『删 check_tool_autonomy』→ ACP 裸写会否复现？」**不会消失**（truthy bug 本就不拦）——此项须诚实定性：删死代码**不修 ACP 裸写**，裸写是 §8.3 既存缺口、有意为之（治理层边界），靠 IDE + 版本控制兜底。方案已如实标注，未把「删死代码」冒充「修裸写」，无措辞欺骗。

---

## Q3 参考的资料是否正确？

**裁决**：通过（一条低风险注记）。

**正向依据**：
- 引用均为**同类问题**（非同名 false friend）：letta-code `memory-confinement`（谁可写什么）、deepagents middleware（门控判定函数形态）、claude-agent-acp `permission-extension.md`（ACP 权限模型）、bisheng OpenFGA（多租户授权）、jcode（工具风险分类）、12-factor-agents（ownership boundary）。
- 已读真实源码而非 README 摘要：ACP `permission-extension.md` 与 Clawith 自定义 RPC `fs/write_text_file` 直写、`fs/safe_delete` 走 `requestPermissions` 的对照是读协议源码得出（§8.3 事实链 4 条）。
- 已归档/停更项目（letta-code 已归档）**未当第一依据**——只借「前缀+名单」思路、明示偏离（fail-closed 无意义，因 `execute_code` 经 `sync_back` 可绕过）。
- **诚实负结论**已在 §1 末尾明示：多租户「非 creator 驱动改文件的权限主体语义」参考清单里无逐字可抄项，按平台自身事实设计。

**负向探针（找引用错点）**：
- 「letta-code 已归档，其 memory-confinement 在归档前是否改名/迁移，导致 §1 的描述失真？」核下来：方案只引用其**思路启发**（前缀+名单）而非机制依赖，即使版本有变也不影响本方案的判定结构 → **低风险，可接受**，但建议实现时不再回引其具体实现细节。
- 「claude-agent-acp `permission-extension.md` 是否官方协议文档、非社区 fork？」已读真实源码确认其 Edit/Write 走 `session/request_permission` 语义 → **无误**。
- 「bisheng OpenFGA 16 类型是否比 maintainer 名单更合适？」对立假设测试：OpenFGA 引入 16 类型授权模型 + 关系 tuple 存储 + 迁移，远超「门控 = 治理配置」的最小需求，且决策 D 已定 maintainer 名单 → **不成立**，偏离理由成立。

---

## Q4 副作用与爆炸半径是否排查完？

**裁决**：通过。

**正向依据**（两个子检查都答）：

① **副作用面**：
- exactly-once 外部写：门控是**执行前读 DB 的纯函数**（`resolve_file_modify_permission`）+ 一次 `is_maintainer` 查询，不引入新外部写、不改执行路径、不重试。
- 缓存失效：无缓存参与。
- 连接/资源：判定复用既有 `db` 会话（durable 路径已有 db），无新连接池、无长事务；`agent_maintainers` 表仅在管理 API 增删，非运行态热路径。
- 权限边界：`DEFER` 落到既有工具描述拒绝路径（`builtin_tool_definitions.py:188/205`），不新增审批流。

② **影响面**（改了契约/状态的所有消费者过一遍）：
- durable `execute_pending:2249`：`_delete_autonomy_gate` 的 L3 审批移除 → 替换为统一 `resolve_file_modify_permission`。
- legacy `execute_tool:6193`：`check_and_enforce` 替换为同一助手。
- ACP `check_tool_autonomy` 删除 + 3 调用点 + 动态塞键删除。
- `resolve_approval` 保留 delete 分支（雷 1）不删。
- 回归：`backend` 全量 pytest（`-p no:cacheprovider`，排除 `test_sso_toggle.py`）+ `scripts/arch-guard.sh`。

**负向探针（点名具体消费者/副作用）**：
- 「漏掉的消费者 1：`_execute_approved_action`（autonomy_service:309-390）审批通过后调 `_execute_tool_direct` **绕过一切门控**执行 delete_files」——核下来：雷 1 已定 3 条 pending delete 按 **reject** resolve + 门控接管后不再产生新 delete 审批 → 该 bypass 对文件工具**休眠**；其 group delete 分支（:337-377）仍活，但 group 路径 `NOT_GATED` 本就不受门控 → **不冲突，不影响**。
- 「漏掉的消费者 2：`execute_builtin_tool_outcome:6067` 对 untyped/dynamic 工具回退 `execute_tool` → 若某文件工具未被 typed 迁移，durable 路径也会经 :6193」——核下来：write/edit/move/delete 均为 typed 迁移（:5664-5678 已列），不走回退；即便有动态文件工具回退，也会被 :6193 门控接住 → **双 seam 兜底，且每工具只走一条路径，无双重拒绝**。
- 「漏掉的副作用 3：删动态塞键是否影响 legacy 分级恢复？」——核下来：塞键污染的是 legacy 也能读到的同一 dict，删掉后 legacy 拿到的是「补齐后的完整映射」，行为更正确 → **无副作用**（见 §0 耦合 1）。

---

## Q5 这是最优且必要的方案吗？

**裁决**：通过。

**正向依据**（两个子检查都答）：

① **候选枚举（≥3，从 Ponytail 阶梯最低档起挑）**：
- A（更简单）：只修 bug、不建 maintainer 表，靠 autonomy_policy 分级兜底。
- B（当前）：三处接入统一助手 + 删死代码 + 修 bug（§3.4）。
- C（更彻底）：OpenFGA 16 类型细粒度授权（bisheng），或 fail-closed 全部文件工具 + execute_code 沙箱强隔离。

② **修已发生 vs 臆想**：edit_file 1281 次零门控 + 46+13 非 creator run = **已发生的真故障**（有真实受众）；G3 挂起、G4 只做周报已**诚实定性为预防**（grill 决策 7/8），未当 P0 已损。

**负向探针（更简单一档能否解决 + 已发生判定）**：
- 「试 A：只靠 autonomy_policy 分级、不按 actor 判定，能否拦住非 creator 改文档？」**不能**——autonomy_policy 是 creator 设定的 agent 自主度，与「谁驱动 run」正交：非 creator 驱动时 L1 照样写。故必须引入按 actor 的 maintainer 名单 → A 不成立。
- 「试 C：OpenFGA 是否更优？」**过度**——16 类型 + 关系 tuple + 迁移成本远超「治理配置」需求，且决策 D 已定名单 → C 不必要。
- 「试『只 gate durable :2249、不碰 legacy』一档」——核 legacy `execute_tool` 仍是活路径（ACP tool_hooks:66/940 调用）→ 非 creator 驱动的 ACP 写会漏 → **不成立**，三处接入是必要的。

---

## Q6 是否已有可复用的逻辑？

**裁决**：通过。

**正向依据**：判定助手**全部复用既有**，不重复造：
- `_PATH_CONVENTION_PARAMS`（:4103）取四类工具路径参数 → 不造第三份清单。
- `safe_agent_path`（:107，`.resolve()` + `startswith`）做 symlink 感知的前缀判定 → 防逃逸已有层。
- `_is_group_scoped_workspace_call`（:704）做 group 短路。
- 既有台账 `agent_tool_executions` 记 `tool_permission_denied` error_code → 零新埋点（§3.5）。
- `_TOOL_AUTONOMY_MAP` 生效版保留给 legacy :6193。

**负向探针**：
- 「知识图谱/代码里是否已有『按 actor 判定文件写权限』的等价逻辑？」我查了：只有 `_is_group_scoped_workspace_call`（group 短路）与 `resolve_approval`（creator+platform_admin 判定），**无 maintainer 名单判定** → 需新增 `MaintainerService`，但**服务内部零新逻辑**，全是指向既有 path 工具的编排。

---

## Q7 会破坏 Clawith 的特性吗？

**裁决**：通过。

**正向依据**（宪法 C1–C6 + 红线逐条）：
- C1 证据先行：§0 接地数据（PG 台账实测）。
- C2 最小改动：不新建 run kind/服务（`MaintainerService` 是纯函数模块，非新执行服务）；不混结构重构。
- C3 契约与状态所有权：`agent_maintainers` 归属 agent 治理配置；creator 不落表、运行时隐式判定（`actor_user_id or agent.creator_id`）零回填。
- C4 测试证行为：§3.6 五类测试（纯函数 + durable/legacy/ACP 三路径 + 迁移 downgrade）。
- C5 保留既有工作：雷 1 保留 delete 审批分支、迁移按 creator resolve；沿 f072 规约。
- C6 模块化与数据边界：`agent_maintainers` 无 tenant_id、靠 agent_id 隐式隔离，与 agents 表同模式。

红线：durable run/checkpoint（门控在执行前判定、不写 checkpoint、不改 run 状态机）✓；多租户隔离（无 tenant_id 跨租户泄漏面）✓；exactly-once（不改执行路径）✓；前缀缓存（不碰 tool 缓存键）✓；WS 状态机 ✓；飞书通道（删除 delete 审批后 L3 delete 不再发 approval card，是决策 B 拍板的有意移除，send_*/web_search 的 L2/L3 通知仍走保留的 `check_and_enforce`）✓。

**负向探针（把方案对红线逐一过，找是否碰到）**：
- 「checkpoint 语义 / 前缀缓存前缀」：门控判定是执行前读 DB 的纯函数，不写 checkpoint、不碰 tool 缓存键 → **不碰**。
- 「飞书审批卡」：删除 delete 审批后，L3 delete 不再发 Feishu approval card——这是决策 B（废弃 approval 流）的**预期行为变更**，非破坏，已要求 release note 明示。
- 「多租户隔离」：`agent_maintainers` 靠 `UNIQUE(agent_id,user_id)` + agent_id FK 隐式隔离，无 tenant_id 字段 → 与既有 `agents` 表同模式，**不破坏** C6。

---

## 结论

- **评审通过（有条件）**：7 问全过，无阻断项；方案可进入实现（Phase 5）。
- **开工前置（不满足则回改）**：§7 的 `context.actor_user_id` 填充逐入口审计（direct/trigger/heartbeat/group/channel）必须先做——它决定「actor 空值回退 creator」能否真兜住后台 run。
- **已知风险（3 项，均带缓解）**：① 误伤自产临时文件（周报观察 + 回退边界）；② `modify_soul` 硬执行未做（另行立项）；③ ACP 写工具裸写既存（IDE + 版本控制兜底）。
- **实现后强制**：跑 `code-review` 对照本方案复核 diff——diff 偏离方案须回改或重审，不得带病交付（Phase 5 铁律）。
