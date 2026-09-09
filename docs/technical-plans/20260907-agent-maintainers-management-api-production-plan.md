# 生产级修复方案：agent_maintainers 管理 API（鉴权含 org_admin）

日期：2026-09-07
状态：已实施（Phase 5 code-review 双轴已跑，Spec 轴 1 项违约已回改「补 `get_current_admin` 角色门控测试」；验证全绿 37 passed + ruff + arch-guard；交付硬绑定已开前端 tab 票）
前置：`20260905-maintainer-gate-g3-g4-production-plan.md`（Maintainer 门控已上线 `0b95c42b`，迁移 f077 已应用）
TODO 来源：该方案 §9.2 **S3** + §9.3 验收红线「agent_maintainers 管理 API 鉴权含 org_admin」——`is_maintainer` 分支目前只有 creator 隐式可达，名单写入口缺失。

## 裁决

**评审通过（有条件）**：本方案为最小、可回退、带回归测试的增量实现；仅新增 3 个只读/治理端点 + 3 个服务方法，零既有契约变更、零迁移、零前端改动。两处**已知风险**已在正文显式标注；风险①（前端 tab 门控错位）不靠人记，已升级为**阻塞性前置依赖**（交付时同步开前端 tab 票、靠依赖链兜底，见「评审裁决汇总」）。

---

## Phase 1: 参考对比（≥10 项目，含诚实负结论）

决策点拆解：①「谁能管理维护人员名单」的授权主体语义；② 子资源 REST 形态；③ 隐式 owner 不可移出；④ 名单=join 表（唯一约束）。逐项对齐：

| # | 参考项目 | 关键证据（真实源码） | 结论 / 偏离 |
|---|---|---|---|
| 1 | **dify**（T1） | `dify/api/core/rbac/entities.py:60` `WORKSPACE_MEMBER_MANAGE="workspace_member_manage"` 独立 RBAC 权限；`api/controllers/console/workspace/members.py:438/473/519` 写操作用 `TenantService.is_owner(...)` 门控；路由 `/workspaces/<id>/members` + `/<member_id>`；`rbac.py:224` owner/admin/editor/normal/dataset_operator 角色金字塔 | 名单管理=**独立权限 + 子资源 REST**，两者均可抄。**偏离**：dify 写操作用 **owner（=creator）** 门控，Clawith 决策 D 拍板用 **admin（platform_admin+org_admin）** 门控——这是有意偏离（雷 3：审批流/维护人员管理是治理配置，归属管理员，非 creator），见 Q5 |
| 2 | **bisheng**（Clawith 上游姊妹项目，T1） | `test/user/test_user_list_without_org_scope.py` 头注「The endpoint gates on organisational roles — super admin, user-group admin」；`RoleService` + `is_admin` + role API 写操作落 audit log；OpenFGA owner>manager>editor>viewer 金字塔 | **组织角色门控 + 写操作审计**：admin 是「组织角色」而非单一全局旗标，与决策 D（platform_admin+org_admin 两者）同构 |
| 3 | **casdoor**（T3） | `routers/authz_filter.go:376` `user.IsGlobalAdmin() \|\| (user.IsAdmin && impUserOwner == user.Owner)`；SCIM `group_handler.go` add/remove/set group members | **admin 按 owner（租户）域隔离**：org admin 仅限自己 owner 域、global admin 越过。与 Clawith「org_admin 租户隔离 + 无租户 platform_admin 可跨租户」逐字同构 |
| 4 | **LangBot**（T1） | `pipeline/process/handlers/command.py:54` `is_admin=(privilege == 2)` 特权位检查；`docs/multi-tenant/` 9 份文档证 tenant RLS | 特权位 + 租户 RLS 双层，与 Clawith role 枚举 + 租户过滤一致 |
| 5 | **letta-code**（T1） | `src/memory-confinement.ts` → `permissions/memory-confinement-launcher` + cross-agent-guard | **负结论**：单用户 harness，无「谁能管理」协作名单 API；其 fail-closed 隔离是**记忆读写隔离**非名单治理，不可照搬 |
| 6 | **FastGPT**（T3） | `test/datas/users.ts:97` `MongoTeamMember` `role:'owner'`；team 成员 join 表 | 名单=成员 join 表、owner 是成员角色；Clawith 的 creator **不落表**（隐式判定）是对它的简化（M-1） |
| 7 | **MaxKB**（T3） | `apps/tools/serializers/tool.py:50` `is_workspace_manage` / `is_workspace_manage_permission_read`；`WorkspaceUserResourcePermission` + `workspace_user_role_mapping` | 资源权限=用户-角色映射 + manage 守卫函数，与 Clawith `can_manage_agent` 模式一致 |
| 8 | **new-api**（T3） | `middleware/audit.go:100-137` `AdminAuth/RootAuth` 中间件 + `beginAdminAudit/finishAdminAudit` 审计兜底 + `operatorRole := c.GetInt("role")` | **admin 鉴权收进中间件 + 审计兜底**，保证「新增接口自动留痕」——Clawith 无中间件层、审计走 `AuditLog`，本方案对齐其「写操作留痕」思想但落在端点层 |
| 9 | **coze-studio**（T3） | backend 切片为 infra（eventbus/rmq/kafka 等） | **负结论**：OSS backend 无成员管理路由（成员管理在未开源的 service 层），不可读 |
| 10 | **deepagents**（T0） | `middleware/` 钩子（`_message_eviction`/`_prompt_caching`） | **负结论**：库非多租户平台，无人类协作名单管理；仅证「在唯一执行边界加纯函数判定」模式（已用于门控本体） |
| 11 | **openai-agents-python / gptme / codex / gemini-cli**（T1/T2） | 单用户 CLI agent 源码 | **负结论**：单用户、无协作名单 API（4 个合并为 1 条负结论） |
| 12 | **12-factor-agents**（方法论） | ownership boundary 原则 | 维护人员名单=把「谁拥有 workspace/skills 写权限」显式化，对齐 ownership boundary |
| 13 | **claude-agent-acp**（T2） | `docs/permission-extension.md`（已读真实源码，见门控方案 §1） | 其权限模型针对「工具执行时 per-session 权限」，**非治理配置名单**，与名单管理不同层，不可照搬 |

