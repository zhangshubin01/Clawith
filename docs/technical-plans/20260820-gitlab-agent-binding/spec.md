# Spec: GitLab Agent Binding（每 agent 一个 GitLab token + 一个项目，纯 git CLI 链路）

- 日期：2026-08-20
- 状态：已确认（2026-08-20，含评审修订 v2；**v3 布局修订已确认**——用户拍板改子目录布局，见 §7a）
- 关联决策：撤销 gitlab-mcp 网关（2026-08-20 用户拍板），改用纯 git CLI 路径；glab 不装。

## 1. 背景与目标

agent（如「Android 工程师」）需要在**内网 GitLab（http://192.168.5.254）**上完成：
1. 拉代码
2. 提交代码、提 MR
3. 切换/合并/新建分支

约束（用户明确）：
- 每个 agent 只能使用**一个 GitLab token**、绑定**一个 GitLab 项目**
- 一律走 git CLI（沙箱内 git 2.47.3 已确认可用），不依赖 MCP、不装 glab

## 2. 需求（10 项确认结果）

| # | 需求 | 决定 |
|---|---|---|
| 1 | 默认分支 | 绑定字段 `default_branch`，**默认值 `f_android_ai`**；clone 后若远程无此分支，从远程默认分支（main）创建并推送 |
| 2 | clone 时机 | 绑定保存时后台自动 clone |
| 3 | 仓库/clone 位置 | **仓库目录 = `/data/agents/<agent_id>/workspace/<项目名>/`**（v3 修订：workspace 根下还可能放**不入库**的其他文件，仓库不再占用根；沙箱内即 `/workspace/<项目名>`） |
| 4 | 项目路径格式 | 字符串直存，支持 `group/repo` 与子组 `group/subgroup/repo` |
| 5 | 提 MR 方式 | git push options：`-o merge_request.create -o merge_request.target=<main>`，写入 agent 指南 |
| 6 | main 防线 | GitLab 侧保护分支（用户侧操作，本 spec 附步骤说明） |
| 7 | 危险操作 | 约定禁 `push --force` 等；平台防线兜底 |
| 8 | 工作区已有内容 | 三态判定作用于 `workspace/<项目名>/`：目录空 → clone；**有代码且无 `.git` → adopt 模式**（init + remote + 首次提交推送，不覆盖任何文件）；已有 `.git` → **注入模式**（不动文件，重写凭证与提交人身份，并**自愈漂移的 origin URL**）；**旧布局（v2 根仓库）→ 本地 clone 迁移到子目录**（见 §7a） |
| 9 | token 权限 | GitLab 侧建议：PAT scope `read_repository`+`write_repository`，成员角色 Developer |
| 10 | 绑定可改 | 改 token 只更新凭证；改项目路径 → 新项目克隆到新的 `workspace/<新项目名>/`（旧仓库目录保留，需手动处理） |
| 11 | **提交人身份** | **固定 = agent 名字**：`user.name=<agent.name>`、`user.email=<agent.id 前 8 位>@clawith.local`，初始化时写入仓库本地 config（local 优先级高于全局，沙箱内提交必生效）；指南明确禁止 `--author`/`-c user.*` 覆盖 |

## 3. 范围

**做**：
- 后端：per-agent GitLab 绑定 CRUD（复用 `channel_configs` 表 + `channel_type='gitlab'` 枚举迁移）
- 后端：绑定保存触发的后台初始化任务（clone / adopt / 注入三态，含凭证注入、分支初始化、状态机、失败记录）
- 前端：agent 设置页「GitLab 绑定」表单（token / 项目路径 / 默认分支）+ clone 状态展示
- agent 指南：clone 完成后在工作区写入 `GITLAB_GUIDE.md`（分支约定、push options 提 MR、禁 force push）

**不做**：
- glab CLI、MCP 网关（已撤销）、GitLab REST API 集成
- 多项目/多 token 支持（一个 agent 一个绑定）
- GitLab 侧保护分支设置（用户手动，附指引）
- 提交前 diff 校验、MR 自动评审等

## 4. 方案概述

