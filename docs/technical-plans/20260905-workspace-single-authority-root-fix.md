# 方向 3 根治方案：workspace 单一权威（消除「storage 层 vs git 视图」分层）

- 日期：2026-09-05
- 状态：**设计（只到方案，不实施）**
- 承接：`docs/analysis/20260905-workspace-view-divergence-research.md`（缺口 3/4 真伪判定，方向 1/2/3 已列）
- 关联：P0 edit_file CAS（`bf26613f`）、立项 `20260905-workspace-dual-writer-and-git-baseline-drift-research.md`、`20260904-git-metadata-publication-integrity-fix.md`（git bundle 化动机与历史）
- 触发 run：`764eb591-a38a-48cb-9120-23c83c3b0bda`（agent 在 `f_android_ai ↔ fix/p1-repeating-display ↔ fix/p1-display-fixedpoint` 间反复切分支）

---

## 0. TL;DR（先给结论）

| 判定 | 一句话 |
|---|---|
| **根因定论** | workspace 有 **storage / A（run 物化）/ B（execute_code 沙箱）三条物理拷贝**，一致性图里**只有一条断边**——「直写 storage 的写者（edit_file/write_file/move_file/delete_file/android_compile）→ B」。edit_file 直写 storage 后只刷 A，B 是 run 开始的一次性 clone，**永远看不到 edit** → `git status` 全程 clean → 模型把 git 当「找回改动」的救命稻草。 |
| **letta 方向的正误读** | 「edit 即 commit」**字面照搬是错的**：它修的是「bundle 冷缓存陈旧」（缺口 4），**不修**「B 看不到 edit」（764eb591 病灶）；且 auto-commit 会让 `git status` 仍 clean，反而二次迷惑模型。**正确迁移**是 letta 的底层原则——「**单一权威 + 视图实时派生 + 写后立即一致**」——落到 Clawith 的 storage 抽象上，就是「**让 B 成为 storage 的实时派生视图**」。 |
| **推荐根治 = 方向 3a** | 把「直写 storage → 同步进 B」从现在的 **best-effort 事后 refresh** 升格为 **第一类不变量（同步、锁保护、幂等、双后端覆盖）**。这是闭合唯一断边的最小且根治形态。 |
| **端局 = 方向 3d** | 让 A 与 B 合一（run 工作树直接挂进沙箱、不再 clone），从物理上消灭「分层」；但被 docker 挂载解析 + 属主漂移 + 隔离三件事阻塞，单列为后续演进，不进本次轨道。 |
| **3b（edit 即 commit）/ 3c（execute_code 前重物化）** | 分别因「修错层」和「是 3a 的劣化」被否（详见 §4）。 |

---

## 1. 根因定论：单一权威被违反，且只有一条断边

### 1.1 三条物理拷贝（read_file 核实，file:line 精确）

| 视图 | 载体 | 关键代码 | 角色 |
|---|---|---|---|
| **storage** | `STORAGE_LOCAL_ROOT/<agent_id>`（`WORKSPACE_ROOT`） | `agent_tools.py` L163 | **持久单一权威**（Local/S3 抽象） |
| **A：run 物化工作树** | `tempfile.TemporaryDirectory` | `_prepare_temp_workspace` `agent_tools.py` L1891-1945 | edit 后刷新目标；execute_code 结束 flush 回 storage |
| **B：execute_code 沙箱** | `staging_path`（`clone_workspace_to_staging` 产物） | `docker_backend.py` L327、`shared.py` L554-567 | execute_code 在容器内操作的工作树（git 视图在此） |

### 1.2 一致性图（唯一断边）

```
              clone 一次(run 开始)           bind-mount
storage  ──物化──▶  A（run 工作树） ──clone──▶  B（沙箱 staging）──▶ 容器 /workspace
   ▲                  ▲                          ▲
   │ edit_file 直写    │ refresh(A)               │ ✗ 断边（不更新）
   └──────────────────┴──────────────────────────┘
        execute_code: B --verify_and_merge_outputs(B→A)--> A --flush(A→storage)--> storage
```