**无相关参考的类别（明示）**：「非 creator 用户驱动 agent 改文件」的权限主体语义在清单里无逐字可抄项（同门控方案 §1 结论），本方案按平台自身事实（决策 D + M-1）设计，偏离已在各决策点写明理由。

---

## Phase 2: 双源定根因（S3 缺口，非凭记忆）

**症状**：Maintainer 门控已上线（0b95c42b，f077），但 `agent_maintainers` 名单没有任何读/写入口——门控的 `is_maintainer` 分支实际不可达，只有 creator 隐式放行生效。

**源码证据（read_file 核对）**：
- `app/services/maintainer_service.py:102-113` `MaintainerService.is_maintainer` 查 `agent_maintainers` 表；`:142-151` `resolve_file_modify_permission` 中 `actor == agent.creator_id` 短路放行后，其余 actor 走 `is_maintainer`。
- 全仓 grep `maintainers`/`AgentMaintainer` 于 `app/api/`、`app/dao/`、`app/schemas/`：**零命中**（无端点、无 DAO、无 schema）。`AgentMaintainer` 模型（`app/models/agent.py:210-236`）与迁移 `v1_11_4_f077_add_agent_maintainers.py` 已存在，但**无任何写路径**。

**运行数据证据（PG 只读直查）**：
```
SELECT count(*) FROM agent_maintainers;  → 0
```
表已建（f077 applied）、**0 行**——证实名单为空且无写入途径，不是「有数据没读」。

**最深根因**：门控方案把「管理 API」列为 §3.4 P2 / 验收红线残留（S3），实施时只落地了运行时门控（`resolve_file_modify_permission`）与数据表，**漏掉了名单的治理写入口**。可证伪：若「管理 API 缺失」是根因，则补上 GET/POST/DELETE 后，org_admin 可增删名单、`is_maintainer` 分支可达、0 行名单可被填充——验收红线「鉴权含 org_admin」达成。

---

## Phase 3: 修复方案（最小、可回退）

### 3.1 端点（新增，`backend/app/api/agents.py`）

| 方法 | 路径 | 鉴权 | 语义 |
|---|---|---|---|
| GET | `/agents/{agent_id}/maintainers` | admin | 列出维护人员 + 隐式 creator |
| POST | `/agents/{agent_id}/maintainers` | admin | 添加维护人员（幂等） |
| DELETE | `/agents/{agent_id}/maintainers/{user_id}` | admin | 移除维护人员 |

### 3.2 鉴权（复用 `get_current_admin`）+ helper（新增，`agents.py` 内）

端点签名用 `current_admin: User = Depends(get_current_admin)`（`app/core/security.py:215-220`）——角色判定（platform_admin + org_admin + `identity.is_platform_admin` 兜底）由该 dependency 完成，**不在 helper 里重复手写**（避免代码库里第 4 份角色判定分叉）。

