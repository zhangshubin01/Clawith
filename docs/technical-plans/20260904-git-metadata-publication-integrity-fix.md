# Git 元数据发布完整性修复方案（execute_code git 一直不通）

- 日期：2026-09-04
- 状态：P0-1 埋点已上线并拿到证据（`511b1a20` 已部署，5 次 `SandboxPublicationUnhandled` 命中，根因二已坐实为 `.git/objects/` 下 19 个 root 属主历史目录）；P0-2 待授权、P1/P2 待拍板。**2026-09-04 多角度评审已复核：根因/方向正确，改动清单 3 处实质缺口已补齐（§3.5）；P1 由「方案 A 唯一」扩为「方案 A vs 方案 B′ 双候选」，正式权衡见 §3.4，默认倾向 B′（git bundle 单 blob 原子发布，保留本地 commit 历史）。2026-09-04 已按授权实施 P1 方案 B′：代码落地 + 单测全绿 + arch-guard/ruff 通过（实现记录见 §3.6）；随后 P2 远程 restore 兜底亦已实现（§3.6）；仅迁移清理 storage 历史 `.git` 条目留待授权**
- 分支：`f-shubin-0806`，HEAD `511b1a20`（`fix(observability): 埋点日志占位符 %s→{} 修正 loguru 格式化`；上一跳 `6dacf86b` 埋点初版、`369cc4a9` 为埋点前基线）
- 影响面：Clawith 平台内部 agent（`Android 工程师 07`, agent_id=`950a1943-6ad6-4139-842e-8bde89ca823c`）通过 `execute_code`（language=bash）执行 `git commit/push/MR` 到内网 GitLab `http://192.168.5.254/zhangshubin/mydome1` 持续失败。
- 关联文档：`20260903-workspace-conflict-observability-hardening.md`（观测加固，B/C2 已落地）、`20260901-sandbox-git-materialize-fix.md`、`20260901-workspace-publication-p0-fix.md`（前序 .git 三代修复史）。

---

## 1. 问题陈述

平台内部 agent 反复执行 `execute_code` git 提交/推送流程，表现为三类现象：

1. 沙箱返回平台级 `PermissionError`（`outcome=unknown`，工具明令"不要重试代码"）——07:17–07:23 连续 4 次 `sandbox_execution_unverifiable`，`retryable=false`。
2. 07:49–07:51 转成 `sandbox_execution_failed`，git 报 `HEAD tree chain BROKEN - cannot rebuild index from HEAD` / `fatal: Not a valid object name aa/ba6d554c...`。
3. 09-02 历史 run 有 `workspace_sync_conflict`，冲突路径全是 `.git/` 文件（`COMMIT_EDITMSG`/`index`/`logs/HEAD`/`objects/aa/ba6d554...`）。

本地 shell git 已排除（用户澄清为平台内部链路，非宿主 shell）。

---

## 2. 根因分析（三根因，证据充分）

> **根因主次判定（2026-09-04 评审修订）**：三根因分两层——
> - **直接根因（本次 git 不通的触发点）= 根因二**（PermissionError，属主漂移）。5 次 `SandboxPublicationUnhandled` 全部 `error_type=PermissionError`，traceback 核心帧确认抛出点在 **reconciliation 路径**（`recover_publication` → `workspace_reconciliation.apply_candidate` → `write_bytes_if_match` → `_atomic_write_bytes` 写 `.git/objects/7f/.clawith-storage-tmp-*` EACCES），无一次是「残缺 `.git`」报错。
> - **结构根因（为什么 `.git` 反复坏）= 根因一**（`.git` 逐文件 CAS 非原子）。逻辑成立，但证据间接（storage 172 loose objects vs staging 29+1 pack），**不是本次失败的触发点**。
> - 根因三（吞异常）为可观测性盲区。
>
> 结论：**止血（P0-2 chown）即可恢复 git**；方案 A 针对结构根因一，属可选重构，非止血必需。

### 根因一（结构根因）：`.git` 被当作 CAS 逐文件发布，中途冲突/GC 造成存储层仓库损坏

git object 库是 Merkle DAG，任何"逐文件、非原子"的发布语义都会在部分写入时破坏链完整性。当前实现正是逐文件发布：

