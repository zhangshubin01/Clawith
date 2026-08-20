# Agent 维护人员（Maintainer）—— 代码级生产实施计划

日期：2026-08-19（已按三轴评审修订）
状态：待实施（设计见 `20260819-agent-maintainers-permission-model.md`）

> 本文只描述「改什么、怎么改、怎么验证、怎么上线」，不含代码落地（实施时逐条 TDD）。

## 0. 已确认决策（速览）

- **定位：治理层**（防误改），非硬安全边界——`execute_code` 明确不在门控范围。
- 维护人员 = 审批人 + 直接操作者。
- **取代** `autonomy_policy` 对 delete/modify 的 L1/L2/L3 分级：删除/修改文档只看维护人员名单。
- 门控路径：`workspace/` 与 `skills/`；`memory/` 永远放行；`soul.md`/`enterprise_info/` 显式走既有机制（见 §3）。
- 非维护人员（即使通过 agent）→ 直接拒绝 + 告知，不产生 approval_request。
- **creator 默认是维护人员，不可移除**。
- 管理员（`platform_admin` + `org_admin`）可增删维护人员。
- 后台 run（trigger/scheduled）`actor_user_id` 缺省回退 `creator_id`（治理层默认，见 §9）。
- 组工作区（`workspace_scope=group`）不纳入，短路放行。

---

## 1. 改动文件清单

| 层 | 文件 | 动作 |
|---|---|---|
| 模型 | `backend/app/models/agent.py` | 新增 `AgentMaintainer` 模型 + `Agent.maintainers` relationship |
| 迁移 | `backend/alembic/versions/v1_0_0_f069_agent_maintainers.py` | 建 `agent_maintainers` 表 |
| 服务 | `backend/app/services/maintainer_service.py`（新） | `MaintainerService` + 门控判定助手 |
| 运行时门控 | `backend/app/services/agent_runtime/tool_step_service.py` | `_delete_autonomy_gate` 收窄为 delete 薄壳，write/edit 在 `execute_pending` 统一走助手 |
| 遗留门控 | `backend/app/services/agent_tools.py` | `execute_tool`（~3448）与 `check_tool_autonomy`（~24919）接入同一助手 |
| API | `backend/app/api/agents.py` | `GET/POST/DELETE /agents/{agent_id}/maintainers` |
| 前端 | `frontend/src/pages/agent-detail/agentDetailTabs.ts` | 注册 `maintainers` tab |
| 前端 | `frontend/src/pages/agent-detail/tabs/MaintainersTab.tsx`（新） | 维护人员面板 |
| 前端 | `frontend/src/services/` | 维护人员 CRUD 封装 |

> 勘误：遗留门控在 **`execute_tool`**（`agent_tools.py:3448`，`check_and_enforce` 在 3491），**不是** `execute_builtin_tool_outcome`（3000 处无 autonomy 检查）。

---

## 2. 数据模型 + 迁移

### 2.1 模型（`backend/app/models/agent.py`）

```python
class AgentMaintainer(Base):
    __tablename__ = "agent_maintainers"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    agent_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("agents.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (UniqueConstraint("agent_id", "user_id", name="uq_agent_maintainers_agent_user"),)
```

- creator **不落表**，运行时隐式判定（`user_id == agent.creator_id`），无需存量回填。
- **C5 张力（明示）**：constitution 主张「No Physical Foreign Keys」，但全仓现状普遍用 FK（`agent.py` 的 `creator_id`/`tenant_id` 等）。本表沿用 FK 先例；若坚持无物理 FK，则 `agent_id/user_id` 改为普通 UUID 列 + 应用层校验。**实施时二选一并写进 commit message**。
- 无 `tenant_id` 列：靠 `agent_id` 全局唯一隐式租户隔离；`add_maintainer` 必须校验 `user_id` 属于 agent 同 tenant（C2 防线）。

### 2.2 迁移 `v1_0_0_f069_agent_maintainers.py`

- revision `f069_agent_maintainers`，`down_revision = "f068_conversation_id_uuid"`。
- `op.create_table` / `op.drop_table` 均带 inspector 存在性守卫（`001_initial_schema` 从模型 `create_all`，新库表已存在）。
- `upgrade()`/`downgrade()` 均 DDL-only、对称。
- 校验 `uv run alembic heads` 恰好一个 head。

---

## 3. `MaintainerService` + 门控助手