```python
async def _require_maintainer_agent(db, current_user: User, agent_id: uuid.UUID) -> Agent:
    # arch-guard: allow (platform_admin cross-tenant) — 超管需跨租户，用父类 get() 而非 tenant-scoped get_active()
    agent = await agent_dao.get(agent_id, db=db)
    if not agent or agent.deleted_at is not None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Agent not found")
    # 手动租户隔离：org_admin 与有租户的 platform_admin 必须同租户；无租户 platform_admin（超管）可跨租户
    if current_user.tenant_id is not None and agent.tenant_id != current_user.tenant_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "No access to this agent")
    return agent
```

要点：① 租户隔离对齐 casdoor `IsGlobalAdmin || (IsAdmin && owner==user.Owner)` 语义；② **决策 D 拍板**：写名单仅 `platform_admin`+`org_admin`，creator 不在名单管理授权内（creator 是隐式维护人员 M-1，可改自己 agent 的 workspace/skills，但**授予他人**是治理动作、归管理员，雷 3 原文）；③ **勿抄 `super_admin`**：role 枚举（`app/models/user.py:78`）仅 `platform_admin/org_admin/agent_admin/member`，**无 `super_admin`**——`delete_agent`（`agents.py:1053`）与 `files.py:219` 里的 `"super_admin"` 是死字符串（历史残留），实现时不要照抄。④ **关键陷阱：`agent_dao.get_active()` 是 tenant-scoped**（`AgentDAO` 继承 `TenantScopedBaseDAO`，`Agent.tenant_id` 非空列被 session 级 `with_loader_criteria`（`dao/base.py:147-171`）注入当前 `_tenant_ctx` 租户过滤；超管无租户上下文时 `_require_tenant_id()` 返 None → `Agent.tenant_id == None` 恒 false → 必返 None/404）。故**不能用 `get_active`**，改用父类 `BaseDAO.get()`（`agent_dao.get(id)` 跨租户直查、绕过 loader criteria），手动做软删除（`deleted_at is None`）+ 租户隔离——这正是 `agent_dao.py` docstring 明示的「platform_admin cross-tenant 用 `BaseDAO.get()` + `# arch-guard: allow`」用法。

### 3.3 服务方法（新增，`backend/app/services/maintainer_service.py`）

> **修订注记（2026-09-09）**：上文「② 决策 D 拍板」已**反转**——维护者名单管理权限由「仅 admin」改为「**创建者 + 管理员**」，理由与依据见 `20260909-maintainer-creator-management-ui-plan.md` §0.1（采纳 dify 的 owner 模型并与 Clawith 既有 admin 门控叠加）。实现据此落地：端点改 `Depends(get_current_user)` + `_require_maintainers_admin`（creator OR `is_admin_user`），共享谓词 `is_admin_user` 抽于 `app/core/security.py`。

在 `MaintainerService` 增 3 个方法（复用 `is_maintainer` 的查询模式）：

```python
async def list_maintainers(self, db, agent_id) -> Sequence[AgentMaintainer]      # order_by created_at
async def add_maintainer(self, db, *, agent_id, user_id, created_by) -> AgentMaintainer
async def remove_maintainer(self, db, *, agent_id, user_id) -> bool              # False = 不存在
```

校验（端点层，trust boundary）：
- **POST**：目标 user 存在且 `is_active`、`user.tenant_id == agent.tenant_id`（否则 400）；`user_id == agent.creator_id` → 409（creator 隐式、不落表，M-1）；已是维护人员 → 409（唯一约束 `uq_agent_maintainers_agent_user` 幂等，预查 `is_maintainer` 避免裸 `IntegrityError` 泄漏）。**并发竞态兜底**：预查是 check-then-act，两个并发 POST 同一 user 时唯一约束会兜底抛 `IntegrityError`——端点 catch 后转 409（而非 500），保证幂等语义在竞态下也成立。
- **DELETE**：`user_id == agent.creator_id` → 400「creator 是隐式维护人员、不可移除」；非维护人员 → 404。