- `workspace_policy.py:55` `classify_publish_path` 对 `.git` 返回 `"source"`；`DERIVED_SEGMENTS`（`workspace_policy.py:22`）**刻意**不含 `.git`（注释 18–21 明写 "source-grade"），于是 `.git` 进入 CAS 逐文件通道。
- `_collect_temp_workspace_files`（`agent_tools.py:2608`，`classify_and_put` 2626–2633）把每个 `.git` 文件收进 `cas_files`。
- `flush_temp_workspace`（`agent_tools.py:2028`）的 CAS 循环（2095–2203）对每个 `.git` 文件**独立**执行 `write_bytes_if_match`（带 `version_token` / `require_absent` 条件，2102–2124）。冲突时 `conflict_mode=="fail"` 立即 `return`（2184–2193）。
- **缺陷**：该循环是顺序提交的，冲突发生前已写入的 `.git` 文件**已经落库**，冲突后的文件未写入 → 存储层得到**残缺 `.git`**。下一次 materialize 得到的 `.git` 缺 object → git 报 `HEAD tree chain BROKEN` / `Not a valid object name`。
- 决定性证据：存储层 `.git` 有 **172 个 loose objects（`git status` 健康）**，而当前 live staging `.git`（`/data/agents/.sandbox-staging/2a66cd9f-bfd96619/workspace/mydome1/.git`）只有 **29 个 objects + 1 个 pack**，缺 143 个 object。pack 的出现说明 staging 里发生过 `git gc`/repack：flush 会新增 pack、同时经 delete pass（2205–2285）删除 manifest 中已不存在于 staging 的 loose object；一旦中途冲突，存储层就是"部分旧 loose + 部分新 pack"的混合态。

补充：materialize 侧的 `_drop_incomplete_git_dirs`（`agent_tools.py:1804`）只保证"budget 截断时 `.git` 要么完整要么删除"（且会 purge manifest，1831–1833），**不防 flush CAS 中途丢弃**——这是 4d3fe431 根治后仍复现的残留根因。

### 根因二（次根因，已埋点坐实）：`.git/objects/` 下 19 个 root 属主历史目录 + backend uid=1000 → `PermissionError`

- **坐实证据（`511b1a20` 埋点命中 5 次）**：run `5df4bab8`×4 + run `3b8cab45`×1，全部 `error_type=PermissionError`、`execution_started=True`。精确抛出点（traceback 核心帧）：

  ```
  PermissionError: [Errno 13] Permission denied:
  '/data/agents/950a1943.../workspace/mydome1/.git/objects/7f/.clawith-storage-tmp-8dcc0b3f...'
  调用链: agent_tools.py:5540 execute_builtin_tool_outcome
    → agent_tools.py:3344 → agent_tools.py:3190 recover_publication
    → workspace_reconciliation.py:333 apply_candidate → write_bytes_if_match
    → storage_runtime/local.py:187 → 244 _run_sync_mutation → 261 _atomic_write_bytes（抛错点）
  ```

- **真实根因（推翻原"build/ 目录 root 属主"假设）**：`.git/objects/` 下存在 **19 个 root(0:0) 属主的 object 子目录**（`06/36/48/55/5d/5f/61/68/69/6e/7d/7f/84/9b/a6/d0/e1/f8/fe`，全部 **Sep 2 14:26** 历史遗留，root 进程运行的 git 操作创建，目录内 loose object 文件同样 `-r--r--r-- 0 0`）；另有 **10 个 root 属主 loose object 文件**散落在 1000 属主目录内（`10/2a/88/a8/fb/d2/4b/df/90`）。合计 `.git` 内 root 属主文件 **31 个**（`find .git -type f -uid 0` 实核）。
- 当前（09-04）object 目录（`0c/21/2d/31/...`，Sep 4 07:17）都是 1000:1000 —— 说明当前沙箱 git 以 uid 1000 跑，但 reconciliation 阶段 backend（clawith uid 1000，uvicorn 非 root）要往这些历史 root 目录（`drwxr-xr-x 0 0`，仅 root 可写）写 `.clawith-storage-tmp-*` 临时文件 → `EACCES`。
- 背景：backend 容器 PID1=root，但 uvicorn 实际以 `clawith(uid 1000, groups 含 0)` 运行；沙箱容器 user 由 `docker_backend.py:274/521` 固定为 `_CONTAINER_UID_GID`(1000)。Sep 2 14:26 这批 root 属主目录来自更早的 root 运行路径（历史 git 操作），与当前 1000 运行路径属主分叉。

### 根因三（可观测性盲区）：`agent_tools.py:3390` 的 `except Exception` 无 `logger.exception`

```python
# agent_tools.py:3390-3399（已 read_file 核对）
except Exception as exc:
    if execution_started:
        return _typed_workspace_publication_failure(
            f"Sandbox execution outcome is unknown after {type(exc).__name__}.",
            "sandbox_execution_unverifiable",
        )
    return _typed_failure(...)
```

4 次生产 `PermissionError` 期间（07:16–07:24）`docker logs` **无任何 ERROR/traceback**，失败前仅一条 `[Sandbox] Executing code with backend: DockerSessionBackend...`（13325），之后零落地日志——异常被 3390 静默吞掉。这正是"日志里什么都看不到"的直接原因。

---

## 3. 修复方案

分 P0（止血+埋点，低风险、可立即做）、P1（根治 `.git` 发布完整性）、P2（跨 run 恢复兜底）。

### P0-1 埋点：3390 `except` 加 `logger.exception`（含上下文）

在 `agent_tools.py:3390` 的 `except Exception as exc:` 块首行补一条带上下文的异常日志：

