# 研究立项：workspace 双写者漂移 + git bundle 恢复基线代际一致性（缺口 3 / 4）

> **状态**：研究立项（只读调研，不改代码、不写库、不部署）。产出 = 每个缺口的「真问题 / 假警报」判定 + 证据链 +（若真）最小修复方向，供拍板。
> **上游**：
> - 根因分析 `docs/analysis/2026-09-05-git-branch-switch-loop-764eb591.md`（run 764eb591 三分支 ping-pong checkout）。
> - 方案文档 `docs/technical-plans/20260905-workspace-write-generation-cas-plan.md` §2 缺口 3/4（当时标记「候选，未完全确认」）。
> - 姊妹研究 `docs/technical-plans/20260905-run-state-generation-study.md`（run 所有权代际，互补不重复）。
> - 关联实现 `docs/technical-plans/20260904-git-metadata-publication-integrity-fix.md`（`.git` bundle 化 + restore 已落地，但只解决「git 一直不通」，未覆盖本文的「基线代际漂移」角度）。
> **红线**：测试环境不灰度（2026-08-30）；DB/Redis 只读；写代码级方案前 read_file 核对真实代码。

---

## 0. TL;DR

CAS 方案文档把两个「候选缺口」标为未确认。本立项要把它们各拆成可验证的研究问题，用 read_file + PG/Langfuse 证据 + reference-check 判真伪：

- **缺口 3**：android_compile 是「双写者」——docker 直接写持久工作区，不走 run-scoped 物化，靠 best-effort `_refresh_run_workspace_after_direct_write` 事后对账。要查清「run-scoped 视图 vs 持久根」是否分叉、best-effort 漏刷新会否在下一次 flush 产生伪冲突。
- **缺口 4**：`restore_git_metadata_from_bundle` 的 mixed reset 基线 = bundle 捕获的 HEAD（可能陈旧），fetch 对账只更新 remote-tracking refs 不 reset 工作树；与 P2 兜底 `restore_git_metadata_from_remote`（reset 到 fresh `origin/<branch>`）基线代际不一致。要查清「陈旧 index 基线」是否造成 git 视图 ≠ storage 视图的状态漂移。

---

## 1. 缺口 3：android_compile 双写者的 run-scoped manifest 漂移

### 1.1 已核实事实（read_file 核对，file:line 精确）

| 事实 | 位置 | 说明 |
|---|---|---|
| `_android_compile_outcome` 直接 docker 编译，不走 `_prepare_temp_workspace`/`flush_temp_workspace` | `agent_tools.py` L5287 起 | `backend.execute(work_dir=str(resolved_path))` L5391-5400 |
| 编译根 = 持久根 `WORKSPACE_ROOT/<agent_id>` | `agent_tools.py` L2835-2837 `_agent_workspace_root` | 非 run-scoped 临时工作区 |
| 成功后逐 apk best-effort 刷新 run-scoped | `agent_tools.py` L5434-5443 | 调 `_refresh_run_workspace_after_direct_write` |
| 刷新函数 best-effort，含 4 类 skip + 异常吞 | `agent_tools.py` L2388-2462 | skip：`no_run_id`(L2407) / `is_dir`(L2418) / `per_file_limit`(L2424, 50MB) / `version_invisible`(L2436)；异常只 `logger.warning` |
| 刷新最终走 `refresh_run_workspace_path` 直写 manifest | `agent_tools.py` L2453-2461 | 算 content_hash |

### 1.2 待研究问题

- **Q3.1 视图同构性**：docker build 的 `resolved_path`（`WORKSPACE_ROOT/<agent_id>/...`）与 execute_code 的 run-scoped temp workspace 是**同一条路径**还是**两条独立视图**？需读 `docker_backend.py` 的 work_dir 挂载映射 + `_prepare_temp_workspace`（L1891）的物化落点，判定 build 产物是否落在 run-scoped 视图内。
- **Q3.2 漏刷新场景**：4 类 skip（尤其 `no_run_id` 与 `version_invisible`）在真实 android run 里多常见？`_refresh_run_workspace_after_direct_write` 失败后，run-scoped manifest 与 storage 的分歧会维持多久、由谁兜底？
- **Q3.3 伪冲突风险**：manifest 陈旧时，下一次 `flush_temp_workspace` 是否会把「docker 直写的新 build 产物」误判成 concurrent 冲突丢弃（`conflict_mode` 见 `workspace_policy.publication_conflict_mode`）？这是不是 764eb591「物化翻转」的一个可能来源？
- **Q3.4 产物分类交互**：`classify_publish_path`（`workspace_policy.py`）对 `build/`=derived、`build/outputs/*.apk`=artifact；双写者直写的产物在 flush 时分类是否一致、会不会 derived/artifact 语义打架？

