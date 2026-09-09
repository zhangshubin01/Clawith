# 维护者管理 WebUI 模块 — 改代码方案（一期）

> 日期：2026-09-09
> 状态：已实现（2026-09-09，后端+前端+测试+文档注记；Playwright 冒烟待部署后补）
> 关联：`docs/technical-plans/20260905-maintainer-gate-g3-g4-production-plan.md`（门控已上线）
> 目标：在 Agent 设置页新增「维护者」管理模块，支持 查看/添加/删除 维护者。

## 0. 已确认的范围决策

1. **权限模型**：创建者 + 管理员（`platform_admin` / `org_admin` / `identity.is_platform_admin`）都可操作。
2. **只做维护者增删**；「创建者转移」**不在本期做**，列为二期（见 §7）。
3. 无新增 DB 迁移：`agent_maintainers` 表、`Agent.creator_id` 列均已存在；`AuditLog.action` 为自由 `String(100)`。

### 0.1 决策反转记录（必读，评审补充）

本方案的权限模型**推翻了 S3 管理 API 方案已拍板的「决策 D」**，属治理决策变更，显式记录如下：

- 决策 D 原文（`20260907-agent-maintainers-management-api-production-plan.md` §3.2 要点②）：
  「写名单仅 `platform_admin`+`org_admin`，creator 不在名单管理授权内——creator 是隐式维护人员（M-1），可改自己
  agent 的 workspace/skills，但**授予他人是治理动作、归管理员**（雷 3 原文）」。
- 本方案改为「**创建者 + 管理员**」均可增删维护者。理由：
  1. 创建者本就持有该 agent 的全部 workspace/skills 写权，让其管理自己的维护者名单**不提升其自身权力**，
     只是把「owner 管理自己协作团队」这一常识能力补回来。
  2. 参考同类项目（S3 方案 Phase 1 已比对）：**dify 用 `is_owner`（=creator）门控成员写操作**、bisheng/casdoor
     用组织角色（admin）门控——「owner + admin」是业界通行模型，本方案本质是**采纳 dify 的 owner 模型并与
     Clawith 现有 admin 门控叠加**（混合模型），非凭空造新。
  3. 管理员仍保留治理兜底 + 无租户超管跨租户。
- 连带影响：雷 3 原「审批流（creator+platform_admin）与维护人员管理（admin-only）两套主体**不合并**」的边界
  随之移动——维护者管理现含 creator，与审批流的 creator 主体产生重叠，但两者仍是**不同机制**（运行态裁决 vs
  治理配置名单），不合并为同一判定。
- 落地动作：实现时在本方案 `20260907-...-management-api-production-plan.md` 的「决策 D」与 G3/G4 方案 §9.2
  S6（「前端 tab 必须按 admin 角色门控」）处**补一条指向本方案的修订注记**，避免设计文档与代码静默分叉。

## 1. 现状（已核实代码）

### 1.1 门控本体
- `backend/app/services/maintainer_service.py` `MaintainerService.resolve_file_modify_permission`：只拦
  `write_file / edit_file / delete_file / move_file` 四个工具，且仅拦 `workspace/`、`skills/` 前缀路径；
  `memory/` 放行，`soul.md/tasks.json/enterprise_info` defer。
- 判定顺序：a2a 放行 → group 放行 → 非 4 工具放行 → 路径 open 放行 →
  `actor = actor_user_id or agent.creator_id` → `actor == creator_id` 放行（创建者隐式维护者）→
  `is_maintainer(actor)` 放行 → 否则 `GATED_DENIED`。
- 实际拦截点：`backend/app/services/agent_runtime/tool_step_service.py` `_maintainer_file_gate`（L1925）。

### 1.2 数据模型
- `backend/app/models/agent.py`：
  - `Agent.creator_id`（L68）：创建者，创建时写入，不可改（本期不动）。
  - `AgentMaintainer`（L210-236）：`agent_id + user_id + created_by + created_at`，`(agent_id,user_id)` 唯一。
- `AuditLog.action` 自由字符串（`backend/app/models/audit.py` L25）。

### 1.3 现有 API（`backend/app/api/agents.py` L1060-1193）
| 端点 | 方法 | 现状权限 |
|---|---|---|
| `/agents/{agent_id}/maintainers` | GET | `get_current_admin`（仅管理员） |
| `/agents/{agent_id}/maintainers` | POST（`{"user_id"}`） | `get_current_admin` |
| `/agents/{agent_id}/maintainers/{user_id}` | DELETE | `get_current_admin` |

- `get_current_admin`（`backend/app/core/security.py` L215）= `platform_admin` / `org_admin` / `identity.is_platform_admin`。
- `is_agent_creator(user, agent)` = `agent.creator_id == user.id`（`backend/app/core/permissions.py` L573）。
- 租户隔离在 `_require_maintainer_agent`（agents.py L1041-1057）：无租户超管可跨租户，其余须同租户。
- `get_agent_permissions` 已返回 `can_manage / is_owner / creator_id`（L632-726）。