```python
except Exception as exc:
    logger.exception(
        "[SandboxPublicationUnhandled] run_id=%s agent_id=%s session_id=%s "
        "error_type=%s publish_paths=%s",
        sandbox_run_scope_id.get().strip() or None,
        agent_id,
        session_id,
        type(exc).__name__,
        (publish_paths if publish_paths is not None else selected_paths)[:20],
    )
    ...
```

要点：
- 复用同函数内已有的上下文变量（`agent_id`/`session_id`/`publish_paths` 在 2967 作用域内可见；`run_id` 用 `sandbox_run_scope_id.get()`，与 `flush_temp_workspace:2041` 同口径）。
- 若精确字段名在实现时与 2967 形参不一致，以函数签名实测为准（实施前 `read_file` 核对形参名）。
- 目的：下一次 `PermissionError` 直接拿到 traceback + 精确抛出点，把根因二的"不可定位"变成"一步定位"。

### P0-2 止血：修复 `.git/objects/` 下 root 属主历史目录（一次性清理，经审批执行）

- 目标：把 `950a1943.../workspace/mydome1/.git/objects/` 下的 root(0:0) 属主历史残留改为 1000:1000，让 backend(1000) 的 `.clawith-storage-tmp-*` 临时文件可写。
- 范围（只读扫描已产出清单）：
  - **19 个 root 属主 object 子目录**：`06/36/48/55/5d/5f/61/68/69/6e/7d/7f/84/9b/a6/d0/e1/f8/fe`（Sep 2 14:26）。
  - **31 个 root 属主 loose object 文件**（`find .git -type f -uid 0`），含 10 个散落在 1000 属主目录内（`10/2a/88/a8/fb/d2/4b/df/90`）。
- 同步排查结论（`find /data/agents/950a1943... -uid 0` 全量只读扫描）：root 属主项**全部**落在两处——①`.git/objects/`（上述 19 目录 + 31 文件，Sep 2 14:26）；②`mydome1/build` 目录本身 0:0（`drwxr-xr-x`，空目录，Sep 4 07:43，android 构建 tmpfs 挂载点由 daemon 以 root 创建所致）。`.git/objects` 之外无其他 root 残留。
- 执行范围因此收敛为两条命令（backend 容器内，`docker exec` 默认 root，可写）：
  ```bash
  docker exec clawith-agent-backend-1 \
    chown -R 1000:1000 /data/agents/950a1943-6ad6-4139-842e-8bde89ca823c/workspace/mydome1/.git/objects \
                       /data/agents/950a1943-6ad6-4139-842e-8bde89ca823c/workspace/mydome1/build
  ```
  （一次性、幂等；或先 `find ... -uid 0 -exec chown 1000:1000 {} +` 只挑 root 属主项，更保守。）
- 红线：chown 属写操作，**必须用户明确授权后执行**；执行前 `check-inflight-runs.sh` 确认无在途 run。

### P0-3（可选，防复发）持久化根因：`clone_workspace_to_staging` 后统一 chown

- `docker_backend.py:_start_persistent_session`（306）在 `clone_workspace_to_staging`（327）后无 chown；`shared.py:clone_workspace_to_staging`（554）`shutil.copytree(copy2)` 保留源属主。若历史 storage 含 root 属主文件，会再次复制出 root 属主 staging。
- 建议：clone 后对 staging 根执行幂等 `chown -R 1000:1000`（与 android build 既有 root exec chown 手法一致），把"属主漂移"从随机变为确定性。

### P1 根治：`.git` 不再进入 CAS 逐文件发布（两个候选：方案 A「不落库」/ 方案 B′「打包 bundle」，正式权衡见 §3.4）

**参考资料对照结论（reference-check，逐项对齐）：**

| 决策点 | 参考资料 | 结论 |
|---|---|---|
| git 元数据如何持久化 | letta-code MemFS（`docs/technical-plans/20260903-letta-code-study.md` §2）：git 当持久层，但走**整仓 git commit（原子）+ `memory-git.ts` config-lock/precommit/postcommit/sync-state**，绝不逐文件 CAS | 逐文件 CAS 发布 `.git` 是 Clawith 自创反模式，无参考先例 |
| 沙箱 filesystem 持久化 | E2B：**整 rootfs（含 `.git`）filesystem snapshot**（`packages/python-sdk/tests/async/sandbox_async/test_snapshot_filesystem_only.py` 实核，rootfs 整目录持久化 + 冷启动恢复）；OpenHands：docker volume 整目录挂载 | 业界口径 = 整目录原子（含 .git），非逐文件。⚠️ **二者都持久化 `.git`**，只支持「别逐文件 CAS `.git`」，**不直接支持「`.git` 不进 storage」**（评审修订，见 §3.5） |
| git 事实源 | git 标准工作流：remote 是唯一持久事实源，本地 `.git` 是 cache | GitLab remote 应为唯一事实源；storage 持 `.git` = "双重事实源"，正是损坏/冲突温床 |
| 凭证隔离 | letta「secret 进 harness secrets store，绝不进 git 追踪文件」+ Clawith 现有 `redact_git_secrets`/`inject_credentials_into_temp_workspace` | 方案 A 下 `.git` 不进 storage，凭证隔离天然强化 |