- **execute_code 写**：B →（`verify_and_merge_outputs` 只挑 publish 路径，`docker_backend.py` L685-693）A →（`flush_temp_workspace`）storage。三视图**收敛**。
- **直写工具（edit_file 等）写**：storage（直写）→（`_refresh_run_workspace_after_direct_write`，`agent_tools.py` L2388-2468）A。**B 不动**。
- 结论：一致性图里**只有一条断边**——「直写 → B」。这是 764eb591 的精确机制。

### 1.3 为什么这是「根治」而不是「补丁」

- 断边只有一条。闭合它，三视图在 run 内恒等，`git status` 天然反映真相。
- 补丁思路（改模型 git 行为、加提示词「别切分支」）是「触发-消解」错位——模型反复 git 是**症状**，B 看不到 edit 是**因**。消解交给「视图正确」，不是「教模型别信 git」。（memory `plans-compare-reference-materials`：触发-消解分离，规则只召回、语义交给模型。）

---

## 2. letta 方向的正误读

### 2.1 letta MemFS 的真实模型（`letta-code/src/agent/memory-git.ts` 已 read_file 核对）

- `commitMemoryWrite`（L1340）→ `commitMemoryPaths`（L1213）：`stageMemoryPaths` + `git commit`——**每次 memory 写即一次 commit**。
- `assertMemoryRepoCleanForWrite`（L1266 附近）：**存在 uncommitted 变化就拒绝 memory 工具**（"Commit, discard, or sync them before using memory tools"）。
- 关键前提：**memory 工具是唯一的写接口**，没有 shell/execute_code 与它争写同一份文件；commit 本身就是「写已生效」的验证。所以 letta 里 `git status` 永不需要 agent 自己去核对——「无 uncommitted」是健康态，有就是异常态要 sync。

### 2.2 为什么字面「edit 即 commit」在 Clawith 是错的

Clawith 的 agent 有**两条写接口**：`edit_file`（backend 直写 storage）与 `execute_code`（沙箱 shell + 手动 `git add/commit/push`）。把 letta 的「edit 即 commit」字面搬来会：

1. **修错层**：auto-commit 只动 B 的 `.git` objects，**不更新 B 的工作树文件内容**（除非 commit 前先 sync 文件——那就又回到 3a）。所以它解决不了「B 的工作树文件内容陈旧」这个 764eb591 直接病灶；它真正对位的是「bundle 冷缓存陈旧」（缺口 4，已判冷启动体验）。
2. **二次迷惑模型**：agent 手动 `git status` 验证时，改动已被 auto-commit，`git status` **仍 clean**——「edit 后 git status clean」的困惑不但没解除，反而变成「clean + git log 多了一堆 auto-commit」，且 auto-commit 与 agent 自己的 `git add/commit/push` 流程打架、污染历史。
3. **不适配非 git workspace**：storage 里大量文件不在任何 repo（`focus.md`/`memory/`/`skills/`），「edit 即 commit」对它们无意义；Clawith 的持久权威是 storage（Local/S3），git 只是「绑定 repo」的可选特性。

### 2.3 正确迁移 = 单一权威 + 视图实时派生

letta 可迁移的不是「commit」这个动作，而是底层原则：**一个权威、一条写路径、视图从权威实时派生、写后立即一致。** Clawith 的忠实翻译：

> **storage 是唯一权威；B 是它的实时派生视图。任何写（直写工具 或 execute_code）在返回前，必须让 storage 与 B 处于一致态。**

这正是方向 3a。至此，「方向 3」与「方向 1（增量同步 B←A）」在根上合流——差别只在**把同步从 best-effort 升格为第一类不变量**，从而配得上「根治」二字。

---

## 3. 设计决策：单一权威模型

### 3.1 不变式（Invariant）

```
INV-1（单一权威）   storage 是 workspace 的唯一持久权威；A、B 都是派生视图。
INV-2（写后一致）   任何写操作返回前，storage 与 B（若已存在）对受写路径达到字节级一致。
INV-3（幂等合并）   B 的改动经 verify_and_merge_outputs 回 A、flush 回 storage 时，不产生重复发布/伪删除。
INV-4（隔离不破坏） 同步进 B 只动 publish 路径，不碰 .venv/.tmp/derived/.git 逐文件；沙箱隔离与回滚能力不变。
```

### 3.2 推荐方案：3a — 直写 → 沙箱同步，升格为第一类不变量

**机制**（设计级，非实现）：

