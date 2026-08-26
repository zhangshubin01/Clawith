# Tasks: GitLab Agent Binding

- 依据：spec.md v2 + design.md（均已确认）
- 分支：`feat/gitlab-agent-binding`
- 每波完成后跑相关测试；后端收尾跑 `scripts/arch-guard.sh` + 全量 pytest

## Wave 0 — 基础设施（无依赖，可并行）

| # | 任务 | 文件 |
|---|---|---|
| T0.1 | Settings 加 `gitlab_base_url`（默认 http://192.168.5.254） | `backend/app/config.py` |
| T0.2 | ChannelConfig Python Enum 加 `"gitlab"` 成员 | `backend/app/models/channel_config.py` |
| T0.3 | Alembic 迁移：`ALTER TYPE channel_type_enum ADD VALUE IF NOT EXISTS 'gitlab'`（autogenerate 取单头，验证 `alembic heads` 单行） | `backend/alembic/versions/<ts>_*.py` |

## Wave 1 — 服务层（依赖 T0.1/T0.2）

| # | 任务 | 文件 |
|---|---|---|
| T1.1 | `gitlab_workspace.py`：`_repo_root`/`_credential_rewrite`/`_detect_mode`/`_run_git`（子进程参数数组 + 脱敏） | 新增 |
| T1.2 | 三态实现：clone（分支初始化）/ adopt（init+push+main 检查）/ inject（凭证+身份）+ GITLAB_GUIDE.md + per-agent 锁 + 状态机写 ChannelConfig | 同上 |
| T1.3 | 单测：三态判定、分支选择、凭证串、身份、脱敏、失败清理（mock `_run_git`） | `backend/tests/test_gitlab_workspace.py` |

## Wave 2 — API 层（依赖 T1.*、T0.3）

| # | 任务 | 文件 |
|---|---|---|
| T2.1 | `gitlab_binding.py`：GET（不回 token）/ PUT（首次 token 必填、幂等、触发任务）/ DELETE（解绑留仓库） | 新增 |
| T2.2 | `main.py` 注册 router | 修改 |
| T2.3 | 单测：权限 403、校验 422、不回 token、DELETE 语义 | `backend/tests/test_gitlab_binding_api.py` |
| T2.4 | `scripts/arch-guard.sh` + 全量 pytest 通过 | — |

## Wave 3 — 前端（依赖 T2.*）

| # | 任务 | 文件 |
|---|---|---|
| T3.1 | `gitlabBindingApi`（get/put/del） | `frontend/src/services/api.ts` |
| T3.2 | `GitlabBinding.tsx` 组件（表单/状态徽标/轮询/确认弹窗/只读态） | 新增 |
| T3.3 | SettingsTab 挂载 + i18n 中英词条 | `SettingsTab.tsx`、`zh.json`、`en.json` |

## Wave 4 — 集成验证（依赖全部）

| # | 任务 |
|---|---|
| T4.1 | 真实 GitLab（192.168.5.254）三路径验证：空→clone（f_android_ai 自动创建）、有代码→adopt、有 .git→inject；换 token 后 push；push options 建 MR |
| T4.2 | 前端手工验收（三态徽标、解绑、确认弹窗） |
| T4.3 | `/code-review --base <主分支>` 收尾 |