**方案 A（候选一）：storage 永不持有 `.git`，GitLab remote 为唯一事实源。**

改动清单（全部已 read_file 核对落点；**2026-09-04 评审修订后 = 7 处改 + 1 新增恢复 + 1 迁移**）：

1. `workspace_policy.py:15` — `PublishClass` 增 `"git_metadata"`。
2. `workspace_policy.py:55` `classify_publish_path` — `parts` 含 `.git` 段即返回 `"git_metadata"`（置于 `build/outputs` 与 `DERIVED_SEGMENTS` 判断之前）。
3. `agent_tools.py:2626` `classify_and_put`（`_collect_temp_workspace_files`）— `"git_metadata"` 既不进 `cas_files` 也不进 `overwrite_files`（进 `derived_paths` 或独立 `git_paths` 集合仅统计），CAS 循环自然跳过 `.git`。
4. `agent_tools.py:1923` `_materialize_storage_entry` — ⚠️ **判断必须放在函数最前（`is_file`/`is_dir` 判断之前）**，`classify_publish_path(rel_path) in {"derived", "git_metadata"}` 时直接 `return`。⚠️ 有**两个同名函数**：1790 行是旧版（4 参数，已被 1923 覆盖的死代码），**必须改 1923 行带 `budget/manifest` 的新版**，否则改动不生效。若只改 1934 的 `is_file` 分支，目录分支（1981–1993）仍会 `mkdir` 出**空壳 `.git` 目录树**，导致 P2 的「无 `.git`」判断误判而跳过 restore。
5. `agent_tools.py:2213` `flush_temp_workspace` delete pass — `"git_metadata"` 分支跳过 delete（与 derived 同语义"绝不反向删除 storage"）。
6. **⚠️ 评审补** `agent_tools.py:2507` `_workspace_candidate_changes` delete pass — 与 2213 **对称**地跳过 `"git_metadata"`。当前出错路径正是 reconciliation（`_workspace_candidate_changes` → `persist_candidate` → `apply_candidate`），其 delete pass 若遇 manifest 残留 `.git`，`git_metadata` 会落到 `CandidateChange.delete` 分支误删 storage 残留 `.git`。同函数 2445 的 `redact_git_secrets` 因 `.git` 不进 `cas_files` 而自然失效（见 §3.5）。
7. **新增** `gitlab_workspace.restore_git_metadata_from_remote()` + 在 `agent_tools.py:1870` 前调用（见 P2）。
8. **迁移**：storage 残留历史 `.git` 条目一次性清理（只读扫描→审批→delete，见 §5）。

结果：storage 只持 working tree（source+artifact），`.git` 永不落库 → 从根上消除"残缺 `.git` 进 storage"（根因一）与"storage 侧 `.git/objects` 属主漂移 → 写临时文件 EACCES"（根因二）。

**方案 B′（候选二）：`.git` 打包为 git bundle 单 blob 原子发布。**

用 git 原生 bundle（整仓 refs+objects 打包）替代「逐文件 CAS 发布 `.git`」，bundle 作为一个 CAS 对象原子落库。落地设计（2026-09-04 评审补，已核实 storage 写接口无大小硬限制、materialize 有 50MB 预算）：

- **发布侧**：`classify_and_put`（`_collect_temp_workspace_files`）遇 `.git` 目录时不逐文件收 `.git/**`，而是对该 repo 根执行 `git -C <repo> bundle create <tmp>/<repo>.git.bundle --all`（复用 `_run_git`，argv 无 shell），得单个 bundle 文件，作为独立 CAS 对象（`PublishClass` 增 `"git_bundle"`）写 storage（key `<agent>/workspace/<repo>/.git.bundle`，`write_bytes_if_match` 原子）。
- **materialize 侧**：storage 的 `.git.bundle` 单 key 读出 bytes → `git init` + `git bundle unbundle <bundle>`（或 `git fetch <bundle>`）恢复 refs+objects；working tree 逐文件 materialize 照旧；`inject_credentials_into_temp_workspace` 照旧写 rewrite（bundle 恢复的 `.git/config` 无 PAT，正好注入）。
- **credential 天然隔离**：bundle 只含 refs+objects，**不含 `.git/config`**（insteadOf rewrite/PAT 不在其中），故**无需 `redact_git_secrets`**——比方案 A 更干净（A 需处理 `redact_git_secrets` 变死代码）。
- **边界**：空仓库（无 commit）→ `bundle create` 失败即跳过（无历史可存），materialize 后 `.git` 为空，agent 据 `GITLAB_GUIDE.md` 自行 init/clone；bundle 超 materialize 预算（`TOOL_MATERIALIZE_MAX_FILE_BYTES`=50MB）→ 需为 bundle 设独立预算（建议 200MB）或降级为「跳过 bundle、靠 remote+P2 fetch 恢复」。
- **并发/双重事实源**：bundle 单 key CAS，多 run 并发写同 repo 会 version token 冲突（干净失败、不落库，触发冲突断路器，**无残缺损坏**）；bundle 可能落后 remote → materialize 后 `inject` 时顺手 `git fetch origin` 对账（复用 P2 的 fetch 认证），bundle 降级为「冷启动缓存」，remote 仍权威。