1. **复用现有刷新锚点**：`_refresh_run_workspace_after_direct_write`（`agent_tools.py` L2388-2468）已刷 A；在其后接一步「同步进 B」。该锚点已被所有直写工具收敛调用——`_execute_workspace_mutation`（L3560，write/move/edit 共用变异路径，P0 CAS 所在，调用点 L3606/3637/3638/3664/3727）、`_write_file_outcome`（L4249/4273）、`_move_file_outcome`（L4617/4622/4657/4662）、`_delete_file_outcome`（L4801/4826）、`_edit_file_outcome`（L4956/4980）、`_android_compile_outcome`（L5452）——因此**只改锚点一处，所有直写工具自动获得 B 同步，无需改每个工具**。
2. **新增同步原语**（放 `sandbox/local/shared.py` 或 `run_workspace.py` 层，双后端共用，避免 docker/subprocess 各写一份）：
   - 定位 `run_id` 对应的活跃沙箱 session（`DockerSessionBackend._run_sessions[run_id]` / subprocess 等价表）。
   - **B 不存在（首次 execute_code 未启动）→ no-op**：未来 `clone_workspace_to_staging(work_path, …)` 从 A 拿最新，自然覆盖（A 已被刷新）。
   - 在 `persistent.lock` 保护下，把 `rel_path` 写入 `staging_path` 下对应路径（或删除），跳过 `.venv/.tmp`、非 publish 路径、derived 段（与 `verify_and_merge_outputs` 同口径 `is_allowed`）。
3. **幂等性（关键，已核对代码）**：写完 B 后，下一次 execute_code 的 `verify_and_merge_outputs` 对「B 文件字节 == A 文件字节」的路径直接 `continue`（`shared.py` L394-399）→ **不产生重复发布**；flush 的 delete pass 也因 manifest 已含该路径而不会误删。同步本身与现有合并/发布**正交**。
4. **锁序**：定义 `run_workspace state.lock`（A）→ `persistent.lock`（B）的固定顺序，避免死锁。实际 run 内 edit_file 与 execute_code 是串行的（模型一次一个工具），并发窗口极小，但锁序要显式写进实现约束。
5. **隔离不破坏**：同步只写 publish 路径（已在 storage 侧过校验的 `rel_path`），沙箱仍可 `rm -rf`/`pip install` 而不会让这些动作反向污染 A/storage——B 作为「可弃副本」的隔离/回滚语义**保持不变**（这是 3a 优于 3d 的核心）。
6. **后端覆盖**：`docker_backend.py` 与 `subprocess_backend.py` 都要接（两者 L327/L411 同样的 clone、L685/L717 同样的 merge）。接口抽象到 shared 层，避免双份逻辑漂移。

**为什么这是「根治」**：闭合 §1.2 的唯一断边后，三视图在 run 内恒等，`git status` 天然反映真相；且改动面收敛在「刷新锚点 + 一个同步原语」，不触碰 storage 抽象、不破坏沙箱隔离、不改变 agent 的 git 工作流。

### 3.3 被否候选（正式理由）

| 候选 | 对位 | 否决理由 |
|---|---|---|
| **3b（edit 即 commit）** | 缺口 4（bundle 陈旧） | 修错层 + auto-commit 让 `git status` 仍 clean（二次迷惑）+ 污染历史 + 与 agent 手动 git 流程打架 + 不适配非 git 文件（§2.2） |
| **3c（execute_code 前 restore 最新 bundle / 重物化 B）** | B 陈旧 | 整仓重物化会**丢掉 B 里 agent 上一次 execute_code 的未 flush 改动**（沙箱自身 in-progress 工作），除非做「增量 re-apply」——那正是 3a；且每 execute_code 一次 clone 成本高。3c 是 3a 的劣化形态 |
| **3d（A==B，单一物化视图 / bind-mount）** | 物理消灭分层 | 纯正形态，但被三件事阻塞（§3.4），单列演进，不进本次轨道 |

### 3.4 端局方案 3d（单一物化视图）—— 记录阻塞点，供后续单独评估

真正消除「分层」的形态：run 工作树（A）直接作为容器挂载，不再 clone 出 B，则 edit_file 刷 A 即刷容器视图，断边**不存在**。

三个阻塞点（都不是小改动）：