**审计留痕（对齐 `agents.py` `delete_agent` 的 `AuditLog` 惯例）**：治理写操作必须落 `AuditLog`——POST 成功写 `AuditLog(user_id=当前 admin, agent_id, action="maintainer_added", details={"target_user_id","resource_id","tenant_id"})`；DELETE 成功写 `action="maintainer_removed"`（同 `details` 结构）。`add_maintainer` 的 `created_by` 是**行级留痕**（落 `AgentMaintainer.created_by`）；删除行即无行级留痕，`removed_by` 只落 `AuditLog.user_id`（审计流）——两者都留、删除不留无痕。（实现注：`remove_maintainer` 故**不加 `removed_by` 参数**——`AgentMaintainer` 无该列、删除后行已消失，参数无落点。）

### 3.4 响应与序列化

- GET 返回 `{"maintainers": [{"id","user_id","name","username","email","created_by","created_at"}], "creator": {"user_id","name","username","is_implicit":true}}`，用户展示信息复用 `user_dao.list_by_ids` + `user_dao.get_with_identity`（同 `agents.py:630` permissions 端点模式）。
- POST 返回创建的维护人员（201）；DELETE 返回 204。

### 3.5 回归测试（TDD，宪法 IV）

- `test_maintainer_service.py` 增：`list/add/remove` 纯服务方法（重复 add 幂等、remove 不存在返 False）。
- 新 `test_agent_maintainers_api.py`（沿 `test_agent_delete_api.py` 的直调 + `RecordingDB`/`make_user`/`make_agent` 模式）：
  1. org_admin / platform_admin → GET/POST/DELETE 均放行；
  2. member / agent_admin / creator（非 admin）→ 403；
  3. 跨租户 org_admin → 403；
  4. POST `user_id == creator_id` → 409；POST 重复 → 409；POST 跨租户 user → 400；
  5. DELETE creator → 400；DELETE 不存在 → 404；
  6. 审计留痕：POST 成功后 `AuditLog(action="maintainer_added", details.target_user_id=...)` 落库；DELETE 成功后 `AuditLog(action="maintainer_removed")` 落库（治理写操作审计不缺失，对齐 `delete_agent`）。

### 3.6 影响面（爆炸半径）

- **纯增量**：新端点 + 新服务方法，无既有函数契约变更、无模型/迁移变更（`AgentMaintainer` + f077 已就位）、无运行时门控改动（`is_maintainer` 未动）。
- 前端：**本方案不含**（TODO 标题限定「管理 API」）；维护人员 tab 是独立 P2（门控方案 §9），届时前端须按 **admin 角色** 而非 `canManage` 门控（`canManage` ⊃ admin，含 creator，会与 admin-only 后端错位——见 Q4）。**硬绑定**：本方案交付时**同步开前端 tab 票**，并标注「阻塞性前置依赖：依赖本方案 admin 角色门控、禁复用 canManage」——闭环不靠人记、靠依赖链兜底。

> **修订注记（2026-09-09）**：前端 tab 已实现（`20260909-maintainer-creator-management-ui-plan.md`）。门控口径随「决策 D 反转」同步改为 **`isOwner || isAdmin`**（窄口径），仍**禁复用 `canManage`**（`canManage` = access_level==='manage' 含 custom 模式被授予 manage 的非 owner/admin 用户，会对 `/maintainers` 403）。`isAdmin` = `role in (platform_admin, org_admin) || is_platform_admin`。

---

## Phase 4: 7 角度评审（裁决 → 正向依据 → 负向探针）

**1. 根因是否正确？** — **通过**
- 正向：根因（管理 API 缺失）解释全部症状——源码零 `AgentMaintainer` 引用（无写入口）+ DB 0 行（名单空）+ `is_maintainer` 分支不可达（仅 creator 短路生效）。已追到最深：不是「有 API 但没接前端」，是**根本没有治理写入口**（grep 零命中）。
- 负向（反例测试）：「若根因是『API 缺失』，则 DB 应显示 0 行且源码应无端点」——我实测 `count(*)=0` 且 grep `maintainers` 于 api/dao/schemas 零命中，证实；若根因是「有 API 但被删」，grep 应残留路由注册或 import——无，推翻对立假设。

**2. 方案是否根治？** — **通过**
- 正向：方案直接补上缺失的写/读入口（3 端点 + 服务方法），使 `is_maintainer` 分支可达、名单可填充，命中 S3/验收红线。
- 负向（删除测试）：「把本方案删掉，问『名单写入口缺失』会不会复发」——会，因为没有任何其他代码路径写 `agent_maintainers`（grep 零命中），只有本方案新增的 POST/DELETE 能写。证实根治。

