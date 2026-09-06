# workspace 视图分叉研究：缺口 3 / 4 真伪判定

> 2026-09-05 · 承接立项 `docs/technical-plans/20260905-workspace-dual-writer-and-git-baseline-drift-research.md`。
> 本文只做**只读调研**：逐缺口给出「真问题 / 假警报」判定 + 证据链（file:line + PG 样本）+ 若真的最小修复方向。**不实施、不改代码、不写库。**
> 方法论先跑 **Q4.3 因果定位**（立项明确要求防硬套），再落两个缺口的判定。

---

## 0. TL;DR（先给结论）

| 缺口 | 判定 | 一句话证据 |
|---|---|---|
| **缺口 4**（bundle restore 基线代际） | **假警报**（对 run 764eb591） | git 视图 `HEAD == origin/f_android_ai == 2782458`，基线 **fresh**；restore 基线陈旧不在 764eb591 因果链上。代码里 L509↔L622 的基线不一致**确实存在**，但只是「冷启动体验」隐患，触发面窄，与 764eb591 无关。 |
| **缺口 3**（android_compile 双写者） | **真问题，但立项表述需修正** | 764eb591 里 `workspace_conflicted_count` 全程 **0**（无「伪冲突丢弃」）；真正的病灶是**「直写 storage 的写者（edit_file / android_compile）与 execute_code 沙箱视图是两份独立拷贝，且沙箱是一次性 clone，永远看不到 edit」**——这才是 `git status` 全程 clean 的机制。 |

**核心新发现（764eb591 的精确机制，比立项假说更准）**：execute_code 的沙箱**不是**「run-scoped 物化拷贝（A）本身」，而是 A 在 run 开始时的一次性 clone（B）。edit_file 直写 storage 后，`_refresh_run_workspace_after_direct_write` 只把改动同步进 A，**B 不随 A 更新** → 模型在 B 里跑 `git status` 永远 clean。这是「分层错位」第一因的机制证据，也顺带修正了缺口 3 的定位。

---

## 1. Q4.3 因果定位：restore 基线是否在 764eb591 因果链上（先跑）

### 1.1 结论：**不在**。764eb591 的 `git status` clean 是「运行期视图分层」，非「物化期 restore 基线」。

### 1.2 证据链

**证据 A — git 视图基线是 fresh 的，没有陈旧**（PG `agent_tool_executions`，run_id=`764eb591-a38a-48cb-9120-23c83c3b0bda`）：

execute_code 12:30:25 的 result_summary：
```
=== fetch ===
=== vs origin ===
278245846afcf253e039cf2139f614ca2ac4612d
278245846afcf253e039cf2139f614ca2ac4612d
```
local `HEAD` == `origin/f_android_ai` == `2782458`。restore 出来的 git 基线与 remote **一致**，不存在「bundle 落后 remote 导致陈旧 HEAD」的前提。

**证据 B — clean 发生在 edit 之后的运行期**（分析文档 `2026-09-05-git-branch-switch-loop-764eb591.md` §二）：12:36:31 失忆重做 edit，12:36:58（27 秒后）`git status` 仍空。edit 是运行期动作，若基线陈旧，clean 应出现在「物化期」，而不是「每次 edit 之后仍 clean」这种运行期反复。

**证据 C — 缺口 4 的触发前提未满足**（代码 `gitlab_workspace.py`）：缺口 4 只在「bundle HEAD 落后 remote 且工作树有未 flush 改动」时暴露（`restore_git_metadata_from_bundle` L506-509 reset 到 bundle 捕获的 HEAD）。764eb591 里 bundle HEAD == remote（证据 A），前提不成立，机制未触发。

**判定**：缺口 4 对 764eb591 是**假警报**，降级为「冷启动体验优化」，不进 764eb591 修复轨道。

---

## 2. 缺口 4：git bundle 恢复的 reset 基线代际一致性

### 2.1 代码事实（read_file 核实，file:line 精确）

| 事实 | 位置 | 说明 |
|---|---|---|
| bundle 恢复：`init` → `fetch <bundle>` → `symbolic-ref HEAD <branch>` → **`reset --mixed <branch>`** | `gitlab_workspace.py` L490-509 | `<branch>` = bundle 捕获的 HEAD（`_resolve_bundle_branch` L447-462） |
| reset 后 **fetch origin 只更新 remote-tracking refs，不 reset 工作树/index** | `gitlab_workspace.py` L520-525 | 内联 `-c insteadOf` 认证，best-effort |
| P2 兜底 `restore_git_metadata_from_remote`：先 fetch 再 **`reset --mixed origin/<branch>`** | `gitlab_workspace.py` L608-622 | 基线 = fresh remote，不陈旧 |