### 1.4 前端
- `frontend/src/pages/agent-detail/AgentDetailPage.tsx` `AccessPermissionsPanel`（L613-1018）已有
  「成员搜索下拉」模式，走 `GET /agents/{id}/permissions/candidates?search=`（该端点会 find-or-create
  未关联网页账号的 org member —— 正是飞书占位用户场景）。
- 该 panel 以 `accessPermissionsPanel` prop 注入 `tabs/SettingsTab.tsx`（L358 渲染）。
- API 客户端 `frontend/src/services/api.ts`；i18n `frontend/src/i18n/zh.json` / `en.json`；
  类型 `frontend/src/types/index.ts`。

## 2. 权限模型（已确认）

**维护者 查看/添加/删除 = 「创建者」或「管理员」都可操作；非二者 后端 403、前端隐藏操作项。**

角色矩阵：

| 操作 | 创建者 | 管理员 | 维护者（非创建者） | 普通成员 |
|---|---|---|---|---|
| 查看维护者列表 | ✅ | ✅ | ❌（403） | ❌（403） |
| 添加维护者 | ✅ | ✅ | ❌ | ❌ |
| 删除维护者 | ✅ | ✅ | ❌ | ❌ |

- 创建者：Agent 所有者，本就是隐式维护者（有写权），授权其管理维护者名单**不提升其自身权力**。
- 管理员：治理兜底 + 无租户超管跨租户。
- 不变式：**维护者本人没有治理权**（只有文件写权），普通成员无任何操作。

## 3. 后端改动

### 3.1 新增「创建者或管理员」依赖
`backend/app/api/agents.py`：新增 `_require_maintainers_admin(db, current_user, agent_id) -> Agent`，
复用 `_require_maintainer_agent` 的租户隔离 + 404，再做「admin OR creator」判定。

**不要手写第 N 份角色判定**（S3 方案 §3.2 明确警告过「避免代码库里第 4 份角色判定分叉」——grep 已确认
`security.py:218`、`permissions.py:44`、`experience.py:112`、`agent_dao.py:124`、`organization.py:22`、
`enterprise.py:83` 等多处各写一份）。改为**抽一个共享谓词**：

```python
# backend/app/core/security.py：新增唯一谓词，get_current_admin 改用它（行为等价）
def is_admin_user(user: User) -> bool:
    return user.role in ("platform_admin", "org_admin") or bool(
        getattr(getattr(user, "identity", None), "is_platform_admin", False)
    )
```

`_require_maintainers_admin` 复用该谓词：

```python
async def _require_maintainers_admin(db, current_user, agent_id) -> Agent:
    agent = await _require_maintainer_agent(db, current_user, agent_id)  # 404 + 租户隔离
    if is_agent_creator(current_user, agent) or is_admin_user(current_user):
        return agent
    raise HTTPException(status.HTTP_403_FORBIDDEN, "Creator or admin access required")
```

- 三个 maintainers 端点 `Depends(get_current_admin)` → `Depends(get_current_user)`，
  端点内 `_require_maintainer_agent(...)` 换成 `_require_maintainers_admin(...)`。
- `get_current_admin` 改用 `is_admin_user` 属**行为等价的小重构**（同谓词、同 identity 兜底），
  实现时若担心爆炸半径，也可保留 `get_current_admin` 原文、仅让新 helper 复用 `is_admin_user`——
  两者都满足「不新增分叉」的要求，取其一即可。

### 3.2 GET maintainers 响应补 creator `email`
`list_agent_maintainers` 的 `creator` 对象补 `email` 字段（现缺）便于 UI 展示。
**不加 `can_manage` 字段**（冗余：GET 本就只对 admin/creator 开放，且前端已用 `permData.is_owner` + `currentUser.role`
自行判定，YAGNI）。

### 3.3 复用候选人搜索
前端添加维护者用现成 `GET /agents/{id}/permissions/candidates?search=`，**不新增后端端点**。

- 该端点对未关联网页账号的 org member 会 `get_platform_user_by_org_member(db, m, agent_tenant_id=agent.tenant_id)`
  find-or-create 并**写入 `tenant_id = agent.tenant_id`**（`agents.py` L860+ 已核实），故候选用户满足
  `add_maintainer` 的「同租户」校验，飞书占位用户可正常入名单。前端只取 `users[]`（丢弃 `agents[]`，避免把
  数字员工加进维护者）。

## 4. 前端改动

### 4.1 API 客户端 `frontend/src/services/api.ts`
新增（挂在 `agentApi` 或新建 `maintainerApi`）：
```ts
maintainers: {
    list: (agentId) => request<{ maintainers: any[]; creator: any }>(`/agents/${agentId}/maintainers`),
    add: (agentId, userId) => request(`/agents/${agentId}/maintainers`, { method: 'POST', body: JSON.stringify({ user_id: userId }) }),
    remove: (agentId, userId) => request(`/agents/${agentId}/maintainers/${userId}`, { method: 'DELETE' }),
}
```

