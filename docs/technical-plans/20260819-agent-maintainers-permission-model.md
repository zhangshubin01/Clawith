# Agent 维护人员（Maintainer）权限模型设计

日期：2026-08-19
状态：设计草案（待确认后进入实施）

## 1. 背景与问题

现状：`agents.autonomy_policy` 按 action 分 L1/L2/L3（自动执行 / 自动执行+通知 / 需审批），其中
`delete_files` 与 `write_workspace_files` 的 L3 审批写死为「只有 agent 的 creator 或 platform_admin
能审批」（见 `autonomy_service.resolve_approval`）。这带来三个问题：

1. agent 删除**自己生成的临时文件**也要审批，主任务被卡死（本次事故：Android 工程师 4 删 `jar_check.txt` 等 3 个排障残留被 L3 拦截）。
2. 审批人只能是 creator，无法把「删除/改文档」的权限授给团队其他成员。
3. 审批文案误导（"Workspace deletion"），已随本次修复改为「File deletion: {path}」。

## 2. 目标

用「维护人员列表」取代 delete/modify 的 L1/L2/L3 分级：**删除文件 / 修改文档的权限只由维护人员名单决定**。
维护人员既是审批人，也是直接操作者。

### 定位：治理层（非硬安全边界）

本门控是「防误改」的治理层——约束 agent 在**非维护人员**驱动下自动删/改文档（`workspace/`、`skills/`）。
它**不是**硬安全边界：agent 手里有 shell（`execute_code` 经 `sync_back` 全量回写真实 workspace），
被诱导后仍可用 shell 直接改文件。因此 **`execute_code` 明确不在门控范围**，本方案只治理「文档工具」
（`delete_file`/`edit_file`/`write_file`/`move_file`）这一条路径。

## 3. 核心决策（已确认）

| 决策点 | 结论 |
|---|---|
| 维护人员语义 | 两者都要：审批人 + 直接操作者 |
| 与 L1/L2/L3 关系 | 取代——delete/modify 只看名单，不再看分级 |
| 谁增删维护人员 | 管理员 |
| 非维护人员体验 | agent 直接拒绝并告知，不发起审批 |

## 4. 数据模型

新表 `agent_maintainers`：

| 列 | 类型 | 说明 |
|---|---|---|
| id | uuid PK | |
| agent_id | uuid NOT NULL | FK → agents |
| user_id | uuid NOT NULL | FK → users |
| created_by | uuid | 添加人 |
| created_at | timestamptz NOT NULL DEFAULT now() | |
| updated_at | timestamptz NOT NULL DEFAULT now() | |

约束：`UNIQUE (agent_id, user_id)`。

**待确认 A**：creator 是否始终隐式为维护人员（建议：是，且不可被移出，避免 creator 丧失对自己 agent 的控制）。

## 5. 权限判定逻辑

对「删除文件 / 修改文档」工具：`delete_file`、`edit_file`、`write_file`、`move_file`（含 write_file 的 append 模式）：

1. 取「当前操作者」= `context.actor_user_id`（缺省回退 `agent.creator_id`）。
2. 判定该用户是否为该 agent 的维护人员（含 creator，若采纳待确认 A）。
3. **是** → 放行，直接执行（不再走 `check_and_enforce` 的 L3 审批）。
4. **否** → 拒绝 + 告知：`error_code=tool_permission_denied`，文案告知「仅维护人员可删除文件/修改文档」。

**取代**：`_delete_autonomy_gate` 对 delete/modify 的 `check_and_enforce("delete_files"/"write_workspace_files")` 调用移除；
`autonomy_policy` 里 `delete_files` / `write_workspace_files` 键不再参与这些操作的判定（保留在 JSON 里但忽略，或显式迁移）。

### 5.1 路径边界（关键）

维护人员门控作用于 `workspace/`（文档）与 `skills/`（技能）。**只有 `memory/` 是 agent 自动更新的例外**：

| 路径 | 是否门控 | 说明 |
|---|---|---|
| `workspace/` | ✅ 门控 | 用户/业务文档，删除/修改需维护人员 |
| `skills/` | ✅ 门控 | 非维护人员不得更改 |
| `memory/`（memory.md、curiosity_journal.md、reflections.md、user_profile.md） | ❌ 不门控 | agent 自身记忆，自动读写，永远放行 |
| `soul.md` | ❌ 不门控（走 `modify_soul` 既有机制） | agent 自身指令 |
| `enterprise_info/` | 另行考虑 | 租户级共享信息 |

判定实现：先归一化 `path`，仅当目标落在 `workspace/` 或 `skills/` 前缀内时才做维护人员判定；`memory/`、`soul.md` 直接放行。

**已确认 B**：非维护人员「直接拒绝」不再产生 approval_request，delete/modify 的 `approval_request` 审批流**废弃**，只做名单判定。
`approval_request` 机制保留给其他 action_type（`send_external_message`、`modify_soul`、`access_business_system_write` 等）。

## 6. 修改文档范围（已确认 C）

`edit_file` + `write_file` + `move_file`，作用于 `workspace/` 与 `skills/`。
**不含** `memory/`（agent 自动更新）、`modify_soul`（agent 自身指令，保留既有机制）与 `access_business_system_write`（业务系统）。

## 7. 管理员定义（待确认 D）

「管理员」= `platform_admin`？还是 `org_admin`（租户管理员）也可？建议：两者均可，但增删维护人员的
操作限定在「管理员」与「creator」之间需你确认。

## 8. API 变更

- `GET  /api/agents/{id}/maintainers` — 列出维护人员
- `POST /api/agents/{id}/maintainers` — 添加（管理员）
- `DELETE /api/agents/{id}/maintainers/{user_id}` — 移除（管理员）
- 鉴权：仅管理员（待确认 D）

## 9. 前端变更

- Agent 详情页新增一个 **`maintainers`（维护人员）tab**，紧邻现有 `approvals`（审批）tab 之后（两者同属「治理/权限」类）。
- 现有 tab 列表在 `frontend/src/pages/agent-detail/agentDetailTabs.ts`：`status/aware/mind/tools/skills/relationships/workspace/chat/activityLog/approvals/settings`，新增 `maintainers`。
- 面板内容：维护人员列表 + 添加/移除（用户搜索选择），仅管理员可见（复用现有 `canManage` 权限旗标）。

## 10. 迁移

- `agent_maintainers` 表：alembic 迁移 + inspector 存在性守卫（遵循 `backend/alembic/AGENTS.md` 70-78 规范）。
- 存量数据：把每个 agent 的 creator 写入 `agent_maintainers`（若采纳待确认 A，则运行时隐式判定即可，无需回填）。

## 11. 实施步骤（TDD + code-review）

1. 数据模型 + alembic 迁移（`agent_maintainers`）。
2. `MaintainerService`：查询/添加/移除 + 「是否维护人员」判定（含 creator 隐式）。
3. 权限判定替换：`tool_step_service` 里 delete/modify 分支改走 maintainer check，移除 L3 审批。
4. API 端点 + 鉴权。
5. 前端维护人员面板。
6. 回归测试（TDD）+ 双轴 code-review。

## 12. 待确认项汇总

- **A**：creator 是否始终是维护人员（建议：是，不可移除）。
- **D**：管理员 = platform_admin only，还是含 org_admin（建议：两者均可）。
- ~~B~~：已确认——非维护人员「直接拒绝」，delete/modify 的 approval_request 审批流废弃，只做名单判定。
- ~~C~~：已确认——`workspace/` + `skills/`（`memory/`、`soul.md` 排除）。