```
┌─ agent 设置页（前端）── PUT /agents/{id}/gitlab-binding ─┐
│   token + project_path + default_branch                    │
└────────────────────────────────────────────────────────────┘
                              │
                              ▼
        ChannelConfig(channel_type='gitlab')
        app_secret = 加密 PAT；extra_config = {project_path, default_branch,
                                                init_status, init_error, init_updated_at}
                              │ 触发后台任务（工作区初始化）                     │
                              ▼
        ┌──────────────── 初始化任务（backend 容器内） ────────────────────┐
        │ 目录名 = project_path 末段（安全校验，非法直接 failed）           │
        │ 仓库目录 = /data/agents/<aid>/workspace/<项目名>/                │
        │ 三态判定：                                                        │
        │ A. 仓库目录空 → clone 模式：                                      │
        │    git clone https://192.168.5.254/<project>.git                 │
        │    → /data/agents/<aid>/workspace/<项目名>/                      │
        │ B. 有代码无 .git → adopt 模式：                                   │
        │    git init -b <default_branch> 于仓库目录                        │
        │    git remote add origin ...；写入 .gitignore 模板；              │
        │    git add -A && commit && push -u origin <default_branch>       │
        │ C. 已有 .git → 注入模式：重写凭证 insteadOf + 提交人身份；         │
        │    origin 缺失/漂移 → remote add / set-url 自愈                  │
        │ D. 旧布局迁移：仓库目录空但 workspace 根有 .git（v2 遗留）→        │
        │    git clone --local --no-hardlinks 根 仓库目录 → 删根 .git；     │
        │    untracked 根文件原地保留（不入库）                             │
        │ 共同：本地 config 注入 insteadOf 凭证 + user.name/user.email；    │
        │       clone 模式分支初始化；GITLAB_GUIDE.md 写到 workspace 根      │
        └───────────────────────────────────────────────────────────────────┘
                              │ 之后
                              ▼
        agent 在沙箱（/workspace 挂载）里进入 /workspace/<项目名> 跑 git：
        pull/commit/branch/merge/push
        提 MR：git push origin f_android_ai -o merge_request.create -o merge_request.target=main
```

关键机制说明：
- **凭证注入**：初始化时把 `insteadOf` 重写写入**仓库目录的本地 config**（`workspace/<项目名>/.git/config`，不落全局），随工作区卷持久化；沙箱内 /workspace 挂载同一目录，git 天然可用。换 token 时重写该配置，并清掉指向同一 host 的旧 PAT 残留键（token 轮换不留死凭证）。
- **网络**：backend 容器 → 192.168.5.254 已实测可达；bwrap 沙箱不隔离网络。
- **状态机**：`init_status ∈ {pending, initializing, done, failed}`，GET 绑定返回状态与错误，前端展示。

## 5. 数据模型

复用 `channel_configs`（`app/models/channel_config.py`），**需 Alembic 迁移**：`ALTER TYPE channel_type_enum ADD VALUE 'gitlab'`（事务内执行，见 `backend/alembic/AGENTS.md` 约定）。

| 字段 | 用途 |
|---|---|
| `channel_type='gitlab'` | 绑定类型 |
| `app_secret` | 加密的 PAT（复用 `encrypt_data`/`decrypt_data`） |
| `extra_config.project_path` | 如 `liuyl/wwg1b` |
| `extra_config.default_branch` | 默认 `f_android_ai` |
| `extra_config.init_status / init_error / init_updated_at / init_commit` | 状态机与审计 |
| `is_configured` | 是否已填 token |

GitLab 地址统一走后端配置 `GITLAB_BASE_URL`（默认 `http://192.168.5.254`，可被环境变量覆盖），不存每绑定。

## 6. API 设计（`app/api/gitlab_binding.py`，router 前缀 `/agents/{agent_id}/gitlab-binding`）

- `GET /` — 返回：`{configured, project_path, default_branch, has_token, init_status, init_error, init_updated_at}`（**不回 token**）
- `PUT /` — body `{token?: str, project_path: str, default_branch?: str}`；upsert 绑定、加密 token、触发初始化任务；**权限**：同 `agent_credentials` 的 manage 级（复用 `check_agent_access`），非 manage 且非平台/组织管理员 → 403
- `DELETE /` — 解绑：置 `is_configured=False`、清 `app_secret`、保留 `extra_config`（供重绑）；若仓库 `.git/config` 有本绑定的 insteadOf 凭证行则 `git config --unset` 移除——按候选路径列表逐一清理（先 `workspace/<项目名>/`，再旧布局的 workspace 根，兼容 v2 存量）。**仓库文件与 .git 不动**（agent 可继续本地工作，重新绑定前不可推送）。权限同上。
- token 语义：**首次绑定时必填**（空则 422）；已有绑定时空 = 保留旧 token（只改项目/分支不必重传）