**代码事实确认**：L509（reset 到 bundle 陈旧 HEAD）与 L622（reset 到 `origin/<branch>`）**基线语义不一致**。立项 §3.4 关键发现 1「Clawith 自相矛盾（文档写 remote 权威、代码 reset 到 bundle 陈旧 HEAD）」**属实**。

### 2.2 判定：**代码缺陷为真，但危害是「冷启动体验」，非 764eb591 相关**

- **真问题（范围收窄）**：当 `bundle HEAD` 落后 `origin/<branch>`（跨 session 冷启动 + 上次 flush 后 remote 有新提交）时，bundle 路径 restore 后 `git status` 会把「remote 新于 bundle 的那批文件」也显示为 modified/untracked，且 `fetch origin`（L520）不 reset 工作树，**对账没做完**。模型冷启动即看到一个「脏但说不清」的 git 视图。
- **触发面窄**：需同时满足「跨 session 冷启动」+「bundle 落后 remote」+「工作树有未 flush 改动」三条件。764eb591 是单 run 内运行期反复，不命中（证据 A/C）。
- **Q4.1 实测语义**：`reset --mixed` 只动 index 不碰工作树文件，所以「storage 工作树比 bundle 新」时，git 会把这些新文件显示为 **untracked/modified**，与 agent 实际 edit 对不上 → 状态漂移。这正是「冷启动体验」问题的形态。

### 2.3 最小修复方向（只到方案，不实施）

**方向（单一权威，对齐 remote）**：bundle 路径在 `fetch origin`（L520）之后，把 reset 基线从 `<bundle branch>` 改为 `origin/<branch>`（复用 L622 的语义），使 bundle / remote 两条 restore 的基线统一为「remote 权威」。改点极小（约 L509 一处 + 分支名映射），可复用 `_credential_rewrite` 内联认证后的 fetch 结果。

**参考项目对照**：letta「git 即文件系统、每次写 commit」让基线天然无陈旧；opencode 用 `Hash.fast(worktree)` 内容哈希标识快照、无「陈旧基线」概念。两者都指向「**基线不应是 bundle 捕获时的陈旧 HEAD，而应是 remote 权威或工作树内容哈希**」。→ 本方向的「对齐 remote」是最小实现，成本最低。

**建议**：作为低优先级「冷启动体验优化」单列，**不进** 764eb591 相关修复轨道；进入实施需另行拍板。

---

## 3. 缺口 3：android_compile「双写者」的 run-scoped 视图漂移

### 3.1 先给修正后的判定

立项把缺口 3 表述为「android_compile docker 直写持久根、绕过 run-scoped 物化、靠 best-effort refresh 事后对账 → 伪冲突风险」。764eb591 证据显示：

1. **「伪冲突丢弃」是假警报**：全程 `workspace_conflicted_count == 0`（见 §3.3 证据 B），apk 走 LWW（`classify_publish_path` artifact），refresh 成功后 manifest 对账无冲突。
2. **真问题是「双写者」这个表述之外的更广机制**：execute_code 沙箱 B 是 A 的**一次性 clone**，**任何**「直写 storage」的写者（edit_file / write_file / move_file / delete_file / android_compile）的改动都到不了 B → `git status` 永远 clean。android_compile 只是其中一个写者，**764eb591 的直接病灶是 edit_file，不是 android_compile**。

### 3.2 视图同构性（Q3.1）：三条视图，不是两条

read_file 核对后，实际是**三条独立视图**，比立项假说的「两条」多一层：

| 视图 | 载体 | 代码位置 | 谁写/谁读 |
|---|---|---|---|
| **storage（持久根）** | `STORAGE_LOCAL_ROOT/<agent_id>/...`（`WORKSPACE_ROOT`） | `agent_tools.py` L163、`storage_runtime/local.py` L32-36 | edit_file / write_file **直写**（`write_workspace_file`）；android_compile docker **bind-mount 直写**；read_file **直读** |
| **A：run-scoped 物化拷贝** | `tempfile.TemporaryDirectory`（`_prepare_temp_workspace`） | `agent_tools.py` L1891-1945 | edit 后 `_refresh_run_workspace_after_direct_write` **刷新**（L2388-2468）；execute_code 结束 `flush_temp_workspace` **回写 storage** |
| **B：execute_code 沙箱** | `staging_path`（`clone_workspace_to_staging` 的产物） | `sandbox/local/docker_backend.py` L327、`shared.py` L554-567 | execute_code **只读 + 改动**（git 操作在此）；结束 `verify_and_merge_outputs` **单向 B→A 合并**（`shared.py` L305-434） |

