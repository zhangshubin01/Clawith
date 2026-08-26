# ADR 0003: 部署锁与部署注册表（多会话部署避让）

- **状态**: Accepted
- **日期**: 2026-08-26
- **决策者**: 用户（访谈逐条确认）+ 本会话

## 背景

同一宿主、同一仓库、同一 compose project 上常有多个 agent 会话并行工作，各自随时可能执行部署/回滚。历史事故三类（均见 workspace memory `clawith-workspace-facts` 部署记录）：

- **A. 同时部署/构建竞争**：ef8fa7c4 部署期间并行会话同时部署，构建被竞争中止；418f6c16 部署时对方刚重建过容器、旧镜像已被 prune，回滚标签打空。
- **B. 部署内容与分支 tip 错位**：镜像在 T0 构建，对方在 T0+4min 提交并推送新 commit——运行镜像落后分支 tip（53882564 事件）；反向为部署把对方未提交/坏态 compose 卷入。
- **C. 提交窗口 index 竞态**：对方在「`git add` 与 `git commit` 之间」stage 文件被误提交（两起）。

## 决策

| # | 决策点 | 结论 | 理由 |
|---|---|---|---|
| 1 | 机制形态 | **部署锁 + 部署注册表两件套** | 锁解决 A；注册表解决 B（C 无法机制化，见 #8） |
| 2 | 锁粒度 | **全局一把锁**（backend/frontend 部署互斥） | 同一 compose project；事故全是「双方同时动整栈」，分锁收益≈0 |
| 3 | 锁机制 | **Python `fcntl.flock` 内核锁** | macOS 无 `flock(1)`、`shlock` 进程死后留陈旧锁；fcntl 是唯一「部署进程被杀/会话中止自动释放」的选项，零外部依赖 |
| 4 | 锁与注册表位置 | **仓库内 `.clawith-deploy/`**（.gitignore 新增） | 双会话锚定同一仓库路径；随仓库走，不依赖宿主用户目录 |
| 5 | 撞锁行为 | **默认排队 600s**（`CLAWITH_DEPLOY_LOCK_TIMEOUT` 可覆盖），`--no-wait` fail-fast | 双会话频繁时「等一会儿就能部署」优于「失败让人重来」；无人值守场景用 --no-wait |
| 6 | tip vs 运行检查 | **默认提示并继续**，`--strict` 阻塞 | 展示 `git log <上次部署>..<目标>`；测试栈双会话常在，默认阻塞会频繁卡住；提示已让部署者看清带上/落下什么 |
| 7 | 覆盖面 | **deploy.sh + restart.sh docker 分支 + 回滚流程**都接锁 | restart.sh docker 分支同为 `compose up -d --build` 部署路径（且含 `down`）；回滚=镜像替换同样排他 |
| 8 | 提交窗口竞态 | **仅固化协议**，不上机制 | 任何 wrapper/hook 都跑在「对方已 stage」之后，机制防不住；协议=提交前 `git diff --cached --stat` 复核 + 仅用 pathspec 提交本任务文件 |
| 9 | 纸迹 | ADR 本件 + CONTEXT.md 术语 + skill clawith-prod-deploy 更新 | grill-with-docs 承诺：决策进 ADR、术语进 CONTEXT.md、流程进 skill |

## 实现形态

- `scripts/deploy_guard.py`（stdlib only，~150 行）：
  - `lock <state_dir> <timeout_seconds> <commit> <scope> -- <cmd...>`：`fcntl.flock` 排他锁（0.2s 轮询直至超时，超时 exit 9 + stderr 提示；timeout=0 即 --no-wait）→ 写注册表 `active` 条目 → 子进程跑 cmd（SIGTERM/SIGINT 转发）→ 结束后清 `active`、按成功/失败追加 `last_deploys`（保留 20 条）→ 以子进程退出码退出。锁在进程死亡时由内核自动释放。
  - `check <state_dir> <target_commit>`：取注册表最近一次部署 commit，`git log --oneline <baseline>..<target>` 展示；有未部署提交 exit 1（供 `--strict` 中止），否则 exit 0。无基线时提示并 exit 0。
- 注册表 JSON：`{"active": {pid, started_at, target_commit, scope} | null, "last_deploys": [{at, commit, image_sha, scope, success}]}`。
- `deploy.sh`：解析参数后若未处于锁定态，则 `exec python3 scripts/deploy_guard.py lock ... -- "$0" "$@"` 整段重入；新增 `--no-wait` / `--strict` 标志；成功后镜像 sha 由 deploy.sh 写入 marker 供注册表记录。
- `restart.sh` docker 分支：同样以 guard 锁包裹（轻量：等待 600s、不 strict）。
- 回滚：skill 中固化为「回滚命令一律在 guard 锁下执行」的一行式。

## 后果

- 正：并发部署被串行化；部署者可见「自己在带上/落下哪些提交」；进程死亡不留陈旧锁。
- 负：部署需排队至多 10 分钟（可 --no-wait）；`.clawith-deploy/` 是本地状态，换机后注册表基线为空（check 退化为提示）。
- 中性：锁文件/注册表不进 git（gitignored），多机共享同一仓库目录时锁仅覆盖本机（本部署形态为单宿主，可接受）。