校验：project_path 非空、无空白、**拒绝完整 URL（`://`）**、容忍末尾 `.git`（规范化剥离）；**末段（= 子目录名）必须匹配 `[\w.-]+`（UNICODE，CJK 允许）且不能是 `.`/`..`/`.git`/`.tmp`**；default_branch 合法分支名（正则 `[A-Za-z0-9_\-./]+`）；token 长度 ≤ 100。

## 7. 工作区初始化任务（`app/services/gitlab_workspace.py`，clone / adopt / inject + 旧布局迁移）

输入：agent_id、project_path、default_branch、PAT（明文，仅任务内使用，不落日志）。

仓库目录固定为 `WORKSPACE_ROOT/<aid>/workspace/<项目名>/`（沙箱内 `/workspace/<项目名>`），其中 `<项目名>` = project_path 的末段，由 `_repo_dir_name()` 做安全校验（匹配 `[\w.-]+` UNICODE、拒绝 `.`/`..`/`.git`/`.tmp`；非法直接记 failed，不碰文件系统）。workspace 根下可放其他不入库文件。

**流程分支**：
- **D. 旧布局迁移（优先于 clone）**：仓库目录判定为「clone」（空）但 workspace 根存在 `.git`（v2 遗留：仓库根=工作区根）→ `git clone --local --no-hardlinks <根> <仓库目录>` 全量迁移 refs，成功后删除根 `.git`（untracked 根文件原地保留、不入库），随后走 C 注入。clone 失败则删除半成品仓库目录、**不动根 `.git`**，记 failed。
- **A. clone 模式**（仓库目录不存在、为空、或仅含沙箱临时项）：
  1. 状态置 `initializing`；`git clone https://<project>.git <仓库目录>`（若 clone 需认证：`git -c url.<rewrite>.insteadOf=... clone` 一次性注入，不落文件）
  2. 写仓库本地 config：`insteadOf` 凭证重写、`user.name=<agent 名>`、`user.email=agent-<id>@clawith.local`
  3. 确定远程默认分支（`refs/remotes/origin/HEAD`）
  4. 远端有 `default_branch` → `git checkout -b <db> origin/<db>`；否则 `git checkout -b <db> origin/<remote_default>` + `git push -u origin <db>`
- **B. adopt 模式**（仓库目录有代码、无 `.git`；**不覆盖任何文件**）：
  1. 仓库目录 `git init -b <default_branch>`
  2. `git remote add origin https://<project>.git`；写本地 config（insteadOf 凭证 + 身份，并清同 host 旧 PAT 残留键）
  3. 写 `.gitignore` 模板（`.tmp/`、`__pycache__/`、`.DS_Store` 等）
  4. `git add -A && git commit -m "Initial commit"`；`git push -u origin <default_branch>`
  5. 推送后检查远端是否有 `main`（远程默认分支）：**没有 → 状态仍 `done`，但在 GITLAB_GUIDE.md 附加提示**（「请在 GitLab 初始化 README 或补推 main，MR 目标依赖它」）
- **C. 注入模式**（仓库目录已有 `.git`）：不动文件，**重写本地 config 的 insteadOf 凭证 + 提交人身份**（并清同 host 旧 PAT 残留键）；**自愈 origin**——`git remote get-url origin` 与期望 clone URL 比对：缺失 → `remote add`，漂移（如早期绑定时误填完整 URL 造成错误 remote）→ `remote set-url`，一致 → 不动。状态 `done`。

共同收尾：写 `GITLAB_GUIDE.md` 到 **workspace 根**（内容指向子目录，见 §9）；状态 `done`（记录 init_commit + repo_dir 入 extra_config）；任何失败 → `failed` + 截断错误（≤500 字，token 脱敏为 `glpat-****`）；clone 失败删除本次 clone 的仓库目录，adopt 失败只回滚本次新建的 `.git` 元数据（不删用户文件），迁移失败不动根 `.git`。

**提交人身份（硬性固定）**：A/B 两模式都在初始化时写入仓库本地 config（优先级高于任何全局配置）：
- `user.name = agent.name`（agent 名字，固定）
- `user.email = <agent.id 前 8 位>@clawith.local`
- 沙箱内 `git commit` 自动使用该身份；指南禁止 `--author`/`-c user.name=...` 覆盖。C 态（已有 .git）时校验本地 config 身份，不一致则重写。