1. **docker 挂载解析**：A 是 `tempfile.TemporaryDirectory`（/tmp），而 docker daemon（OrbStack）需在共享 bind mount 下解析 mount source——这正是 B 被放到 `/data/agents/.sandbox-staging/` 的原因（`docker_backend.py` L318-320 注释「Under /data/agents (shared bind mount) so the host daemon can resolve the mount sources」）。要 3d，A 也得建在共享挂载下，牵动 `_prepare_temp_workspace` 的落盘位置（后端侧、跨后端、跨非沙箱读路径）。
2. **属主漂移历史**：20260904 根因二（`.git/objects/` 19 个 root 属主目录）证明「容器直接写宿主目录」会引入属主分叉。bind-mount A 会让容器 uid 1000 的写直接落 A，需把 P0-3 的 chown 防复发做成不变量。
3. **隔离性收窄**：B 作为副本给了「容器自由突变 + 受控合并（`verify_and_merge_outputs` 只挑 publish 路径）」的隔离边界；bind-mount 后 `rm -rf` 直接打 A，边界消失，flush 的 delete pass 会面对「整树删除」，风险面变宽。

**结论**：3d 是「单一权威」的纯正终态，与 letta「git 即文件系统、零分层」最贴近，但代价是 mount 布局 + 属主 + 隔离三件架构级改动。建议在 3a 落地并稳定后，再单独立项评估 3d（与 `run-state-generation-study` 的「run 所有权代际」一并纳入架构演进路线图）。

---

## 4. 参考项目对照（≥10，reference-check 逐项对齐）

> 方法：把「edit 工具 与 shell/git 是否同源、是否单一权威、陈旧写者如何被拒」这个决策点，对照本地 reference-projects 清单（memory `reference-projects`）。诚实标注「无此机制/不可读」的负结论。

| # | 项目 | 关键机制（file:line 或研究出处） | 对本次设计的结论 |
|---|---|---|---|
| 1 | **letta-code** | MemFS：memory 存 server git repo，`commitMemoryWrite`（memory-git.ts L1340）每次写即 commit；`assertMemoryRepoCleanForWrite`（L1266）有 uncommitted 即拒绝 | 唯一「edit=commit」先例；但**前提是 memory 工具是唯一写接口**。迁移的是「单一权威」，不是「commit」动作（§2.2） |
| 2 | **opencode** | content-addressed 工作树快照（`Hash.fast(worktree)`，snapshot.ts，见 20260904 研究） | 「单一权威 = 内容哈希」，无「陈旧基线」概念；edit 与 shell 共享一个 worktree，无 storage/view 分层 |
| 3 | **OpenHands** | 单一 docker volume 挂为 workspace，file 工具 + shell + git 都在容器内同一目录操作 | 同源单视图，天然无「直写 vs 沙箱」断边 |
| 4 | **E2B** | 沙箱 filesystem 即状态，整 rootfs snapshot 持久化 | 同源单视图；持久化=整目录原子（非逐文件） |
| 5 | **SWE-agent** | apply-patch + shell 在一个容器/工作目录，git 是基线 | 同源单视图 |
| 6 | **codex（OpenAI）** | edit/write/shell 工具作用于同一工作树 | 同源单视图（据工具模型推断，未逐行核） |
| 7 | **gemini-cli** | 本地工作目录，工具与 shell 同目录 | 同源单视图（推断） |
| 8 | **orca** | `runtimeFence`/`expectedRuntimeFence` 单调计数（agent-session-wire.ts，见 20260905-orca-study） | 「写前代际仲裁」= 方向 2；与 3a 互补（防「写进去」vs 防「看不见」） |
| 9 | **LangBot** | `placement_generation` 单调代际 + `assert_execution_active`（见 20260905-langbot-study） | 同 orca；陈旧执行体自灭 |
| 10 | **herdr** | 结构态/高 churn 态分离 + 原子写 + 双源仲裁（见 20260905-herdr-study） | 「单一权威 + 派生视图」范式的直接佐证 |
| 11 | **claw-code** | 单工作目录（museum exhibit，见 reference-projects） | 中性：同源单视图，但非生产、参考价值弱 |
| 12 | **CubeSandbox** | 沙箱即服务 | 负结论：只给沙箱隔离，无「storage↔沙箱」同源机制可抄 |
| 13 | **deepagents（T0，同栈）** | StateBackend 把文件状态放进 graph state `files` channel 随 checkpoint 持久（backends/） | 同栈 LangGraph 的最优先参考：**文件状态与 checkpoint 单一权威**，可迁移范式；与本方案「storage 单一权威」同构 |