### 4.2 新组件 `MaintainersPanel`
新建 `frontend/src/pages/agent-detail/components/MaintainersPanel.tsx`（独立文件，不塞进 `AccessPermissionsPanel`）：
- props：`agentId`、`isOwner`、`isAdmin`、`queryClient`（**不要传 `canManage`**——见下方门控说明）。
- **渲染门控（关键，勿用 `canManage`）**：`canManage = (agent).access_level === 'manage'`（`AgentDetailPage.tsx`
  L5002）是宽口径，**包含 custom 模式被授予 manage 的非 owner/非 admin 用户**——这类用户能进 SettingsTab，
  但 `GET /maintainers` 会对他们 403。故面板**只在 `isOwner || isAdmin` 时整体渲染 + 发起列表请求**
  （`useQuery` 的 `enabled` 同条件），否则不渲染、不发请求，避免 403 报错与空态误显。
  - `isOwner` 复用 `AgentDetailPage.tsx` L2439 已算好的 `isAgentOwner`（`currentUser.id === agent.creator_id`）。
  - `isAdmin` = `currentUser.role === 'platform_admin' || 'org_admin' || !!(currentUser as any)?.is_platform_admin`。
- 显示：
  - 创建者卡片（name / @username，标「创建者 · 隐式维护者」，不可删除）。
  - 维护者列表（name / @username + 删除按钮）。
- 操作（面板已渲染即代表有权限）：
  - **添加维护者**：复用 `AccessPermissionsPanel` 的候选人搜索下拉（`/permissions/candidates`）。
  - **删除维护者**：`ConfirmModal` 二次确认。
- 操作后 `queryClient.invalidateQueries(['agent-maintainers', agentId])`。

### 4.3 接线 `SettingsTab`
- `SettingsTab.tsx` 新增 prop `maintainersPanel: ReactNode`，在 `{accessPermissionsPanel}`（L358）之后渲染。
- `AgentDetailPage.tsx`（L7701-7738）向 `SettingsTab` 传：
  `maintainersPanel={isAgentOwner || isAdmin ? <MaintainersPanel agentId={id} isOwner={isAgentOwner} isAdmin={isAdmin} queryClient={queryClient} /> : null}`
  —— 非 owner/admin 传 `null`，与后端 403 对齐。

### 4.4 i18n
`zh.json` / `en.json` 新增 `agent.settings.maintainers.*`（标题、描述、添加、删除、确认、空态、创建者标签）。

## 5. 测试改动

### 5.1 后端单测 `backend/tests/test_agent_maintainers_api.py`
- 现有「仅管理员」用例改为「创建者或管理员」：
  - 新增正向：creator 可 list/add/remove。
  - 保留：非 creator、非 admin 普通成员 403。
  - **新增：custom 模式被授予 manage 的非 owner/非 admin 用户 → 403**（覆盖「canManage 宽口径」与后端窄口径的边界）。
  - 保留：跨租户 org_admin 403、无租户超管跨租户 200。
  - 保留：不能添加/删除 creator 本人（409/400）、重复添加 409、inactive/跨租户目标 400。
- 视需要补 `test_maintainer_service.py`（service 层逻辑无变化，仅确认无需改动）。

### 5.2 前端
- 若有组件测试基建则补 `MaintainersPanel` 渲染/权限开关用例（**关键断言**：非 owner/admin 传 `null` 不渲染、
  不发起 `GET /maintainers`）；否则以 `npm run build` + Playwright 冒烟为准。

## 6. 验证清单（实现后逐条跑）

1. `scripts/arch-guard.sh` 通过（后端改动）。
2. 后端：`pytest backend/tests/test_agent_maintainers_api.py backend/tests/test_maintainer_service.py`。
3. 前端：`npm run build` 通过。
4. Playwright 冒烟（`clawith-local-dev` / `clawith-frontend-triage`）：
   - 创建者登录：设置页出现「维护者」模块，可添加/删除维护者。
   - 管理员登录：同上可用。
   - 普通成员登录：不出现该模块（或 403 无操作项）。
   - 飞书占位用户被加入维护者后，`edit_file/write_file` 不再 `tool_permission_denied`。

## 7. 二期（本期不做）

- **创建者转移** `POST /agents/{id}/transfer`：改动 `creator_id` 影响访问控制 / private 模式 / `agent_permissions`，
  属最敏感一环，单独立项，含权限迁移 + AuditLog + 转移弹窗强确认 + 可选「旧创建者转入维护者」。

## 8. 边界说明：与「飞书身份关联」修复方案的关系

本方案是**治理侧**手段：把某用户加入维护者名单，即可让其驱动 `write_file/edit_file/delete_file/move_file`。
但它**不是**用户最初「飞书身份未关联 → 被识别成占位用户 f50eb606」的根治——那属于**身份侧**问题，已有
`docs/technical-plans/20260909-session-compact-thinking-and-feishu-identity-gate-fix-plan.md` 在跟进。

两者关系：身份关联（治本）让飞书身份解析回真正的创建者账号；维护者名单（本方案）让授权用户无需是创建者
即可写文件。二者互补、可并行，实现本方案时不必等待身份关联落地。