并发与幂等：同一 agent 一把 asyncio.Lock（重复 PUT 排队/合并）；init 完成后 PUT 只更新凭证与绑定字段，不重复初始化（除非项目路径变化）。

**实现约束（防注入）**：所有 git 命令一律子进程**参数数组**直传（无 shell 拼接）；`user.name` 等取自 agent 名字的值不参与字符串拼命令（用 `git config --local user.name <值>` 的参数形式写入）。

## 7a. v3 布局修订说明（workspace 根 → workspace/<项目名>/）

用户拍板：旧 v2 设计「工作区根 = 仓库根」改为**子目录布局**——workspace 根下还要放其他（不入库）文件。要点：

1. **仓库目录** = `workspace/<project_path 末段>/`；沙箱挂载点不变（`/workspace` 仍挂 workspace 根，仓库路径变为 `/workspace/<项目名>`），挂载无需任何改动。
2. **inject 自愈（fix 2 落地）**：此前「项目路径误填完整 URL」事故会在本地产生漂移 origin（`.git.git` 双重拼接类问题）；注入模式现在比对 `git remote get-url origin` 与期望 clone URL——缺失则 `remote add`、漂移则 `remote set-url`、一致则不动。重新保存绑定即自愈，无需人工干预。
3. **旧布局自动迁移**：v2 存量（仓库根 = workspace 根）在重新保存绑定时被 `git clone --local --no-hardlinks` 迁移到子目录，随后删根 `.git`；根下 untracked 文件原地保留（新布局下它们本就该在仓库外）。迁移失败删除半成品仓库目录、**不动根 `.git`**，可安全重试。
4. **防注入加固**：project_path 校验拒绝完整 URL（`://`）、剥离末尾 `.git` 后缀、末段（子目录名）安全校验（正则 `[\w.-]+` UNICODE + 拒绝 `.`/`..`/`.git`/`.tmp`，CJK 允许）；服务层 `_repo_dir_name()` 同规则兜底，非法路径直接记 failed、不碰文件系统。
5. **凭证卫生**：`_apply_repo_config` 清理指向同一 host 的旧 PAT 残留键（token 轮换不留死凭证）；DELETE 按候选路径列表（先子目录、后旧根）清理 insteadOf。
6. 指南改写到 **workspace 根**，内容引导 agent 先 `cd <项目名>`（或 `git -C`），并声明根下其他文件不属于仓库。

## 8. 前端（agent 设置页，参考 `ChannelConfig.tsx` 既有模式）

表单：`GitLab Token（password 输入）`、`项目路径（如 liuyl/wwg1b）`、`默认分支（默认 f_android_ai）`；保存按钮调用 PUT。
状态区：init_status 徽标（初始化中 / 成功 / 失败+错误+「重试」按钮=再 PUT）、token 已配置提示（不显示明文）、清空工作区提示文案。
改项目路径时弹确认：「项目路径变更后新项目会克隆到 workspace/<新项目名>/，旧仓库目录保留（需手动处理）」。

## 9. agent 指南（`GITLAB_GUIDE.md`，初始化完成后自动写入 **workspace 根**，内容指向仓库目录）

- **仓库目录是 `workspace/<项目名>/`**（沙箱内 `/workspace/<项目名>`），git 操作先 `cd <项目名>`（或 `git -C <项目名>`）；**workspace 根下的其他文件不属于仓库**，git 不会跟踪
- 日常：`git pull` 同步；改动后 `git add -A && git commit -m "..."`；推送到 `f_android_ai`
- 提 MR：`git push origin f_android_ai -o merge_request.create -o merge_request.target=main -o merge_request.title="..."`（已存在 MR 则更新）
- 分支：本地开发分支随意建/切/合（`git merge` 合进 `f_android_ai`）；**main 只能经 MR 进入，绝不直接 push**
- 禁止：`push --force`、`git push origin main`、reset 远程共享分支
- **提交身份固定为本 agent**：不得用 `--author` 或 `-c user.name=` 覆盖提交人
- 项目身份：本 agent 只操作 `<project_path>`，token 权限仅限该项目
- adopt 模式特有提示（若远端无 main）：先让管理员在 GitLab 初始化 README 或补推 main，再提 MR

## 10. 安全与合规

