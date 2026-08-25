# Agent 维护人员（Maintainer）—— 代码级生产实施计划（v2，逐条核对代码）

日期：2026-08-19（v2：全部代码级事实经 `read_file` 核对，每条标注出处）
状态：待实施

> 本版与前版区别：所有函数名、参数名、已有助手都直接从源码读来，不再凭记忆。

## 0. 已确认决策（同前，不变）

治理层定位；维护人员=审批+操作；取代 delete/modify 的 L1/L2/L3；门控 `workspace/`+`skills/`、`memory/` 放行、`soul.md`/`enterprise_info/` 走既有机制；非维护人员直接拒绝；creator 默认维护人员不可移除；admin=`platform_admin`+`org_admin`；后台 run actor 回退 creator；组工作区短路。

---

## 1. 已验证的代码事实（方案的地基）

### 1.1 目标工具的**真实参数名**（`builtin_tool_definitions.py`，逐个读）

| 工具 | 路径参数 | 其它参数 |
|---|---|---|
| `write_file` | `path` | `content`、`mode`(overwrite/append) |
| `delete_file` | `path` | — |
| `edit_file` | `path` | `old_string`、`new_string`、`replace_all` |
| `move_file` | **`source_path` + `destination_path`** | `overwrite` |

> 前版把 move_file 写成 `source`/`destination`——错。且 `delete_file` 描述自带「Cannot delete soul.md or tasks.json」、`move_file` 自带「Cannot move soul.md, tasks.json, enterprise_info/」，门控实现要与之对齐。

### 1.2 已有「工具→路径参数」映射，直接复用

`builtin_tool_definitions.py` 的 `_PATH_CONVENTION_PARAMS`（dict）已精确列出：
```python
"write_file": ("path",), "delete_file": ("path",), "edit_file": ("path",),
"move_file": ("source_path", "destination_path"),
```
→ 门控助手**复用这张表**提取路径，不自造第三份清单。

### 1.3 路径归一化 vs 防逃逸（两个不同函数）

- `normalize_workspace_path`（`workspace_collaboration.py:92`）：**纯词法**（去 `..`/`\`/前导`/`），不 resolve symlink。
- `safe_agent_path`（`workspace_collaboration.py:107`）：`.resolve()` + `startswith(base)`，**symlink 感知**。

→ 门控的「判定文件属于哪个前缀」必须走 `safe_agent_path` 语义（resolve 后再判前缀），否则 `memory/link→workspace/x` 的符号链接会绕过。

### 1.4 组工作区短路（已核清）

- `SCOPED_WORKSPACE_TOOL_NAMES`（`group_runtime_tools.py:74`）= {list_files, read_file, read_document, search_files, find_files, **write_file, edit_file, delete_file**}——**不含 move_file**（move_file 注释明确「Agent-Workspace-only」）。
- `_is_group_scoped_workspace_call(state, tool, args)`（`tool_step_service.py:461`）= `_is_group_agent_run(state) and tool_name in SCOPED_WORKSPACE_TOOL_NAMES`。

→ 门控短路条件：`_is_group_scoped_workspace_call(...)` 为真时返回「不门控」（组路径）。move_file 永不进组路径，始终按 agent 门控。

### 1.5 三处自主策略执行点的**真实现状**（逐处读，已核对）

| 路径 | 函数（真实） | 现状 |
|---|---|---|
| durable runtime | `RuntimeToolStepService.execute_pending`（`tool_step_service.py:1295`） | 只对 `delete_file` 走 `_delete_autonomy_gate`（L3）；**write/edit/move 无门控** |
| durable 执行器 | `__init__` 默认 `tool_executor = execute_builtin_tool_outcome`（`tool_step_service.py:589`） | `execute_builtin_tool_outcome`（`agent_tools.py:3071`）**本身无自主检查** |
| legacy | `execute_tool`（`agent_tools.py:3519`） | `check_and_enforce`（`agent_tools.py:3562`），走 `_TOOL_AUTONOMY_MAP`（`:1591`，**漏 edit_file**） |
| ACP | `check_tool_autonomy`（`agent_tools.py:24990`） | `allowed = policy.get(category, True)`（`:25020`）把 `"L3"` 当真值 → **永远放行**（bug）；`_TOOL_AUTONOMY_MAP`（`:24981`）**漏 move_file** |

→ 汇总成一张「工具 × 路径」矩阵（见下），这是门控接入的唯一依据。

### 1.6 两份 `_TOOL_AUTONOMY_MAP` 互相矛盾（已核对）

- `:1591`（legacy 用）：write/move/delete + send_*/web_search，**无 edit_file**。
- `:24981`（ACP 用）：write/edit/delete/execute_code/execute_command，**无 move_file**。

### 1.7 actor 来源（已核对）

- `_delete_autonomy_details`（`tool_step_service.py:494`）：`actor_user_id = context.actor_user_id or str(agent.creator_id)`。

---

## 2. 门控判定（修正版）

新建 `backend/app/services/maintainer_service.py`，含一个共享助手：

```python
class FileModifyDecision(Enum):
    GATED_ALLOWED = "gated_allowed"   # 门控路径 + actor 是维护人员
    GATED_DENIED  = "gated_denied"    # 门控路径 + actor 非维护人员
    NOT_GATED     = "not_gated"       # 非门控工具 / 非门控路径 / 组短路
    DEFER         = "defer"           # soul.md / tasks.json / enterprise_info → 既有机制