**一句话对照结论**：成熟 coding agent（codex/gemini-cli/OpenHands/SWE-agent/E2B/opencode）**都靠「edit 工具与 shell/git 同源单视图」避开本问题**——它们没有 Clawith 的「持久 storage 抽象」这一层。letta 是唯一「edit=commit」，且其成立前提（memory 是唯一写接口）Clawith 不具备。Clawith 的分层是它「多租户 + 跨 run 持久 workspace」差异化能力**的代价**，根治不是放弃该能力，而是把「直写→派生视图」这条断边做成第一类不变量（3a）。orca/LangBot 的单调代际（方向 2）是「防陈旧写者」的互补件，herdr/deepagents 是「单一权威 + 派生视图」的同构佐证。

---

## 5. 与既有工作的关系（不重复、不冲突）

- **P0 edit_file CAS（`bf26613f`）**：防「写丢」（并发覆盖）——已落地，不重复。
- **方向 2（run 代际 / 写前仲裁）**：防「陈旧写者写进去」——与 3a 互补（一个管「写不写得进去」，一个管「写完了看不看得见」），可后续与 `run-state-generation-study` 合并立项。
- **缺口 4（bundle 基线对齐 remote）**：冷启动体验优化，独立小改，不进本次轨道。
- **本次 3a**：防「看不见」（写后视图不一致）——直接对症 764eb591。

三者是同一条「workspace 单一权威」主线上的三个正交加固，不是互相替代。

---

## 6. 风险与红线

- 本文档为**只读交付**：未改代码、未写库、未部署、未重启容器。
- 测试环境不灰度（2026-08-30 红线）；DB/Redis 写操作需用户明确授权。
- 3a 的实施风险点（进入实施前需逐条在 tdd 轨道验证）：
  1. **锁序死锁**：A lock → B lock 的固定顺序必须显式实现并加并发测试。
  2. **幂等合并**：必须验证「同步进 B 后，下一次 execute_code 不产生重复发布/伪删除」（依赖 `shared.py` L394-399 的字节相等 `continue`）。
  3. **isolated_output 模式**：publish 路径单一，同步需按 `publish_paths` 过滤，避免把不该进沙箱的路径写进去。
  4. **B 未启动 / B 已 close**：`_run_sessions` 无此 run、或 `_container_alive` 为 false 时，同步要安全 no-op（未来 clone 兜底）。
  5. **属主**：写 B 的进程是 backend（uid 1000），与容器 uid 1000 一致，无新增属主分叉；但要防「root 路径写 B」重演（复检 P0-3 chown 不变量）。

---

## 7. 验证计划（进入实施后，tdd 轨道）

1. **单测**：
   - 直写工具返回后，`staging_path` 下对应文件字节 == storage 字节（含 delete 路径删除成功）。
   - B 未启动时同步 no-op、不抛；B 已 close 时 no-op。
   - 同步后紧接 execute_code：`verify_and_merge_outputs` 不产生该路径的 publication candidate（幂等）。
   - isolated_output 模式：非 publish 路径不写入 B。
   - 双后端（docker + subprocess）同一条同步原语各过一遍。
2. **集成**：tmp 沙箱内，`edit_file` 改一个绑定 repo 文件 → 立即 `execute_code git status --porcelain` → 断言该文件显示 ` M`（modified），**不再 clean**。
3. **端到端（test 环境，遵守不灰度红线）**：复刻 764eb591 的「edit → git status」序列，断言不再出现「edit 后 27 秒 git status 仍空」；`workspace_conflicted_count` 保持 0。

---

## 8. 决策结论（2026-09-05 grill-me 收束，已拍板）

**方向选型（对应原 §8 三问）**：

1. **采纳 3a 作根治**（不先评估 3d）。3a 直接对症 764eb591、最小风险、可独立验证；3d 是架构洁癖，留待 3a 稳定后按观测数据再议。
2. **3a 与方向 2（run 代际仲裁）分开立项**。3a 小而独立快速闭环；方向 2 归入 `run-state-generation-study` 大项，走自己的 tdd 轨道。
3. **3d 三阻塞点记 backlog 备忘，不立项不研究**；3a 稳定后用「三拷贝 + 同步是否造成真实运维/性能负担」的观测数据决定要不要做。