**关键（Q3.1 答案）**：`_refresh_run_workspace_after_direct_write` 的目标是 **A**（`refresh_run_workspace_path` 写 `workspace.root / rel_path`，`run_workspace.py` L196-240），**不是 B**。B 是 A 在 run 开始时（首次 execute_code）`clone_workspace_to_staging` 的一次性拷贝，之后由 `_run_sessions[run_id]` 复用（`docker_backend.py` `_persistent_session` L417-439，仅当 `work_path` 变化才重建，run 内不变）→ **B 永远停留在 run 开始快照，edit 永远进不了 B**。

### 3.3 764eb591 证据链（PG 只读样本）

**证据 A — 工具分布**（`agent_tool_executions`）：execute_code **20 次**（全部 git 操作）、edit_file **6 次**、android_compile **1 次**（12:19:31，产物 `app-debug.apk`）。

**证据 B — 全程零冲突**（`result_metadata->>'workspace_conflicted_count'`）：所有 execute_code 均为 `0`，`workspace_resolution_status = 'applied'`。**「陈旧 manifest → 伪冲突丢弃」在 764eb591 未发生**——Q3.3 的伪冲突风险是假警报。

**证据 C — edit 后 git 仍 clean（运行期）**：12:36:31 edit `Calculator.kt`（content_hash `d6ab6845...`），12:36:58 execute_code `git status` 仍空（§1.2 证据 B）。这是「B 看不到 edit」的直接症状。

**证据 D — 12:36:31 与 12:18:35 的 edit content_hash 完全相同**（均 `d6ab6845...` 与 `75e60830...`）：模型「失忆重做」同一批 edit，坐实「改了又消失」的循环。

**证据 E — 物化翻转的 flush 痕迹**：12:21:27 execute_code 的 `artifact_refs` 含 4 个 .kt + apk（`workspace_saved_count: 5`），说明 edit + android_compile 的改动经 refresh 进了 A、在 execute_code 结束被 flush 回 storage；但 B 的 git 视图始终 clean。

### 3.4 真问题的精确机制（764eb591 因果链，修正版）

```
edit_file 直写 storage（Calculator.kt = d6ab6845）
        │  _refresh_run_workspace_after_direct_write → 刷新 A（物化拷贝）
        │  （B 沙箱是一次性 clone，不随 A 更新）
        ▼
execute_code 在 B 里跑 git status → 永远 clean（B 是 run 开始快照）
        │
        ▼
模型「以为没改成」→ git checkout 切分支「找回」→（物化翻转：见下方注）
        │
        ▼
模型 read_file（读 storage）看到旧内容 → 以为改动丢了 → 重新 edit → 循环
```

> **注（防过度断言）**：本文的**铁证**是「edit 直写 storage + B 一次性 clone → execute_code 的 git 视图永远 clean」（代码 L327/L417-439 + PG「edit 后 27 秒 git status 仍空」）。「物化翻转」（checkout 后 content_hash 翻转、`b5751819` 紧跟 checkout 出现）是上游分析文档 `2026-09-05-git-branch-switch-loop-764eb591.md` §三/§四 的结论；其**精确覆盖路径**（checkout 改 B → `verify_and_merge_outputs` 单向 B→A → `flush_temp_workspace` 回写覆盖 storage）需容器日志 `[WorkspaceFlushConflict]`/`[WorkspaceFlushConverged]` 佐证，本文核心判定**不依赖**该环节。

与立项 §3 参考项目对照完全对齐：这是「分层错位」的机制铁证——**文件系统不是单一权威，git 视图（B）与 storage 是两份独立事实源**（letta「文件系统即 git、每次写 commit」正是从根上消除此病；opencode「content-addressed 第二事实源」是同一方向）。

### 3.5 最小修复方向（只到方案，不实施，按成本递增）

