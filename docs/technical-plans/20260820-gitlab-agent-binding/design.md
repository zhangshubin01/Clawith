# Design: GitLab Agent Binding

- 日期：2026-08-20（第 0 步实测 2026-08-26 通过）
- 状态：待用户确认（SDD 第 2 步，含 Constitution 检查）
- 依据：`spec.md`（同目录，v2 已确认）

## 1. 模块总览

```
SettingsTab.tsx（前端）── GitlabBinding.tsx ── gitlabBindingApi ──┐
                                                                  ▼ HTTP /api
main.py ── gitlab_binding_router ── app/api/gitlab_binding.py ──┐
        │  GET/PUT/DELETE（权限：check_agent_access）            │
        ▼                                                        ▼
   ChannelConfig（channel_configs，channel_type='gitlab'）  asyncio.create_task
   app_secret=加密PAT；extra_config=绑定/状态                    │
                                                                 ▼
                                    app/services/gitlab_workspace.py
                                    run_gitlab_workspace_init()：三态初始化
                                    + 旧布局迁移 + inject origin 自愈
                                    （git 子进程参数数组，无 shell）
                                    → /data/agents/<aid>/workspace/<项目名>/
                                      （仓库目录；workspace 根可放其他不入库文件）
```

新增/修改文件清单见 §7。

## 2. 后端设计

### 2.1 配置（`app/config.py`）

Settings 增加字段（pydantic BaseSettings，env_file 自动加载）：
```python
gitlab_base_url: str = "http://192.168.5.254"   # env: GITLAB_BASE_URL
```
只允许 http(s) 值；供凭证重写与 clone URL 拼接（`f"{base}/<project_path>.git"`）。

### 2.2 Alembic 迁移（`backend/alembic/versions/<ts>_add_gitlab_channel_type.py`）

按 `backend/alembic/AGENTS.md`：`alembic revision --autogenerate -m ...` 取单头 `down_revision`；写完后 `alembic heads` 必须仅一行。

```python
def upgrade():
    # PG15 事务内 ADD VALUE 安全（新值不可在同一事务使用——本迁移不再使用该值）
    op.execute("ALTER TYPE channel_type_enum ADD VALUE IF NOT EXISTS 'gitlab'")

def downgrade():
    pass  # PostgreSQL 不支持删除 enum 值；此迁移不可逆（回滚=保留值，无副作用）
```
DDL-only ✓（无数据操作）。enum 名 `channel_type_enum` 取自 `models/channel_config.py` 现有定义。

### 2.3 API 层（新增 `app/api/gitlab_binding.py`）

`router = APIRouter(prefix="/agents/{agent_id}/gitlab-binding", tags=["gitlab-binding"])`；`main.py` 注册：`app.include_router(gitlab_binding_router, prefix=settings.API_PREFIX)`。

**Schemas**
```python
class GitlabBindingPut(BaseModel):
    token: str | None = None          # 首次必填；已有绑定时 None=保留旧 token
    project_path: str                 # 如 liuyl/wwg1b（含子组）
    default_branch: str | None = None # 默认 f_android_ai

class GitlabBindingOut(BaseModel):
    configured: bool
    project_path: str
    default_branch: str
    has_token: bool                   # 由 app_secret 是否存在得出
    init_status: str                  # pending|initializing|done|failed
    init_error: str | None
    init_updated_at: str | None
```

**端点**
- `GET /` — 查 ChannelConfig(channel_type='gitlab')，返回 Out（**不回 token**）。未绑定 → `configured=False` + 默认值。
- `PUT /` — 流程：
  1. 权限：`check_agent_access(db, current_user, agent_id)`，非 manage 且非 platform_admin/org_admin → 403（同 `agent_credentials.py` 模式）
  2. 校验：`project_path` 非空且无空白字符；`default_branch` 匹配 `^[A-Za-z0-9_\-./]+$`；**无既有绑定且 token 为空 → 422**
  3. upsert ChannelConfig：`app_secret=encrypt_data(token, SECRET_KEY)`（token 非空时）、`extra_config` 合并 `{project_path, default_branch, init_status:'pending'}`、`is_configured=True`
  4. `await db.commit()` 后 `asyncio.create_task(run_gitlab_workspace_init(agent_id, project_path, default_branch, token))`（token 明文只在此传递，不落日志）
  5. 返回 `{"ok": True}`