### 3.1 `MaintainerService`（`backend/app/services/maintainer_service.py`）

```python
async def is_maintainer(db, agent_id, user_id) -> bool      # creator 隐式 OR 表内行
async def list_maintainers(db, agent_id) -> list[dict]      # creator 置顶、is_creator=True
async def add_maintainer(db, agent_id, user_id, added_by)   # 幂等 + 同 tenant 校验
async def remove_maintainer(db, agent_id, user_id) -> bool  # 拒移 creator；不存在幂等 False
```

### 3.2 门控助手（单一事实来源）

```python
class FileModifyDecision(Enum):        # 不用裸字符串，三处调用点共用
    GATED_ALLOWED = "gated_allowed"    # 在门控路径且 actor 是维护人员
    GATED_DENIED  = "gated_denied"     # 在门控路径且 actor 非维护人员
    NOT_GATED     = "not_gated"        # 非门控工具 / 非门控路径（memory、其它）
    DEFER         = "defer"            # soul.md / enterprise_info/ → 走既有机制

async def resolve_file_modify_permission(
    db, *, tool_name: str, arguments: Mapping,
    agent: Agent, actor_user_id: uuid.UUID | None,
    workspace_scope: str = "agent",
) -> FileModifyDecision:
    # 1. workspace_scope == "group" → NOT_GATED（组路径短路，不纳入）
    # 2. tool_name not in {"delete_file","edit_file","write_file","move_file"} → NOT_GATED
    # 3. 提取受门控路径（多路径工具逐个判）：
    #      delete/edit/write:  arguments["path"]
    #      move_file:          arguments["source"] + arguments["destination"]  ← 双路径都判
    #    任一目标落在 gated 前缀 → 继续；否则 NOT_GATED
    # 4. 路径分类（归一化后）：
    #      workspace/ 或 skills/ 前缀 → 门控判定
    #      memory/ 前缀 → NOT_GATED
    #      soul.md、enterprise_info/ → DEFER（不静默放行，走 modify_soul / 既有机制）
    # 5. actor_user_id 为空 → 回退 agent.creator_id
    # 6. is_maintainer(actor) → GATED_ALLOWED；否则 GATED_DENIED
```