**3. 参考资料是否正确？** — **通过**
- 正向：引用的都是**名单/成员/授权管理**的同类机制（dify `workspace_member_manage`+is_owner、casdoor owner-scoped admin、bisheng 组织角色门控），非同名 false friend；均读真实源码（grep 到具体行）。
- 负向（反例测试）：「我找了一个可能引用错的点——dify 的 `workspace_member_manage` 是否真是『成员管理』权限而非普通成员标识」——核对 `entities.py:60` 它是 `Permission` 枚举成员、`members.py` 用其门控写操作，无误；coze-studio 我本可臆测它有成员 API，实测 backend 仅 infra、诚实记负结论，无误。

**4. 副作用与爆炸半径是否排查完？** — **通过（已知风险 1 处；本轮负向探针补审计留痕 1 处）**
- 正向：①副作用——无外部写（纯 PG 单表写）、无缓存、无连接/资源新增；唯一约束保证幂等（重复 POST 预查返回 409，不触发裸 `IntegrityError`）。**审计留痕**：治理写操作按平台惯例落 `AuditLog`（`delete_agent`→`agent_deleted`、`groups.py`→`_stage_audit`），本方案 POST/DELETE 补 `maintainer_added`/`maintainer_removed`，删除不留无痕。②影响面——纯增量端点，`is_maintainer` 未动，无既有消费者受影响。
- 负向（反例测试）：「我特意找过一个会漏掉的消费者——**前端 `canManage`（=access_level==='manage'，含 creator）与 admin-only 后端的错位**」——核对：当前前端无维护人员 tab（grep `maintainer` 于 frontend/src 零命中），故本期无消费者受影响；但未来建 tab 若沿用 §9「复用 canManage」，creator 会看到 tab 却 GET 403。**已知风险 + 缓解**：在方案 §3.6 明示「前端 tab 必须按 admin 角色门控」，并升级为**阻塞性前置依赖**——本方案交付时同步开前端 tab 票、标注依赖（不靠人记、靠依赖链兜底）。另补一轮负向探针：「平台治理写操作是否都落审计？若漏，审计链断」——核对 `delete_agent`（agents.py:1071 `AuditLog(action="agent_deleted")`）与 `groups.py` `_stage_audit` 全家桶为惯例，方案初稿只落 `created_by` 字段、未落 `AuditLog`，**已回改 §3.3 补审计**。

**5. 是否最优且必要？** — **通过**
- 正向：①枚举 ≥3 候选——(a) 只加 POST/DELETE 不加 GET（更小）；(b) 本方案（GET+POST+DELETE，完整名单治理）；(c) 加独立 `MaintainerDAO` + pydantic 全套（更彻底）。选 (b)：GET 是「名单可读」的最小闭环（否则无法核对已授名单），DAO 属过度分层（服务已有 `is_maintainer`，3 方法内联即可）。②修的是**已发生的验收红线缺失**（S3/§9.3 明确「待办」），非臆想风险。
- 负向（反例测试）：「更小的一档 (a) 能否解决问题」——不能，缺 GET 则授予后无法核对、且验收红线原文「管理 API」含 GET；「更彻底的一档 (c) 是否更优」——不是，`agent_maintainers` 仅 5 列单表、无跨实体查询，独立 DAO 是投机式分层（宪法 II / Ponytail 第 1 档），当前无消费者需要。

**6. 是否已有可复用逻辑？** — **通过**
- 正向：复用 `MaintainerService.is_maintainer` 查询模式（同表同条件）；复用 `agent_dao.get_active`、`user_dao.list_by_ids/get_with_identity`、`get_current_admin` 的 identity 兜底；鉴权模式复用 `agents.py` 既有 admin 检查（`resolve_agent_approval` 等）。
- 负向（反例测试）：「我特意查过知识图谱/代码是否已有等价『名单管理』端点或 DAO」——grep api/dao/schemas 零命中 `AgentMaintainer`，`is_maintainer` 是唯一现有消费方，无等价逻辑，结论「无→新建（最小）」。

**7. 会破坏 Clawith 特性吗？** — **通过**
- 正向：逐条过宪法 C1–C6——C1 证据先行（Phase 2 双源）、C2 最小改动（纯增量端点）、C3 契约所有权（`agent_maintainers` 唯一写入口 = 本 API，运行时门控只读 `is_maintainer`，不双写）、C4 测试证行为（§3.5）、C5 保留既有工作（不碰运行时门控/迁移）、C6 模块边界（服务方法归 `maintainer_service`）。红线——durable run/checkpoint 无关、多租户隔离（tenant 检查在 helper）、exactly-once 无外部写、前缀缓存无关、WS 状态机无关、飞书通道无关。
- 负向（反例测试）：「把方案对红线过一遍，找是否碰到 checkpoint 语义/前缀缓存前缀/门控判定」——门控判定只读 `is_maintainer`（未改），本 API 只写名单、不碰 checkpoint/cache 前缀，结论「不碰」。