### 1.3 判定标准

- 若 Q3.1 证明两视图本就共享同一落点 → 缺口 3 收敛为「best-effort 刷新健壮性」问题，优先级降。
- 若 Q3.3 坐实「陈旧 manifest → 伪冲突丢弃」→ 真问题，最小修复方向 = 直写后刷新从 best-effort 升级为「冲突即重物化」或补 CAS（复用 CAS 方案文档缺口 1 的同一原语）。
- **参考项目方向（已执行，见 §3.1）**：双写者应带 run 代际（orca `runtimeFence` / LangBot `placement_generation`），代际不符的迟到写显式拒绝——「写前仲裁」而非「事后 best-effort 刷新」。

---

## 2. 缺口 4：git bundle 恢复的 reset 基线代际一致性

### 2.1 已核实事实（read_file 核对）

| 事实 | 位置 | 说明 |
|---|---|---|
| bundle 恢复：`git init` → `fetch <bundle>` → `symbolic-ref HEAD <branch>` → **`reset --mixed <branch>`** | `gitlab_workspace.py` L490-509 | `<branch>` 来自 `_resolve_bundle_branch`(L447-462)，映射 bundle 捕获的 HEAD |
| reset 后 **fetch origin 只更新 remote-tracking refs，不 reset 工作树** | `gitlab_workspace.py` L520-525 | 内联 `-c insteadOf` 认证，best-effort |
| P2 兜底 `restore_git_metadata_from_remote` reset 到 **fresh `origin/<branch>`** | `gitlab_workspace.py` L622 | 先 fetch 再 mixed reset，基线不陈旧 |
| 两者基线代际**不一致** | 对比 L509 vs L622 | bundle=冷缓存（可能落后 remote），remote=权威 |

### 2.2 待研究问题

- **Q4.1 基线代际**：bundle 的 HEAD（上次 flush 时的 git 状态）与 storage working tree（最新文件状态）谁更新？`reset --mixed <bundle branch>` 后，若 storage 文件比 bundle 新，`git status` 会显示什么？是否与 agent 实际做过的 edit 对不上（= 状态漂移，764eb591「分层错位」的另一面）？
- **Q4.2 对账不足**：bundle 路径的 `fetch origin`（L520）只更新 `origin/*`，不把 index/工作树 reset 到 fresh remote，是否等于「对账没做完」？是否应像 `restore_git_metadata_from_remote` 那样 reset 到 `origin/<branch>`？
- **Q4.3 因果定位**：764eb591 里「git status 全程 clean」的现象，与 restore 后的「一堆 modified/untracked」是**同一分叉的两面**，还是 restore 基线问题**根本不在 764eb591 因果链上**（当时是 edit_file 写入对 execute_code 沙箱不可见，属运行期视图问题，非 restore 期）？必须用证据判定，不能凭猜测把 restore 硬塞进因果链。
- **Q4.4 触发面**：bundle restore 在真实 run 里的触发频率（跨 session 冷启动 vs 同 session 热复用）？陈旧基线只在「bundle 落后 remote 且 working tree 有未 flush 改动」时暴露，这个交集多罕见？

### 2.3 判定标准

- 若 Q4.3 证据表明 restore 基线不在 764eb591 因果链上 → 缺口 4 降级为「冷启动体验优化」，非 764eb591 相关修复。
- 若 Q4.1/Q4.2 坐实「陈旧 index 基线造成 git 视图 ≠ storage 视图」→ 真问题，最小修复方向 = bundle 路径 reset 对齐 remote（复用 `_credential_rewrite` 内联认证 fetch 后 `reset --mixed origin/<branch>`），或统一 bundle/remote 两条 restore 的基线语义。
- **参考项目方向（已执行，见 §3.2/§3.3）**：bundle 恢复基线对齐「单一权威」（letta：git 即文件系统；herdr：显式权威优先级），或用内容哈希标识基线（opencode）——不 reset 到 bundle 陈旧 HEAD。

---

## 3. 参考项目对照结论（已执行 reference-check）

> 用 orca / LangBot / letta / herdr / opencode 五份整库研究 + 源码核验，回答「参考项目如何解决双写者漂移 + 基线漂移」。结论收敛为三条原则，每条都直接对应缺口 3/4 的答案方向。

### 3.1 原则一：单调代际 —— 拒绝陈旧写者，而非事后合并

- **orca `runtimeFence`**（单调计数）+ `expectedRuntimeFence` 四字段 envelope（`agent-session-wire.ts:164-217`）：写前校验 fence 是否最新代际，过期写入**在写之前拒绝**。
- **LangBot `placement_generation`**（`botmgr.py:56/646-670`）：`< previous_generation` 拒绝回滚；`assert_execution_active`（`:91`）陈旧执行体自灭，贯穿全链路。
- **落在缺口 3**：android_compile 的 docker 是「第二个写者」，参考项目会要求它**携带 run 代际**、代际不符的迟到写显式拒绝；Clawith 现在是 best-effort 事后刷新（4 类 skip + 异常吞），是「事后弱对账」而非「写前代际仲裁」。