async def resolve_file_modify_permission(
    db, *, tool_name, arguments, agent, actor_user_id,
    is_group_scoped: bool,     # 直接传 _is_group_scoped_workspace_call 的结果
) -> FileModifyDecision:
```

判定顺序（全部有代码依据）：
1. `is_group_scoped` 为真 → `NOT_GATED`（组路径短路，§1.4）。
2. `tool_name` 不在 `{delete_file, edit_file, write_file, move_file}` → `NOT_GATED`。
3. 用 `_PATH_CONVENTION_PARAMS` 取该工具全部路径参数，**逐个**经 `safe_agent_path` resolve 后判前缀：
   - 任一落在 `workspace/` 或 `skills/` → 进入门控判定；
   - 全部落在 `memory/` → `NOT_GATED`（记忆放行）；
   - 命中 `soul.md` / `tasks.json` / `enterprise_info/` → `DEFER`；
   - 其它 → `NOT_GATED`。
4. `actor_user_id` 为空 → 回退 `agent.creator_id`。
5. `MaintainerService.is_maintainer(actor)` → `GATED_ALLOWED` / `GATED_DENIED`。

**关键**：`move_file` 用 `source_path` + `destination_path` **两个都要判**（`move(memory/x → workspace/secret)` 只判源会漏）。

---

## 3. 三处接入（修正版）

| 调用点 | 改为 |
|---|---|
| `execute_pending`（durable） | 在 `_delete_autonomy_gate` 之前，对 delete/edit/write/move 统一调 `resolve_file_modify_permission`；`GATED_DENIED` → `tool_permission_denied` 结果；`DEFER` → 走既有 soul/enterprise 逻辑；`NOT_GATED` → 放行。`_delete_autonomy_gate` 的 L3 审批移除 |
| `execute_tool`（legacy，`:3519`） | 把 `check_and_enforce`（`:3562`）替换为 `resolve_file_modify_permission`；补 `edit_file` |
| `check_tool_autonomy`（ACP，`:24990`） | **修 truthy bug**（`:25020`）+ 走同一助手；补 `move_file` |

**一并处理**：两份 `_TOOL_AUTONOMY_MAP`（`:1591`/`:24981`）收敛为一份，助手复用 `_PATH_CONVENTION_PARAMS`，不再新增工具清单。

---

## 4. 数据模型 + 迁移（同前，微调）

- `agent_maintainers` 表（`agent_id`+`user_id` 唯一，CASCADE，无 tenant_id 靠 agent_id 隐式隔离）。
- creator 不落表、运行时隐式判定。
- 迁移 `f069`，inspector 守卫 + 对称 downgrade + DDL-only。
- C5 张力：沿用全仓 FK 先例，写进 commit message。

## 5. API / 前端 / 测试 / 上线（同前，要点不变）

- API：`GET/POST/DELETE /agents/{id}/maintainers`，写仅 admin。
- 前端：`maintainers` tab 插 `approvals` 后，写按钮仅 `canManage`。
- 测试新增：`move_file` 双路径、组短路、soul.md/tasks.json DEFER、symlink 绕过、ACP truthy 修复回归。
- 上线：增量新表；存量 pending 审批不硬置 rejected，`resolve_approval` 加维护人员兼容分支。

---

## 6. 待办优先级（同前，不变）

P0：提交修复 1 文案、修 ACP truthy bug。
P1：f069 迁移 → MaintainerService+助手 → 三处接入+map 收敛 → API。
P2：前端 tab、存量审批兼容。
P3：上线+回滚+全量测试+code-review。

---

## 7. 风险备注

- **行号会漂移**：并行会话在本工作区持续改文件（审查期间 `agent_tools.py` 行号已偏移、新增 `NOTES.md`/`workflows/` 等）。实施前必须重新 `git status`/`git log` 对齐 HEAD，且**以函数名定位、不依赖行号**。
- **`move_file` 是 agent-only**（不进组路径），无需组短路；但它在 durable 路径当前**无任何门控**，必须纳入。
- **`convert_*` 工具**（csv_to_xlsx、html_to_pdf 等）也写文档，但不在本次「delete/edit/write/move」范围；治理层定位下暂不纳入，留待后续评估。