**评审裁决汇总**：7 问全过；已知风险 2 处——①前端 tab 须按 admin 角色门控而非 canManage：**维持通过但加硬绑定**，本方案交付时**同步开前端 tab 票**并标注「阻塞性前置依赖」，闭环靠依赖链兜底、不靠人记；②admin-only 与 dify owner-only 的偏离是决策 D 拍板、已文档化。均已在方案正文标注缓解措施，本方案核心无需回改。

**复审回改（本轮负向探针新挖，已落正文）**：①§3.3 补 `AuditLog` 审计留痕（`maintainer_added`/`maintainer_removed`）——初稿只留 `created_by`、删除无痕，与平台审计惯例不一致（删除的 `removed_by` 落 `AuditLog.user_id`，不落已删行）；②§3.2 端点改用 `Depends(get_current_admin)` 复用角色判定（不手写第 4 份）、helper 只做 agent 解析 + 租户隔离，并注明 `super_admin` 是死字符串勿抄；③§3.2 helper 由 `get_active()` 改用父类 `BaseDAO.get()`（tenant-scoped 陷阱：`get_active` 按 `_tenant_ctx` 过滤致超管跨租户 404、手动租户检查成死代码），手动软删除 + 租户隔离 + `# arch-guard: allow`；④§3.3 POST 补并发竞态兜底（catch `IntegrityError` → 409）。

---

## Phase 5: 实现落地闭环（已执行）

方案落地为 diff 后，须跑 `code-review` 双轴复核：Spec 轴对照本方案（3 端点 + helper + 3 服务方法 + 测试，无夹带），Standards 轴对照 `backend/AGENTS.md`。验证命令：`cd backend && .venv/bin/python -m pytest tests/test_maintainer_service.py tests/test_agent_maintainers_api.py -p no:cacheprovider` + `ruff` + `scripts/arch-guard.sh`。跳过或偏离未解决 = 不算闭环。

**执行结果（2026-09-07）**：

- **Spec 轴**：2 项发现（同根源）——`get_current_admin` 角色门控无任何测试证明（§3.5 第 1/2 条「org_admin/platform_admin 放行」「member/agent_admin/creator→403」被测试文件 docstring 主动跳过）。**已回改**：`test_agent_maintainers_api.py` 新增 `get_current_admin` 角色矩阵测试 5 条（org_admin/platform_admin 放行 ×2、identity.is_platform_admin 兜底放行 ×1、member/agent_admin→403 ×2），直接兑现验收红线「鉴权含 org_admin」。
- **Standards 轴**：11 项全为「判断项」（0 硬违规），均与既有 `delete_agent` 内联编排/审计、`data: dict` 既有惯例、方案 §3.2「父类 get()+手动租户隔离」决策一致 → **采纳不回改**（回改将违反宪法 II 最小改动 + 偏离本文件既有模式）。最重一项「业务编排入 handler」与 `delete_agent`（`agents.py:1196`）同款，非本 diff 引入的新偏离。
- **验证**：pytest 37 passed（含回归 `test_agent_delete_api.py`/`test_agent_permission_candidates.py`）；`ruff check` All checks passed；`scripts/arch-guard.sh` P0 全净（仅既有 legacy frontend 行数 warning）。

**交付硬绑定**：本方案交付的同时，**同步开前端 tab 票**（维护人员 tab，按 admin 角色门控），并标注「阻塞性前置依赖：依赖本方案 admin 角色门控、禁复用 canManage（风险①）」——该票未开 = 风险①闭环未完成，不算交付完整。**已开**：见 `20260905-maintainer-gate-g3-g4-production-plan.md` §9.2 S6。

## 回滚

纯增量：revert 提交即回滚；`agent_maintainers` 表保留（无消费方即无害，同 f072 规约）。无行为契约变更（门控判定未动），无需 release note 级别的产品契约告警。

## 验收红线对照

| 子项 | 状态 |
|---|---|
| `agent_maintainers` 管理 API（GET/POST/DELETE） | 本方案 |
| 鉴权含 org_admin | 本方案（helper：platform_admin + org_admin + identity 兜底） |
| `is_maintainer` 分支可达 | 本方案落地后（POST 写入名单即可达） |