**语义变化（仅方案 A 适用，需同步 agent 提示词）**：
- 本地 commit **历史**跨 session 不持久（`.git` 不进 storage）；但 **working tree 文件内容持久**（它们在 storage）。恢复后，上次 push 之后的未提交改动以 **untracked/modified** 形态保留（原型已验证，见 P2）。
- 已 push 的 commit：remote 有完整历史，跨 session 恢复后 fetch 拿到，**零损失**。
- 对目标流程 `git add/commit/push/MR` 无影响；仅"commit 了但未 push 的中间 commit"会坍缩成 working tree 改动。

### 3.4 方案 A vs B′ 正式权衡与推荐（2026-09-04 评审补）

| 维度 | 方案 A（`.git` 不落库，remote 唯一事实源） | 方案 B′（`.git` 打包 git bundle 单 blob 原子发布） |
|---|---|---|
| 根治根因一（逐文件 CAS 残缺） | ✓（`.git` 不发布） | ✓（单 blob 原子 CAS） |
| 根治根因二（属主漂移 EACCES） | ✓（storage 无 `.git/objects`） | ✓（新 bundle 由 backend uid 1000 写；历史 root 残留仍需迁移） |
| 本地 commit 历史跨 session | ✗ 丢失（靠 remote + P2 restore） | ✓ 保留（bundle 含 refs+objects） |
| 未 push commit | ✗ 丢失（坍缩成 working tree） | ✓ 保留 |
| 双重事实源 | 消除（remote 唯一） | 仍在（bundle 落后 remote，需 fetch 对账） |
| credential 泄漏面 | 无（`.git` 不落库） | 无（bundle 天然不含 config） |
| agent 体验变化 | 有（需改提示词 / `GITLAB_GUIDE.md`） | 无（照常持久） |
| 实现面 | 小（7 处改 + restore） | 中（bundle create/unbundle 子进程 + 50MB 预算放宽 + 空仓库边界 + 两侧接入） |
| 参考资料对齐 | 自创（无先例） | 贴近 E2B/OpenHands「整目录原子快照」+ letta「整仓 git commit」 |

**推荐：方案 B′（倾向），A 为次选。**

选 B′ 的理由：
1. Clawith 是多租户企业平台，agent 长期维护一个 repo 是核心场景，「本地 commit 历史跨 session 持久」是 git 工作流的刚需（`git log`/`blame`/未 push commit）；方案 A 将其作为语义代价牺牲，是实质功能退化。
2. B′ 用 git 原生 bundle = 整仓原子，正是 letta「整仓 git commit（原子）」+ E2B「整 rootfs 原子快照」的确切语义，参考资料对齐比 A 更硬（A 是自创、无先例）。
3. B′ 的 credential 隔离天然（bundle 不含 config，无需 `redact_git_secrets`），比 A 更干净。
4. 代价可控：bundle create/unbundle 复用 `_run_git` 一个子进程；fetch 对账复用 P2 的 `-c insteadOf` 认证；50MB materialize 预算对 bundle 单独放宽即可。

选 A 的场景（何时 A 更合适）：
- 团队明确「remote 是唯一事实源、本地 `.git` 只是 cache」的哲学，且接受「本地 commit 历史跨 session 不持久」——则 A 实现面最小、彻底消除双重事实源。
- repo 巨大、bundle 频繁超预算（>50MB 是常态）——A 只存 working tree 更省 storage。

**决策点（待拍板）**：核心分歧只有一条——**「本地 commit 历史要不要跨 session 持久」**。重视 git 工作流连续性 → B′；重视「单一事实源 + 最小改动」→ A。本方案文档默认按 **B′** 继续细化（若拍板 A，则回到原 7 处改动清单，B′ 相关段落作废）。

---

### P2 兜底：跨 run 无 `.git` 时按 GitLab remote 恢复（状态模型已原型验证）

- **新增** `gitlab_workspace.restore_git_metadata_from_remote(temp_workspace_root, agent_id)`：对每个绑定 repo（复用 `_load_binding_credential` 拿 `pat/base_url/repo_name`），若 working tree 目录存在且无 `.git`，按 remote 重建 `.git`：
  ```bash
  git init -b <branch> \
    && git remote add origin <clone_url> \
    && git -c url.<rewrite>.insteadOf=<base_prefix> fetch origin \
    && git reset origin/<branch>   # mixed reset，非 --soft
  ```
  ⚠️ **fetch 必须带认证（2026-09-04 评审补）**：restore 在 `inject_credentials_into_temp_workspace` 之前执行，此时 `.git/config` 尚无 insteadOf rewrite，裸 `git fetch origin`（origin 为无凭据 URL）会 401。必须复用 `_clone_mode`（`gitlab_workspace.py:209–220`）的 `-c url.<rewrite>.insteadOf=<base_prefix>` 内联手法（`_credential_rewrite`(76)/`_base_prefix`(83) 构造、argv 数组不落盘、`_run_git` 已 redact PAT）；持久 rewrite 由后续 `inject_credentials_into_temp_workspace` 写入 config。