**grill 第一轮 Q1–Q5 拍板结果（约束 3a 最终形态）**：

| # | 决策 | 拍板 |
|---|---|---|
| Q1 | 同步范围 | **窄**：首轮只同步 source 类直写（edit/write/move/delete）；android_compile 的 `build/outputs` 产物不同步，列为「同断边第二实例」backlog |
| Q2 | 同步语义 | **不 stage**：只写工作树文件，`git status` 显示 ` M modified` |
| Q3 | 语义变化 | **接受**：edit_file 改动覆盖 B 当前 checkout 分支的对应文件（storage 单一权威的必然语义；顺带让 git「有 modified 不能 checkout」保护生效） |
| Q4 | 锁与并发 | **复用 `persistent.lock`**，锁序「run_workspace `state.lock`(A) → `persistent.lock`(B)」；B 未启动/已 close 时静默 no-op（未来 clone 从 A 兜底） |
| Q5 | 推进 | **进入 implement（tdd）**，单 context window，验收=「edit 后 execute_code `git status --porcelain` 显示 ` M` 而非 clean」+ `workspace_conflicted_count` 保持 0 |

---

## 9. 实施记录（2026-09-05，tdd 已闭环）

**已落地（code + test + 提交同源）**：

- **Seam A**：`app/services/sandbox/local/shared.py` 新增 run 级 staging 注册表 `_sandbox_staging_registry`（`_StagingRegistryEntry`：`staging_path`/`lock`/`workspace_mode`/`publish_paths`）+ `register/unregister/refresh_sandbox_staging_path`。写/删/no-op/`..`+symlink 穿越拒绝/isoloated_output 过滤。单测 `tests/test_sandbox_staging_refresh.py`（6 例）。
- **Seam B**：`app/services/agent_tools.py` `_refresh_run_workspace_after_direct_write` 在 `refresh_run_workspace_path`（A）之后，对 `classify_publish_path=="source"` 调 `refresh_sandbox_staging_path`（B）。窄同步（derived/artifact/git_metadata 跳过）+ 删除路径 `data=None` 均覆盖。单测 `tests/test_agent_tools_storage_workspace.py`（source 命中 / 3 类跳过 / 删除 5 例）。
- **双后端接线**：`docker_backend.py` 与 `subprocess_backend.py` 在 `_start_persistent_session` 注册、`close_run` 注销（带 `workspace_mode`/`publish_paths`）。单测 `test_sandbox_docker_backend.py`（真实 fake_client 会话注册/注销）、`test_sandbox_subprocess_backend.py`（mock bwrap 注册）。
- **Seam C（近似端到端）**：`tests/test_agent_tools_storage_workspace.py::test_edit_file_propagates_to_sandbox_staging_git_view` —— 真实 git repo + `clone_workspace_to_staging` + 注册 + edit → `git status --porcelain` 断言 ` M` 而非 clean。

**锁序澄清（§6.1 已证）**：`refresh_run_workspace_path` 在 `async with state.lock:` 内自持 A 锁并在返回前释放（`run_workspace.py` L186）；`refresh_sandbox_staging_path` 在其**返回后**才获取 B 锁（`persistent.lock`）。两把锁**顺序获取、从不嵌套**，故无锁序死锁可能，「A→B 固定顺序」由构造保证——并发测试因此不适用（无共享临界区）。

**已覆盖的 §7.1 单测**：字节一致（写）、删除、no-op、isolated_output 过滤、双后端各过一遍。**§7.2 集成**以近似 Seam C 落地（真实 execute_code 需 bwrap，本地 test 环境无 bwrap，故降级为「clone+注册+git」闭环）。

**明确 deferred（不在本次提交范围）**：
- §7.3 端到端（test 环境复刻 764eb591 序列 + `workspace_conflicted_count==0`）：需部署 test 环境后验证，遵守「测试环境不灰度」红线。
- android_compile `build/outputs` 产物同步：Q1 backlog「同断边第二实例」。
- 3d（A==B bind-mount 单一物化视图）：3a 稳定后按观测数据再议。