> 注意：P0（edit_file CAS，`bf26613f`）已修「并发写覆盖」，但**没修「B 看不到 edit」**——两者是同一病灶的两面，前者防「写丢」，后者防「看不见」。本方向补的是后者。

**方向 1（最小，瞄准 B 的可见性）**：让 execute_code 沙箱 B 在每次执行前**增量同步 B ← A**（而非 run 开始一次性 clone）。即在 `_persistent_session` 复用路径上，把「自上次 execute_code 以来 A 的变化」刷进 B。成本低、直接消除「edit 后 git status clean」。

**方向 2（写前代际仲裁，参考项目主答案）**：直写 storage 的写者（edit_file/android_compile）携带 **run 代际 / version token**，execute_code 物化 B 时记录代际，代际不符的陈旧写者**写前拒绝**（orca `runtimeFence`+`expectedRuntimeFence`、LangBot `placement_generation`），而非现在的「best-effort 事后 refresh（4 类 skip + 异常吞，`agent_tools.py` L2388-2468）」。这是把 P0 的 CAS 原语从「单文件」升格为「run 级代际」。

**方向 3（根治，成本最高）**：letta 方向——git 视图 = 文件系统单一权威，edit 即 commit，`git status` 天然反映真相。需改 workspace 写入模型，属架构级，超出本缺口最小修复。

**推荐**：先做**方向 1**（最小、直接对症 764eb591 的「B 不可见」），把「直写 storage → execute_code 可见」这条通道打通；方向 2/3 作为后续架构演进的候选，与 `run-state-generation-study`（run 所有权代际）合并立项。

---

## 4. 参考项目对照（已执行 reference-check，落地到判定）

| 原则 | 参考项目机制（file:line） | 落在本研究的哪个判定 |
|---|---|---|
| 单调代际（拒绝陈旧写者） | orca `runtimeFence`/`expectedRuntimeFence`（`agent-session-wire.ts:164-217`）；LangBot `placement_generation`（`botmgr.py:56/646-670`） | 缺口 3 方向 2：refresh 升格为写前代际仲裁 |
| 单一权威 + 结构态/高 churn 分离 | letta MemFS git 化（`memory-git.ts`）；herdr 双源仲裁（`persist.rs:3-5`、`detect/mod.rs:10-20`） | 缺口 3 根因（storage↔git 双重事实源）+ 缺口 4 基线统一 |
| content-addressed + 原子写 | opencode `Hash.fast(worktree)`（`snapshot.ts:98`）；herdr 原子写（`io.rs:48-61`） | 缺口 4：基线应是 remote 权威或内容哈希，非 bundle 陈旧 HEAD |

**一句话**：参考项目共同答案「**让陈旧写者写不进去 / 单一权威**，而不是写完再对账」。764eb591 的证据把这条原则落在了一个更具体的位置：**execute_code 沙箱（B）必须是 storage 的一个「实时视图」而非「一次性快照」**。

---

## 5. 证据出处汇总

- **代码**：`agent_tools.py`（L163/L1891/L2388-2468/L3104-3121/L3671-3733/L5299-5468）、`gitlab_workspace.py`（L447-529/L532-635）、`docker_backend.py`（L327/L417-439/L685-693）、`shared.py`（L305-434/L554-567）、`run_workspace.py`（L141-246）、`storage_runtime/local.py`（L32-36/L145-169/L300-302）。
- **PG**：`agent_tool_executions`（run `764eb591-a38a-48cb-9120-23c83c3b0bda`）——工具分布、execute_code result_summary（git 视图 HEAD/`vs origin`）、`result_metadata`（`workspace_conflicted_count`/`workspace_resolution_status`/`workspace_saved_count`/`artifact_refs`）、edit_file content_hash。
- **上游文档**：`2026-09-05-git-branch-switch-loop-764eb591.md`、`20260905-workspace-write-generation-cas-plan.md`、本立项 `20260905-workspace-dual-writer-and-git-baseline-drift-research.md`。

## 6. 红线与退出

- 只读调研完成：未改代码、未写库、未部署、未重启容器。
- 退出标准达成：两缺口各有可拍板判定（缺口 4=假警报+冷启动优化方向；缺口 3=真问题（表述修正）+最小修复方向 1/2/3）。
- **进入实施需另行拍板**；缺口 3 方向 1 建议与 `20260905-workspace-write-generation-cas-plan.md` 同一 tdd 轨道评估。