- **复用清单（2026-09-04 评审补，勿重写）**：`_load_binding_credential`(327) 取凭据、`_credential_rewrite`(76)/`_base_prefix`(83) 构造 rewrite、`_run_git`(95) argv+redact+timeout、`_apply_repo_config`(140) 身份+rewrite、`_detect_mode`(128)/`_adopt_mode`(251) 降级分支。
- **原型验证结论**（本地 git 实跑）：`git reset origin/<branch>`（mixed）后，与 remote 一致的 tracked 文件无 diff、未提交新文件保留为 `?? untracked`、`git log` 完整。⚠️ **必须用 mixed reset 而非 `--soft`**——soft 只动 HEAD 不动 index，会留下空 index 导致 `D a.txt` + `?? a.txt` 双重混乱（原型第一版实测踩坑）。
- 降级边界：remote 无该分支（空仓库/未 push）→ 退化为 `_adopt_mode`（git init + add -A + first commit + push）；working tree 目录不存在 → 跳过（首次 clone 由绑定初始化 `run_gitlab_workspace_init` 的 clone 模式负责）。
- 调用点：`_prepare_temp_workspace`（`agent_tools.py:1869` `_drop_incomplete_git_dirs` 之后、`1870` `inject_credentials_into_temp_workspace` 之前）——顺序 = 先恢复 `.git`，再注入凭证，沙箱内 git pull/push 才能带 token。
- 失败处理：best-effort（catch + log，不打断 execute_code），与 `inject_credentials_into_temp_workspace` 同哲学；失败时沙箱无 `.git`，agent 依据 `GITLAB_GUIDE.md` 自行 git init/clone。

### 3.5 评审修订记录（2026-09-04 多角度评审后新增）

本轮评审（真实代码 read_file 核对 + 埋点日志/traceback 复核 + reference-check）发现的方案缺口与修正，均已同步回正文：

| # | 评审发现 | 严重度 | 修正 |
|---|---|---|---|
| A | **改动清单漏了 reconciliation 路径的 delete pass**：`_workspace_candidate_changes`(2431) 的 2507 delete pass + 2445 redact 未列入。当前抛错的正是这条路（`recover_publication` → `apply_candidate`）。 | 高 | 新增改动 6（2507 与 2213 对称跳过 `git_metadata`） |
| B | **改动 4 落点位置会失效**：只改 1934 `is_file` 分支会留下「空壳 `.git` 目录树」（目录分支 1981 仍 mkdir），使 P2 的「无 `.git`」判断误判、restore 被跳过。 | 高 | 判断提前到 `is_file`/`is_dir` 之前（函数最前），`.git` 整体不物化 |
| C | **P2 restore 的 fetch 无认证**：restore 在 inject 前跑，config 无 insteadOf，裸 `git fetch origin` 401。 | 高 | fetch 复用 `_clone_mode` 的 `-c url.<rewrite>.insteadOf` 内联 |
| D | **参考资料引用偏差**：E2B/OpenHands 是「整目录（含 .git）原子快照」，不直接支持「.git 不进 storage」；方案 A 是自创决策，非资料直接推荐。 | 中 | 对照表措辞修正为「只支持否定逐文件 CAS」 |
| E | **迁移即丢未 push commit**：删 storage 残留 `.git` 会当场丢失本地未 push commit，非「跨 session 后坍缩」。 | 中 | 迁移前只读扫描 + 备份 + 用户确认 |
| F | **`redact_git_secrets` 变事实死代码**：方案 A 后两个调用点（2096/2445）永远 early-return。 | 低 | 保留则改注释，或删除；凭证隔离由「.git 不落库」保证 |
| G | **同名函数陷阱**：1790 旧版 `_materialize_storage_entry`（死代码）与 1923 新版同名，grep 可能改错。 | 中 | 改动 4 点名改 1923 新版 |

另附一条供决策的记录（未写入正文）：

- **死代码待清理**：`_materialize_storage_workspace`(1783) + 旧 `_materialize_storage_entry`(1790) 无人调用，是 materialize 双实现的遗留，建议随本次一并清理（非必须）。
- （原「方案 B′ 应补进权衡」一条已并入 §3.4 正式权衡，此处撤销。）

### 3.6 B′ 落地实现记录（2026-09-04 实施后）

已按授权实施 P1 方案 B′，代码落地 + 单测全绿（`test_workspace_publication_filter.py` 87 项、`test_gitlab_workspace.py`、`test_workspace_reconciliation.py` 等相关 132 项）+ `scripts/arch-guard.sh` 通过 + `ruff check` 通过。落地与方案正文的偏差/细化：