- `DELETE /` — 权限同上；`is_configured=False`、`app_secret=None`、`extra_config.init_status='unbound'`；若有工作区仓库，`git config --local --unset url.<rewrite>.insteadOf` 移除凭证（无仓库则跳过）——**按候选路径列表逐一清理**：先 `workspace/<项目名>/`（由 extra_config.project_path 经 `_repo_dir_name` 推导，非法则跳过），再旧布局的 workspace 根（兼容 v2 存量），对存在 `.git` 的路径执行 unset；**文件与 .git 不动**。返回 204。

### 2.4 服务层（新增 `app/services/gitlab_workspace.py`，目标 <300 行）

```python
_AGENT_INIT_LOCKS: dict[uuid.UUID, asyncio.Lock] = {}

def repo_root(agent_id) -> Path:            # WORKSPACE_ROOT/<aid>/workspace（根，可放其他不入库文件）
def _repo_dir_name(project_path) -> str:    # 取末段；^[\w.-]+$（UNICODE）且拒绝 . / .. / .git / .tmp；非法 raise ValueError
def repo_path(agent_id, project_path) -> Path:
    # repo_root(agent_id) / _repo_dir_name(project_path) —— 仓库目录
def _credential_rewrite(base_url: str, pat: str) -> str:
    # f"https://oauth2:{pat}@{host}/"  （host 取自 base_url）

async def _run_git(args: list[str], *, cwd, timeout=300) -> tuple[int, str, str]:
    # asyncio.create_subprocess_exec("git", *args)，参数数组、无 shell；
    # stdout/stderr 截断各 ≤4KB；PAT 替换为 glpat-**** 后才入库/入日志

async def _apply_repo_config(root, rewrite, base_prefix, agent_name, agent_email, pat) -> None:
    # git config --local url.<rewrite>.insteadOf <base_prefix>
    # git config --local --get-regexp '^url\..*\.insteadOf$' → 同 host 且非当前 key 的旧 PAT 残留键 --unset
    # git config --local user.name <agent_name>      （参数数组直传，防注入）
    # git config --local user.email <agent_email>

def _detect_mode(repo) -> str:
    # 仓库目录不存在/空/仅 .tmp 等白名单项 → "clone"
    # 有文件且无 .git → "adopt"；有 .git → "inject"

async def _inject_mode(repo, clone_url, ...) -> str | None:
    # git remote get-url origin 与 clone_url 比对：
    #   缺失 → remote add；漂移 → remote set-url；一致 → 不动
    # 然后 _apply_repo_config + rev-parse HEAD

async def _relocate_legacy(root, repo, pat) -> None:
    # v2 旧布局迁移：git clone --local --no-hardlinks <root> <repo>
    # 成功后删 root/.git（untracked 根文件原地保留）；
    # 失败 rmtree 半成品 repo 且不动 root/.git，raise

def _write_guide(root, repo_dir_name, project_path, default_branch, adopt_note) -> None:
    # GITLAB_GUIDE.md 写到 workspace 根：先 cd <repo_dir_name>（或 git -C）；
    # 根下其他文件不属于仓库

async def run_gitlab_workspace_init(agent_id, project_path, default_branch, pat) -> None:
    # async with _AGENT_INIT_LOCKS.setdefault(agent_id, asyncio.Lock()):
    #   0. _repo_dir_name 非法 → 直接 failed（不碰文件系统）
    #   1. init_status=initializing
    #   2. 分发（clone / adopt / inject，见 spec §7 全流程）；
    #      clone 且 root/.git 存在 → 先 _relocate_legacy 再走 inject
    #   3. 写 GITLAB_GUIDE.md 到 workspace 根（§9 内容模板）
    #   4. init_status=done + init_commit + repo_dir；异常 → failed + 脱敏错误 ≤500 字
```