- **C2 多租户**：绑定挂在 agent 上，API 经 `check_agent_access`；后台任务只写 `WORKSPACE_ROOT/<aid>/`，天然隔离。
- token：仅 `ChannelConfig.app_secret` 加密落库；clone 任务的明文 PAT 仅存在于任务局部变量；错误日志不打印 token（脱敏 `glpat-****`）。
- 仓库内 `.git/config` 含 insteadOf 凭证（明文）：仅 agent 工作区卷可见，随 agent 数据隔离；换 token 时重写；解绑（DELETE）时移除该配置。
- **项目隔离信任模型（明确声明）**：Clawith 侧**不做运行时项目校验**——agent 理论上可读取 `.git/config` 中的 token 并尝试访问其它项目。唯一硬边界 = **PAT 账号的 GitLab 成员权限必须是「项目级」**（严禁组级授权）；越权访问由 GitLab 权限模型兜底。此模型已与用户确认接受。
- **C1/C3**：初始化任务是幂等后台任务，不触碰运行检查点状态；失败可重试，不改产品执行状态机。

## 11. 验收标准

1. PUT 绑定（token+项目）→ GET 显示 `initializing` → 完成后 `done`；空工作区场景：clone 到 `workspace/<项目名>/` 且分支为 `f_android_ai`
2. 远程无 `f_android_ai` 时自动从远程默认分支创建并推送到远端
3. **adopt 场景**：`workspace/<项目名>/` 已有代码无 `.git` → init + 首次提交推送成功，**原有文件一个不丢**；`.gitignore` 模板已写入
4. **已有 `.git`（注入模式）**：不动文件与远程，凭证与提交人身份已注入；origin 漂移（误填 URL 的历史遗留）被 `remote set-url` 自愈；`git push` 可用
5. **旧布局迁移**：v2 存量（根有 `.git`）重新保存绑定 → 仓库迁移到 `workspace/<项目名>/`、根 `.git` 删除、untracked 根文件保留；失败时根 `.git` 不动、重试可成功
6. 沙箱内 `git pull/push` 免交互成功（凭证注入生效）；`git commit` 的提交人 = agent 名字（`git config user.name` / `git log -1` 验证），且 C 态已有仓库的身份被重写为 agent 名
7. 提 MR：按指南的 push options 命令，GitLab 上出现 MR（target=main）
8. token 换新后 `git push` 仍成功（insteadOf 已重写，同 host 旧 PAT 残留键被清除）；GET 不回 token、has_token=true
9. 校验：完整 URL、`g/..`、`g/.git`、`g/.tmp` 等非法 project_path → 422；`g/repo.git` 归一为 `g/repo`；CJK 项目名可保存
10. 权限：非 manage 用户 PUT → 403
11. `scripts/arch-guard.sh` 与后端测试通过

## 12. 测试计划

- 单元：绑定 CRUD 校验（URL 拒绝/.git 剥离/末段安全/CJK）、三态判定、状态机转换、分支选择逻辑、inject origin 自愈（add/set-url/无操作）、旧布局迁移（成功删根 .git/失败保留）、stale PAT 键清理、目录名安全校验（mock git 子进程输出）
- 集成：对 192.168.5.254 上真实测试项目跑三条路径（空→clone、有代码→adopt、有 .git→注入）+ 旧布局迁移 + 凭证重写 + 换 token 后 push
- 前端：表单提交、状态徽标、错误展示

## 13. 实施第 0 步（风险验证）与用户侧待办

**实施第 0 步（风险验证）——✅ 已实测通过（2026-08-26）**：内网 GitLab 版本 **16.3.2-ee**（远超 push options 所需 11.10+/13.12+）。实测：临时项目上 `git push` 带 `-o merge_request.create -o merge_request.target=main -o merge_request.title=...` 成功创建 MR（`f_android_ai → main`，作者=token 身份），测试项目已删除。**退化方案不需要。** 顺带验证了另一种凭证注入方式 `git -c http.extraheader="PRIVATE-TOKEN: <PAT>"` 可用（design 阶段作为 insteadOf 的备选/测试手段）。

用户侧待办：
1. GitLab 建每 agent 专用 PAT（scope：`read_repository`+`write_repository`）
2. 将 PAT 账号以 Developer 角色加入对应项目——**必须是「项目级成员」，严禁组级授权**（组级会破坏「一个 agent 一个项目」的隔离边界）
3. 项目 main 分支开保护（仅 Maintainer 可推、合并需 MR）——提供操作步骤
4. 确认项目设置里「Allowed to force push」保持**关闭**（默认关闭；它比提示词约束可靠）