| # | 落地决策 | 说明 |
|---|---|---|
| 1 | `_collect_temp_workspace_files` 保持 **sync** | 发布侧收集仍为同步函数，返回 4 元组 `(cas_files, overwrite_files, derived_paths, git_repos)`；新增 **async** `_create_git_bundles(root, git_repos)` 在 `flush_temp_workspace`/`_workspace_candidate_changes` 中 `await` 后 `cas_files.update(...)`。 |
| 2 | bundle 独立预算 200MB | 常量 `TOOL_MATERIALIZE_GIT_BUNDLE_MAX_BYTES = 200*1024*1024`，发布侧 `_create_git_bundles` 与 materialize 侧 `_restore_git_bundles` 共用；超预算跳过 bundle（降级为无 `.git`，靠 `GITLAB_GUIDE.md` 自恢复）。 |
| 3 | `_load_binding_credential` 扩为 6 元组 | 返回 `(pat, base_url, repo_name, agent_name, agent_email, project_path)`，`project_path` 供 bundle restore 构造 `clone_url = base_url/project_path.git`。 |
| 4 | restore fetch 对账 best-effort | `restore_git_metadata_from_bundle` 用 `-c url.<rewrite>.insteadOf=<prefix>` 内联认证 fetch origin 对账（bundle 是冷缓存，remote 仍权威）；失败不抛。 |
| 5 | materialize 顶部 early-return | `_materialize_storage_path_with_budget` 最前：`git_metadata` 直接跳过（含目录，不 mkdir 空壳）；`.git.bundle` 记录进 `git_bundles` 后跳过。`git_bundles` 参数穿透目录递归。 |
| 6 | bundle 发布路径 | storage key = `<agent>/workspace/<repo>/.git.bundle`（单 CAS blob，`classify_publish_path` 判 `source`，`redact_git_secrets` 对其为 no-op）；本地暂存到 `root/.clawith-git-bundles/`（repo 工作树之外，不进 `git status`）。 |
| 7 | 两条 delete pass 对称跳过 | `flush_temp_workspace` 与 `_workspace_candidate_changes` 的 delete pass 均改为 `if publish_class in ("derived", "git_metadata"): continue`。 |
| 8 | restore 序列 | `git init -b __clawith_restore__` → fetch bundle `+refs/heads/*` `+refs/tags/*` `HEAD:refs/clawith-bundle-head` → `_resolve_bundle_branch` 反查分支 → `symbolic-ref HEAD` + `reset --mixed` → 清临时 ref/分支 → remote add/set-url origin → 内联认证 fetch 对账。 |
| 9 | 空仓库边界 | `create_git_bundle` 无 refs 时返回 `None`，跳过；materialize 后无 `.git`，agent 依 `GITLAB_GUIDE.md` 自 init/clone。 |

**未随本次实施（留待后续授权）**：① storage 历史 `.git` 条目迁移清理（只读扫描→审批→delete）；② 死代码 `_materialize_storage_workspace`(1783)/旧 `_materialize_storage_entry`(1790) 清理。

**P2 远程兜底落地记录（2026-09-04）**：新增 `gitlab_workspace.restore_git_metadata_from_remote(temp_workspace_root, agent_id)`，在 `_prepare_temp_workspace` 中接于 `_restore_git_bundles` 之后、`inject_credentials_into_temp_workspace` 之前。逻辑：绑定 repo 工作树存在但无 `.git` 时按 remote 重建——`git init -b <branch>` → `remote add origin <clone_url>` → `_apply_repo_config` → 内联 `-c insteadOf` 认证 `fetch origin`（走 `remote add` 写入的默认 remote-tracking refspec，确保 `origin/<branch>` 存在）→ `reset --mixed origin/<branch>`（保留未提交改动为 unstaged）→ `--set-upstream-to`；工作分支名优先取绑定 `default_branch`（`_load_binding_credential` 扩为 7 元组新增 `default_branch`，缺失时按 remote HEAD `ls-remote --symref` 推断，再退 `main`）；remote 无该分支 → `_adopt_mode`（init+首提交+push）；目录不存在或已有 `.git` → 跳过。单测新增 7 项（`test_gitlab_workspace.py`，重点断言 mixed reset 而非 soft、fetch 走内联认证），全绿；arch-guard P0 通过；ruff 通过。

---

## 4. 实施顺序与依赖

1. **P0-1 埋点** ✅ 已上线（`511b1a20`），已命中 5 次拿到精确 traceback，根因二坐实。
2. **P0-2 chown 止血**（独立，待用户授权）——`.git/objects` 19 目录 + 31 文件 + `build/` 转 1000:1000。
3. **P1 方案 A**（依赖 P0-2 止血后验证）——`workspace_policy.py`（PublishClass + classify）+ `_materialize_storage_entry` + `_collect_temp_workspace_files` + 两条 delete pass（2213/2507）共七处改动。
4. **P2 兜底**（依赖 P1-A 落地）✅ 已实现——新增 `restore_git_metadata_from_remote()` + 在 `_prepare_temp_workspace` 接入。
5. **P0-3 防复发**（独立，可选）——`clone_workspace_to_staging` 后统一 chown。
6. **迁移**：一次性清理 storage 中残留的历史 `.git` 条目（只读扫描→审批→delete）。