**关键决策点**
- `agent_email = f"agent-{agent_id.hex[:8]}@clawith.local"`，`agent_name = agent.name`（任务内从 DB 读取）。
- 分支选择（clone 模式）：`git symbolic-ref refs/remotes/origin/HEAD` 取远程默认分支；`f_android_ai` 存在则 `checkout -b f_android_ai origin/f_android_ai`，否则 `checkout -b f_android_ai origin/<远程默认>` + `push -u origin f_android_ai`。
- adopt 模式：`git init -b <default_branch>` → remote add → 写 `.gitignore`（`.tmp/`、`__pycache__/`、`.DS_Store`）→ `add -A && commit "Initial commit"` → `push -u origin <default_branch>`；推完检查远端默认分支，无 main 则指南附提示。
- inject 模式：`remote get-url origin` 自愈（缺失 add / 漂移 set-url / 一致不动）+ `_apply_repo_config`（凭证+身份+旧 PAT 残留键清理），不碰工作树文件。
- 旧布局迁移：`git clone --local --no-hardlinks` 本地迁移（refs 全量保留、无硬链接依赖根 .git）；成功后删根 `.git`，untracked 根文件原地保留；失败删半成品仓库目录、根 `.git` 不动（可安全重试）。触发条件 = 仓库目录空且根有 `.git`。
- 失败语义：clone 失败删除本次 clone 的仓库目录；adopt 失败**不删除任何用户文件**（只回滚本次新建的 .git 元数据目录）；inject 失败仅记状态；迁移失败不动根 .git。
- 幂等/并发：锁内重复执行无害；`init_status=done` 后 PUT 仅更新凭证/绑定，不重跑（项目路径变化除外——此时新项目克隆到新的 `workspace/<新项目名>/`，旧仓库目录保留）。

## 3. 前端设计（UI）

### 3.1 位置与组件

- 新组件 `frontend/src/components/GitlabBinding.tsx`（与 `ChannelConfig.tsx` 同层，复用其 fetchAuth 风格与表单/卡片样式）
- 挂载：`frontend/src/pages/agent-detail/tabs/SettingsTab.tsx` L403 `<ChannelConfig .../>` 之后加 `<GitlabBinding agentId={agentId} canManage={canManage} />`（仅 edit 模式）

### 3.2 卡片布局（自上而下）

```
┌─ GitLab 绑定 ──────────────────────────── [状态徽标] ─┐
│  说明文案：绑定后自动拉取/初始化代码仓库（纯 git 方式） │
│  GitLab Token      [············] （password，autocomplete=new-password）│
│  项目路径          [liuyl/wwg1b]  （占位提示，支持子组）                  │
│  默认分支          [f_android_ai]                                        │
│  [保存]  [解绑（已绑定时显示）]                                          │
│  ── 状态 ──                                                            │
│  ● initializing：加载中…（转圈）                                         │
│  ● done：✓ 仓库就绪（分支 f_android_ai）                                │
│  ● failed：✗ <错误信息>  [重试]                                        │
│  token 已配置：仅显示「已配置（不显示明文）」+ 留空=保留旧 token 提示        │
└───────────────────────────────────────────────────────────────────┘
```

### 3.3 交互细节

- **保存**（PUT）：首次绑定 token 必填（前端同样校验）；已绑定时 token 留空 = 不改 token（提示文案）
- **改项目路径**：与当前值不同且已 `done` → 确认弹窗「项目路径变更需手动清空工作区后重新保存才会重新初始化」
- **解绑**（DELETE）：确认弹窗「解绑后仓库文件保留，但 agent 无法再推送」；成功后表单复位
- **重试**：failed 状态下点「重试」= 以当前值再 PUT
- **权限**：非 canManage 时表单只读
- **轮询**：保存后每 3s 轮询 GET（最多 2 分钟）刷新状态徽标，避免用户手动刷新

### 3.4 API 与 i18n

- `services/api.ts` 新增：
```ts
export const gitlabBindingApi = {
  get:  (agentId: string) => request<any>(`/agents/${agentId}/gitlab-binding`),
  put:  (agentId: string, data: any) => request(`/agents/${agentId}/gitlab-binding`, { method: 'PUT', body: JSON.stringify(data) }),
  del:  (agentId: string) => request<void>(`/agents/${agentId}/gitlab-binding`, { method: 'DELETE' }),
};
```
- i18n：`zh.json`/`en.json` 新增 `agent.settings.gitlab.*` 约 12 条（标题、字段标签、占位符、状态文案、确认弹窗、错误提示）

## 4. Constitution 逐条检查（C1–C6）

