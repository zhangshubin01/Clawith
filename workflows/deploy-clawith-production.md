# Workflow：部署 Clawith 生产栈（含回滚）

> 状态：**草稿（第 1 版）**。待用户在文末的 3 个决策点上拍板后转正。

## Loop
每次改完代码、测试通过后，把 Clawith 后端/前端上线到测试 compose 栈，跑完验证、留好回滚后路。已重复 22+ 次，步骤固定，是全工作区最该委托出去的一件事。

## Trigger（事件触发）
用户（或上层 workflow）在对话里给出指令，二选一：
- `部署 <commit>` / `上线 <commit>` → 走「部署」分支。
- `回滚` / `回滚到 <commit>` → 走「回滚」分支。

> 不用 schedule 触发：部署是离散、由人决定的动作，不是固定节律。

## Checkpoint（唯一的人机决策点，push right 到最晚）
**「执行授权」** —— 把目标 commit + 预检结论整理成 brief 后，**只问一次**：
> 「即将部署 `{commit}`（`{title}`），预检已过（alembic 头一致 / 无并行会话脏文件 / 工作树无他人改动）。确认执行？」

人批准后才碰容器。除此之外**全程自动、不打断**。

## Steps（实现者可照做，无需再问）

### 0. 预检（不碰容器）
- 读记忆 `clawith-workspace-facts`：当前部署 commit、现存回滚标签、在途事项。
- `git log --oneline -5`：确认 HEAD 是否被并行会话推进（**提交前必查**）。
- `git status`：工作树有无他人脏文件；有则剥离、不提交、不部署。
- 核对 `alembic_version`（DB）与仓库迁移文件头一致——**不一致先停，报告，不构建**（否则新镜像 alembic 崩溃 Restarting）。
- 若后端代码改了：确认目标 commit 里关键值仍是你预期的（并行会话可能在你提交瞬间回退你的改动，提交前再 grep 一次）。

### 1. 干净检出
```
git worktree add /tmp/clawith-deploy-<commit> <commit>
```

### 2. 打回滚标签（**在替换镜像之前**）
```
docker tag clawith-agent-backend:latest clawith-agent-backend:pre-<新commit>-<旧镜像短sha>
```
旧镜像会被 GC，标签是回滚唯一把手；不打 = 回滚只能从旧 commit 重建。

### 3. 重建镜像（仅后端代码变更时）
build-arg 必须带，否则 PyPI 元数据截断（JSONDecodeError）：
```
CLAWITH_PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
CLAWITH_PIP_TRUSTED_HOST=pypi.tuna.tsinghua.edu.cn
```
纯 compose 改动（如 `init: true`）**不重建镜像**，`up -d` 即可。

### 4. 上线
```
docker compose --env-file <生产.env> -p clawith-agent -f <worktree>/docker-compose.yml up -d --no-deps backend
```
- `-p clawith-agent` 必带（否则 worktree 目录名成项目名、镜像名错）。

### 5. 验证（全项，缺一即报告失败，不宣称成功）
- `/api/health` = 200（路径是 `/api/health` 非 `/health`）；LAN IP 必须拒连（回环绑定）。
- 前端 3008 = 200。
- 容器内特征三件套：`upload_limits.py` 在 `api/`（非 `services/`）、`DB_RESERVED_CONNECTIONS`+`get_shared_checkpoint_pool` 在、`_bind_if_exists`×8 在、`_execute_code_legacy` 不在。
- `/app` 对 uid 1000 只读（`docker exec -u 1000`，root 可写不算）、`/data` 与 `/home/clawith` 可写；uvicorn 以 `clawith` 跑；`Privileged=false` + 3 caps。
- alembic 迁移成功；android `.image_version` 镜像=卷=15859902 无漂移。

### 6. 记录（写入记忆，不靠脑记）
- commit / worktree 路径 / 回滚标签 / 镜像短 sha。
- 若有 push 失败（Clash 代理 SSL_ERROR_SYSCALL），标注「本地 ahead N，待换节点重推」。

## 回滚分支
- `docker compose -f <旧worktree>/docker-compose.yml -p clawith-agent up -d --no-deps backend`（指向回滚标签）。
- 回滚标签 `clawith-agent-backend:pre-<commit>-<旧sha>` + 对应 `/tmp/clawith-deploy-*` worktree 一起才是完整回滚。

## 红线（不可违反）
- **无用户明确指令不碰任何容器**（不重启/不停止/不写库）。
- **`/tmp/clawith-deploy-*` worktree 是运行中容器的 bind-mount 源，绝不删除**；删了 daemon 会造空目录、挂载断裂。
- **部署前 `alembic_version` 必须与仓库头一致**，否则新镜像起不来。
- 并行会话可能在同时部署：动手前查 `git status` + 谁拥有 worktree。

## Brief（最终呈现给用户的形态，一行可读）
> ✅ 已部署 `{commit}` `{title}`，health/frontend/LAN/三件套/只读/迁移全部通过。回滚标签 `pre-{commit}-{旧sha}`，worktree `/tmp/clawith-deploy-{commit}`。⚠ 未推送（Clash 代理），本地 ahead 1。

---

## 待拍板的 3 个决策点（拍完即转正）
1. **Checkpoint 位置**：我推荐「执行前授权一次」就够；还是你希望「部署 + 验证完成后，再由你确认是否保留/回滚」？（推荐前者——红线已经挡住无指令部署）
2. **回滚触发**：回滚也走「你说『回滚』」这一个入口，还是要在验证失败时**自动**提示你「要回滚吗」？（推荐后者：验证失败 → 自动给回滚 brief）
3. **推送**：`git push` 因 Clash 代理经常失败，是否并入本 loop（失败就记「待重推」），还是你另起一个「推送到远端」的独立 loop？（推荐并入，只记不重试）