---

## 5. 验证清单

**功能验证（test 环境，遵守"测试环境不灰度"红线）**：
- [ ] execute_code 内 `git add/commit/push` 到 `mydome1` 全链路成功，无 `PermissionError`、无 `sandbox_execution_unverifiable`。
- [ ] `HEAD tree chain BROKEN` / `Not a valid object name` 不再出现。
- [ ] 跨 run（新 session）打开同一 repo：`.git` 经 P2 恢复后 `git status` 干净、`git log` 完整。
- [ ] storage 层确认不再有 `.git` 条目（方案 A 后 `agent_tool_executions` + storage 枚举双重核对）。
- [ ] `.git/objects/` 下 19 个 root 属主目录 + 31 个 root 属主 loose object 已转为 1000:1000（`docker exec` backend 容器 `ls -lan` + `find -uid 0` 复核为 0 残留）。

**回归验证**：
- [ ] `workspace_sync_conflict` 不再以 `.git` 路径为冲突主体（`agent_tool_executions.result_metadata.conflict_details` 核对）。
- [ ] 非 git 的普通 write_file/CAS 发布行为不变（source/artifact/derived 三类回归）。
- [ ] `redact_git_secrets` 语义在方案 A 下仍覆盖（`.git` 不再发布，则 config 不再需要脱敏发布，但 materialize 注入的 credential 仍需只进 sandbox 不进 storage——确认无泄漏）。
- [ ] 埋点后，任何未处理异常在 `docker logs` 可见完整 traceback（`[SandboxPublicationUnhandled]`）。

---

## 6. 回滚方案

- P0-1/P0-2 独立、无状态迁移，回滚 = revert 对应 commit。
- P1-A + P2：回滚 = revert + 恢复旧 `classify_publish_path` 语义；storage 侧残留 `.git` 若已清理则需重新从 GitLab remote clone 恢复（P2 本身即恢复通道，回滚成本低）。
- 任何一步在 test 环境验证失败即停，不进入生产。

---

## 7. 测试计划

复用 `20260903-workspace-conflict-observability-hardening.md` 的测试骨架，新增三组：

1. **单测（pytest，`backend/tests`）**：
   - `classify_publish_path` 对 `.git/**` 返回 `git_metadata`（含 `myrepo/.git/config`、`myrepo/.git/objects/aa/bb...`）。
   - `_collect_temp_workspace_files` 对含 `.git` 的 staging 树：`cas_files` 不含任何 `.git` 路径，`git_paths` 完整。
   - `_materialize_storage_entry` 对 `git_metadata` 路径不物化（storage 残留 `.git` 也不进沙箱）；**含目录级断言**：`.git` 目录本身也不 `mkdir`（沙箱内无 `.git` 目录树），验证判断已提前到 `is_file`/`is_dir` 之前。
   - `flush_temp_workspace` delete pass：manifest 含 `.git` 条目时不再触发 storage.delete（mock storage 断言无 delete 调用）。
   - **`_workspace_candidate_changes` delete pass（评审补）**：manifest 含 `.git` 条目时，`changes` 里不产生 `operation="delete"` 的 `.git` candidate（2507 与 2213 对称）。
   - `_execute_code_with_workspace_outcome` 抛出 `PermissionError` 时，捕获输出含 traceback（caplog 断言 `logger.exception` 被调用）。
2. **集成测试**：`restore_git_metadata_from_remote` 用 tmp git remote 实跑——断言 `git reset origin/<branch>` 后（a）与 remote 一致的 tracked 文件无 diff；（b）未提交新文件保留为 `?? untracked`；（c）`git log` 完整；并断言 **mixed reset 而非 `--soft`**（soft 会留空 index 造成 `D + ??` 双重混乱）。**认证断言（评审补）**：fetch 经 `-c url.<rewrite>.insteadOf` 内联认证（mock `_run_git` 断言内联 `-c` 参数，或 tmp remote 设认证后 fetch 成功、`.git/config` 无明文 PAT）。
3. **端到端（test 环境）**：真实 `execute_code` git push 到内网 `mydome1`，重复 3 次跨 run，断言全绿 + 跨 run 后 `.git` 由 P2 恢复、`git log` 完整、未 push 改动保留为 untracked/modified。

---

## 8. 风险与红线

- 本文档为只读交付：**未改生产代码、未重启服务、未写库**。实施需用户拍板。
- 方案 A 引入语义变化：本地 commit **历史**跨 session 不持久，但 working tree 文件内容持久、恢复为 untracked/modified（见 §3 P1 语义变化声明），需同步更新 agent 提示词/文档。
- 迁移清理 storage 历史 `.git` 属写操作，必须只读扫描→审批→执行，且保留备份。
- 遵守"测试环境不灰度（2026-08-30 红线）"与 DB/Redis 写操作需用户明确授权两条铁律。