| 条 | 检查结论 | 依据 |
|---|---|---|
| C1 运行时边界 | ✅ 通过 | 绑定 CRUD 只写 `channel_configs`（产品记录）与工作区文件系统；初始化任务是独立后台任务，**不读写 checkpoint、不触碰 agent_run 执行状态机**；无执行控制循环 |
| C2 多租户 | ✅ 通过 | 所有端点 `check_agent_access`（agent 维度）；任务只写 `WORKSPACE_ROOT/<aid>/`；无跨租户查询；token 只属于绑定 agent |
| C3 幂等与对账 | ✅ 通过 | PUT 幂等（upsert + 锁 + 状态机）；done 后重复 PUT 只更新凭证；失败可重试；失败清理规则明确（clone 删半成品 / adopt 不删用户文件） |
| C4 客户端封装 | ✅ 通过 | 无新外部 client/SDK；git 走子进程参数数组（沙箱既有通道）；HTTP 仅直连内网 GitLab（内部服务），无新增网关 |
| C5 数据库 | ✅ 通过 | 复用 `channel_configs`（无新表、无 FK）；每请求 ≤2 次查询；无 N+1；枚举 ADD VALUE 为纯 DDL |
| C6 模块化 | ✅ 通过 | 新文件独立且小型（api <200 行、service <300 行、前端组件 <250 行）；不扩 24k 行 `agent_tools.py`；复用既有 `encrypt_data`/`check_agent_access`/`request()` 工具 |

## 5. 测试设计

- **单元 `tests/test_gitlab_workspace.py`**：`_detect_mode` 三态（含 .tmp 白名单）、`_repo_dir_name`（末段提取/子组/拒绝 `.` `..` `.git` `.tmp`/CJK 通过）、分支选择逻辑、凭证重写串格式、身份写入、`_apply_repo_config` 清 stale PAT 键（get-regexp 命中旧键 → unset 被调用）、`_inject_mode` origin 自愈三用例（漂移→set-url、缺失→add、一致→无操作）、`_relocate_legacy` 两用例（成功删根 .git + untracked 保留 / clone 失败保留 .git 且 raise 且清理半成品目录）、`_write_guide` 新签名（cd <repo_dir>/不入库文案）、失败清理规则、PAT 脱敏（mock `_run_git`）
- **单元 `tests/test_gitlab_binding_api.py`**：GET 不回 token、首次 PUT 无 token 422、权限 403、default_branch 非法 422、project_path 校验（完整 URL 拒绝、`.git/` 剥离归一、非法末段 `g/..`/`g/.git`/`g/.tmp` 拒绝、CJK 接受）、DELETE 语义、PUT 幂等
- **集成（手工，实施时执行）**：对 192.168.5.254 真实测试项目三条路径（clone/adopt/inject）+ 旧布局迁移 + inject origin 自愈 + 换 token 后 push + push options 建 MR（第 0 步已预验证可行）
- **前端**：SettingsTab 卡片渲染、状态徽标三态、确认弹窗、非 manage 只读

## 6. 部署与回滚

- 后端镜像重建即生效（无 compose 变更）；迁移随启动自动 `alembic upgrade head`
- **enum 迁移不可逆**：downgrade 为 no-op；回滚代码版本无需回滚该迁移（新值无害）
- 前端随前端镜像重建生效
- 验证清单：`/api/health` 200、alembic heads 单头、`scripts/arch-guard.sh` 通过、绑定三态手工验收

## 7. 文件清单

**新增**
- `backend/app/api/gitlab_binding.py`
- `backend/app/services/gitlab_workspace.py`
- `backend/alembic/versions/<ts>_add_gitlab_channel_type.py`
- `backend/tests/test_gitlab_workspace.py`、`backend/tests/test_gitlab_binding_api.py`
- `frontend/src/components/GitlabBinding.tsx`

**修改**
- `backend/app/config.py`（+`gitlab_base_url`）
- `backend/app/models/channel_config.py`（Python Enum 增加 `"gitlab"` 成员——SQLAlchemy Enum 默认校验写入值，不加会拒绝 channel_type='gitlab'）
- `backend/app/main.py`（注册 router）
- `frontend/src/services/api.ts`（+`gitlabBindingApi`）
- `frontend/src/pages/agent-detail/tabs/SettingsTab.tsx`（挂载组件）
- `frontend/src/i18n/zh.json`、`en.json`（词条）