关键点：
- **多路径工具**：`move_file` 的 `source` + `destination` **都要判**（只判源会被 `move(memory/x → workspace/secret)` 覆写绕过）。
- **归一化**：复用 `normalize_workspace_path`，并补「resolve symlink」「拒绝对称 `..` 逃逸」「裸目录 `workspace`/`skills` 也命中」（前缀判 `==` 或 `startswith` 均处理目录本身）。
- **不静默放行 soul.md / enterprise_info/**：单独返回 `DEFER`，交由既有 `modify_soul` / `enterprise_info` 机制，避免 `write_file` 直改 soul.md 绕过。

---

## 4. 三处门控接入

| 调用点 | 现状 | 改为 |
|---|---|---|
| `tool_step_service._delete_autonomy_gate`（durable runtime） | 只判 `delete_file` 的 L3 | 收缩为 delete 薄壳；delete 的 L3 审批移除，改走助手 |
| `tool_step_service.execute_pending` | write/edit/move 无门控 | 在工具执行前，对 delete/edit/write/move 统一调 `resolve_file_modify_permission`；`GATED_DENIED` → `tool_permission_denied` 结果；`DEFER` → 走既有 modify_soul/enterprise 逻辑 |
| `agent_tools.execute_tool`（~3448，遗留） | `check_and_enforce`（`_TOOL_AUTONOMY_MAP` 漏 `edit_file`） | 同一助手；补 `edit_file`；`GATED_DENIED` → 返回「仅维护人员可…」文案 |
| `agent_tools.check_tool_autonomy`（~24919，ACP） | `policy.get(category, True)` 把 `"L3"` 当真值**直接放行**（既有 bug） | 同一助手；**修掉 truthy bug**（`allowed = policy.get(category) == "L1"`，或直接走助手）；`move_file` 一并纳入 |

**必须一并处理（否则遗留第三份清单）**：现有 `_TOOL_AUTONOMY_MAP` 已有**两份且互相矛盾**（`agent_tools.py:1591` 与 `24910`，一份漏 `edit_file` 一份漏 `move_file`）。实施时：
- 收敛为**一份**工具清单（单一事实来源），`resolve_file_modify_permission` 与之复用，不再新增第三份。
- `check_tool_autonomy` 的 truthy bug 单独修（它是 ACP 路径「等于无闸门」的根因）。

**废弃**：`autonomy_policy` 的 `delete_files`/`write_workspace_files` 键对 delete/modify 不再读取（JSON 保留）。`approval_request` 流仍服务 `send_external_message`/`modify_soul`/`access_business_system_write`。

---

## 5. API（`backend/app/api/agents.py`）

- `GET /agents/{agent_id}/maintainers` → `list[MaintainerOut]`（creator 置顶 + `is_creator`）
- `POST /agents/{agent_id}/maintainers` body `{user_id}` → 201（幂等）
- `DELETE /agents/{agent_id}/maintainers/{user_id}` → 204；移除 creator → 400

鉴权：读=登录用户；写= `role in {"platform_admin","org_admin"}`，否则 403。

---

## 6. 前端

- `agentDetailTabs.ts`：`AGENT_DETAIL_TABS` 在 `approvals` 后加 `'maintainers'`。
- `tabs/MaintainersTab.tsx`：列表（creator 徽标）+ 用户搜索 + 添加/移除；写按钮仅 `canManage` 可见；文案 i18n 中/英。
- `src/services/`：`listAgentMaintainers` / `addAgentMaintainer` / `removeAgentMaintainer`。

---

## 7. 测试计划（TDD）

- `test_agent_maintainers_model.py`：模型/迁移守卫（新库幂等、downgrade 往返）。
- `test_maintainer_service.py`：
  - creator 隐式维护人员；add/remove 幂等；remove(creator) 拒绝；list 顺序与 `is_creator`。
  - `resolve_file_modify_permission`：delete/edit/write/move × `workspace/`/`skills/` 门控；`memory/`、非门控工具放行；**`move_file` 源+目标双判**；**组 scope 短路**；**soul.md/enterprise_info 返回 DEFER**；路径归一化防绕过（`workspace/../memory`、绝对路径、`./`、反斜杠、symlink、裸目录 `workspace`）。
- `test_agent_runtime_tool_step_service.py`（改造现有 3 个 L3 删除用例）：
  - 维护人员 → delete 直接执行（无 waiting_request、无审批）。
  - 非维护人员 → `tool_permission_denied` + 文案，无 approval。
- `test_agent_tools_autonomy.py` + ACP 路径回归（`check_tool_autonomy` truthy 修复）。
- `test_agents_api_maintainers.py`：CRUD + 鉴权（admin/非 admin/移除 creator）。
- 全量 `uv run pytest --ignore=tests/test_sso_toggle.py`；`ruff check` + `arch-guard.sh`。

---

## 8. 迁移与上线（生产级）

1. **向后兼容**：`autonomy_policy` 旧键保留不删。存量 delete/modify 的 pending `approval_requests`：**不硬置 rejected**（会卡死等待中的 run）；改为——新门控不再产生这类审批，旧 pending 保留在审批 tab 供人工处理，`resolve_approval` 增加「维护人员也可处理」的兼容分支（否则旧审批只有 creator/platform_admin 能点）。
2. **上线顺序**：迁移 → 服务 + 门控 → API → 前端。迁移是增量新表（守卫、无大表锁）。
3. **回滚**：`f069` 可 downgrade；后端回滚上一镜像即可（门控纯逻辑无数据副作用）。
4. **验证清单**：`/api/health`、alembic 单 head、`agent_maintainers` 表在、非维护人员 delete 被拒、维护人员 delete 成功、creator 默认在名单、admin 增删成功、ACP 路径回归。
5. **红线**：不触碰并行会话脏文件（`config.py`/`checkpointer.py`/`docker-compose.yml`/`agent_workspace_cleanup.py` 等）。

---

## 9. 边界与风险

- **治理层定位（已确认）**：不防 `execute_code`（shell）恶意改文件；只治理文档工具路径。文档已明示。
- **后台 run actor（已确认）**：`actor_user_id` 缺省回退 `creator_id`。因是治理层（非硬边界），「trigger payload 借用 creator 权限」视为可接受；若未来要硬防，再在 trigger 上记录 owner。
- **组工作区（已确认）**：`workspace_scope=group` 短路放行，不纳入门控。
- **`enterprise_info/`**：`DEFER`，维持既有机制。
- **性能**：`is_maintainer` 每轮走 DB 查询；若成为热点再进 `agent_tools_cache` 类 TTL 缓存（先测后定，避免过度设计）。