### 3.2 原则二：单一权威 + 结构态/高 churn 分离

- **letta MemFS git 化**（`memory-git.ts` + `memory-git-config-lock.ts` 的 `withSerializedGitConfigMutation`）：文件系统就是 git 仓库，每次写 = commit，`git status` 天然反映真相——从根上消除「storage 层 vs git 层」分层错位（764eb591 病灶）。config-lock 防并发写。
- **herdr** 结构态/高 churn 态分离（`persist.rs:3-5`）+ 「hook 权威 + 屏幕 fallback」双源仲裁（`detect/mod.rs:10-20`）：一个权威源，fallback 只补缺。
- **落在缺口 4**：bundle（冷缓存）vs storage 工作树（真实现状）是「双重事实源」，参考项目要么合一（letta），要么显式定权威优先级（herdr）。

### 3.3 原则三：content-addressed + 原子写

- **opencode** 内容寻址快照：`snapshot.ts:98` 用 `Hash.fast(worktree)` 作快照目录名、`:219-222` 按 `Git.TreeID` checkout——状态用内容哈希标识，恢复没有「陈旧基线」概念（基线 = 内容哈希）。
- **herdr** 原子写：`io.rs:48-61` 先写 `.tmp` 再 `rename`。
- **落在缺口 4**：bundle 恢复基线应是「工作树内容哈希」或「remote 权威」，而非 bundle 捕获时的陈旧 HEAD。

### 3.4 关键发现（喂给研究问题）

1. **Clawith 自相矛盾**：20260904 文档写明「bundle=冷启动缓存，remote 仍权威」，但 `restore_git_metadata_from_bundle` L509 却 `reset --mixed <bundle branch>`——reset 到 bundle 陈旧 HEAD，代码没执行「remote 权威」。（→ Q4.1/Q4.2 答案方向已明确：对齐 remote）
2. **缺口 3 是 LangBot 期望态对账的弱化版**：LangBot `reconcile_projected_workspaces` + `InstallationBinding` 用「期望态 + 代际 + digest 三件套」对账，Clawith 对应物只有「skip 条件 + 异常吞」。（→ Q3.3 答案方向：双写者带代际+digest，而非 best-effort）

**一句话**：参考项目共同答案是「**让陈旧写者写不进去**（单调代际 / 内容寻址），而不是写完再对账（best-effort refresh / fetch 对账）」。

---

## 4. 研究方法

1. **read_file 核对**（首选）：`docker_backend.py` 的 work_dir 挂载、`_prepare_temp_workspace` 物化落点、`run_workspace.py` 的 `refresh_run_workspace_path`/`close_run_workspace`、`workspace_policy.py` 分类。
2. **PG / Langfuse 证据**（只读）：查 `agent_tool_executions` 里 android_compile + execute_code 共存的 run，看 `result_metadata` 的 content_hash / manifest 漂移；查 bundle restore 触发后的 `git status` 输出形态。
3. **reference-check** ✅ 已执行（结论见 §3）：对照 letta（整仓 git commit 原子）/ orca（expectedRuntimeFence）/ herdr（结构态+版本化+原子写）/ opencode（content-addressed 快照）/ LangBot（placement_generation）的「单一权威 + 单调代际」，判定「双写者」与「双基线」是否违反共同原则。
4. **prototype（必要时）**：本地 git 实跑 bundle restore 后 `git status` 的形态，验证 Q4.1 的「陈旧 index vs 新工作树」输出（成本低、可直接证伪）。

---

## 5. 交付物与退出标准

**交付物**：`docs/analysis/20260905-workspace-view-divergence-research.md`，逐缺口给出：

- 「真问题 / 假警报」判定 + 证据链（file:line + PG/Langfuse 样本 + prototype 输出）。
- 若真问题：触发条件、影响面（哪些 run / 什么规模）、最小修复方向（只到方案，不实施）。
- 若假警报：记录否定证据，防止重复立项。

**退出标准**：两个缺口各有一个可拍板的判定；真问题则附带「是否进入 CAS 方案文档同一 tdd 实施轨道」的建议。

---

## 6. 红线

- 只读调研：不改代码、不写库、不部署、不重启容器。
- 不凭记忆写函数名/参数名/行号；所有结论 read_file 或证据核对。
- DB/Redis 只读；测试环境不灰度。
- 产出停在「判定 + 修复方向」，进入实施需另行拍板。
