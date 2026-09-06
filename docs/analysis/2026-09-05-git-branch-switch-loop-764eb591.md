# Git 多分支状态漂移：为什么 agent 频繁切 git（run 764eb591 · 放大因素 2）

> 2026-09-05 · 承接 `2026-09-05-compaction-amnesia-764eb591.md`（五层根因）。本文单点深挖其中的「放大因素 2」：execute_code 20 次全是 git status/branch/checkout/fetch，在 `f_android_ai ↔ fix/p1-repeating-display ↔ backup/f_android_ai-2b6077f` 三个分支间来回 checkout，`Calculator.kt` 的 content_hash 在两个稳定值间横跳。
>
> 取证源：`agent_tool_executions`（execute_code 全量 result_summary + read_file 的 result_metadata->>'content_hash'）、`workspace_file_revisions`、`agent_run_events`（均 PG 只读）。

## 一、结论先行（一句话）

**模型把 git 当「找回自己改动」的救命稻草，但这根稻草是断的：`edit_file` 写的是 storage 层，`execute_code` 的 git 视图永远 clean，切分支又触发 re-materialize 让文件内容翻转——于是模型在三个分支间来回 checkout，越切越「看不到」自己的改动。**

「频繁切 git」不是模型犯傻，而是模型在**正确地走 GitLab 规范流程（建分支→改→提交→MR）**时，被一个「编辑层与 git 视图分层 + 物化时内容翻转 + 压缩失忆」的系统缺陷反复打断、反复从头确认。切 git 是**果**，分层/翻转/失忆是**因**。

## 二、20 次 execute_code 全量时间线（铁证）

| 时刻(UTC) | 动作 | 关键输出 |
|---|---|---|
| 12:13:53 | 方向确认 | `=== branch === f_android_ai`，`=== status ===`（空=clean），HEAD 2782458 |
| 12:21:27 | 方向确认（紧跟第1次压缩 12:21:27） | f_android_ai clean |
| 12:22:22 | 方向确认 | `git branch` 首次列出 `backup/f_android_ai-2b6077f 2b6077f Initial commit` |
| 12:24:58 | 方向确认 + ls-remote + files | 列全 23 个文件 |
| 12:27:40 | 方向确认 + probe | — |
| **12:29:16** | **checkout -b fix/p1-repeating-display** | `Switched to a new branch 'fix/p1-repeating-display'` |
| **12:30:25** | **checkout f_android_ai**（70 秒后切回） | `Switched to branch 'f_android_ai'`，`open MRs === []` |
| 12:34:39 | 分支清点 | 3 分支全在，`* f_android_ai`，HEAD 2782458 |
| 12:35:28 | fix/p1-repeating-display log + probe | — |
| 12:36:58 | 方向确认（紧跟失忆重做 edit 12:36:31） | f_android_ai clean |
| 12:37:15 | **reflog 铁证** | `checkout: fix/p1-repeating-display→f_android_ai`、`checkout: f_android_ai→fix/p1-repeating-display`、`reset: moving to f_android_ai` |
| 12:38:06 | fetch + origin 比对 | local==remote 2782458 |
| 12:42:13 | 方向确认（紧跟压缩 12:42:14） | — |
| 12:42:41 | 方向确认 + branch -vv + reflog | 3 分支同指 2782458 |
| 12:45:08 | ls-remote 全量 MR | merge-requests 1..7 全列 |
| 12:46:21 | fetch main + MR 列表 | 7 个 MR 全 merged，`2782458 ancestor of main? YES-in-main` |
| **12:48:26** | **checkout -b fix/p1-display-fixedpoint** | `Switched to a new branch 'fix/p1-display-fixedpoint'`（**同一 P1 又建一个分支！**） |
| 12:49:36 | 分支清点 | `* fix/p1-display-fixedpoint`，`working tree clean` |
| **12:50:20** | **checkout f_android_ai** | `Switched to branch 'f_android_ai'`，`working tree clean` |
| 12:53:43 | 最终方向确认 + token/ls-remote/refs | 6 个本地 ref 全列 |

### 三个致命事实

1. **同一个 P1 任务，模型建了两个分支**：12:29:16 `fix/p1-repeating-display`，19 分钟后 12:48:26 又建 `fix/p1-display-fixedpoint`（命名都变了）。中间隔了 12:36:31 / 12:42:14 / 12:48:49 三次压缩 → **失忆的铁证**：它不记得自己已经建过分支。
2. **两个 fix 分支都是空分支**：`branch -vv` 显示 `fix/p1-repeating-display 2782458`、`fix/p1-display-fixedpoint 2782458`——与 `f_android_ai` 同一个 commit，**一个 commit 都没往上提交过**。模型「建了分支 → 改文件 → git status 永远 clean → 以为没改成 → 切走/再建分支」。
3. **git status 全程 clean**：即使紧跟在失忆重做 edit（12:36:31）之后 27 秒（12:36:58），`git status` 仍是空。edit_file 的写入对 execute_code 的 git 视图**完全不可见**。

## 三、content_hash「两稳值 + 30+ 一次性」的真相

`Calculator.kt` 的 content_hash 来自 `agent_tool_executions.result_metadata->>'content_hash'`（read_file 工具），**不是** `workspace_file_revisions`（那里只有 2 条 edit：`f6bf8c18` 12:18:35、`7e7e637d` 12:36:31）。read_file 全量分布：

| content_hash(前8) | 次数 | 首见 | 末见 |
|---|---|---|---|
| **a8e4f167** | 12 | 12:02:30 | 12:57:29（贯穿全 run） |
| **a721b8ed** | 3 | 12:02:50 | 12:48:32 |
| b5751819 | 2 | 12:29:19 | 12:34:39（**紧跟 checkout -b fix/p1-repeating-display 12:29:16**） |
| 其余 ~35 个 | 各 1 | 散落 | 散落 |

### 关键推论

- **a8e4f1↔a721b8 在 12:02–12:04 就横跳**（首 edit 12:18:35、首 execute_code 12:13:53 之前）→ **内容漂移是 pre-existing**，不是本 run 的 edit/checkout 造成的。模型一进场就继承了一个「每次读都变」的不稳定工作区。
- **`b5751819` 紧跟 checkout -b 之后 3 秒出现** → 切分支直接触发了内容翻转，产生了一个既非 a8e4f1 也非 a721b8 的新态。
- **~35 个一次性哈希** = edit_file 的写入 + 每次 re-materialize 产生的中间态，写完即被回退/覆盖，从不复用。

**这就是「切一次分支，改动就消失一次」的量化证据**：模型 edit → 短暂出现新 hash → 某处回退/物化 → 回到 a8e4f1 稳定态 → 模型重读看到「没变」→ 以为改动丢了。

## 四、因果链：为什么模型「反复确认、反复切、反复做」

```
规范 git 工作流（建分支→改→commit→MR，本工程 ls-remote 里 MR 1..7 全是这个套路）
        │
        ▼
模型 edit_file 改文件（写 storage 层）
        │
        ▼
模型 execute_code 跑 git status 验证  ← 它的「验证神谕」
        │
        ├─ git 视图与 storage 分层 → 永远 clean / HEAD 2782458
        ├─ 切分支 → re-materialize → content_hash 翻转（b5751819 等）
        └─ 压缩 → 失忆（不记得建过 fix/p1-repeating-display）
        │
        ▼
「我的改动没落地 / 丢了」的信念被坐实
        │
        ├─ 切回 f_android_ai「找回」→ 又翻
        ├─ 再建一个分支（fix/p1-display-fixedpoint）
        └─ 重新 edit 同一批文件（12:36:31 重做）
        │
        ▼
回到第一步，自激闭环
```

**三个「因」逐个对应**：

1. **分层错位（edit_file ↔ execute_code 沙箱）**：这是「永远看不到改动」的第一因。见 `compaction-amnesia-764eb591.md` §根因 5 的铁证（`git status` 全程 clean、edit 的 `before_len` 回退签名）。模型唯一能确认「我改过没有」的客观手段（git status/log）报出来的是「没改」。
2. **物化内容翻转（checkout → re-materialize）**：切分支是模型「找回改动」的动作，但它恰好是触发内容翻转的动作——`b5751819` 紧跟 checkout -b 出现。**模型的解药就是毒药**。
3. **压缩失忆（completed_actions 管道太小 + 摘要漂移）**：每次压缩抹掉「我建过分支、改过文件」的事实，模型回到「从零确认当前状态」的起点，而确认手段（git）又是坏的。

三者叠加成「**失忆 → 查 git 找回 → git 显示 clean/内容翻转 → 怀疑改动丢 → 切分支 → 再翻转 → 更乱 → 重新编辑 → 又失忆**」的漂移循环。这正是 `compaction-amnesia` 文档说的「漂移循环」（prefix 每步变），精确匹配熔断器结构性抓不到。

## 五、为什么这对「根治」有意义（与 17 项目研究接轨）

「git status/branch/checkout/fetch 不改变 workspace 材料状态」在 `2026-09-05-agent-loop-rootcause-cross-project-study.md` §四已被列为「零材料进度，天然命中」。本文补上了**为什么它会发生**的机制证据：

- 模型切 git 不是漫无目的的绕圈，而是**在一个「验证工具与真实状态脱节」的环境里，反复尝试用坏掉的工具找回状态**。
- 所以单纯的「git 命令折叠归一」或「git 巡检连续 N 次熔断」只是压症状；**根治要修状态通道本身**：让 read_file/execute_code 读到的内容、git status 看到的改动、edit_file 写入的内容三者一致（即 `workspace_file_revisions` 反映的「材料进度」必须与 execute_code 沙箱视图同源）。
- 反过来，在状态通道修好之前，**「同一路径 content_hash 在无 edit 的情况下横跳」本身就是最灵敏的漂移信号**——比「git 命令重复」更早、更准，可直接喂给「零材料进度 / 证据增益」计分器。

## 七、参考项目怎么解（实读本地源码，非摘要）

「编辑层 vs git 视图分层 + 物化内容翻转 + 状态漂移」这一族问题，参考项目的解法收敛成**一个共同原则：单一权威 + 单调代际 + 原子写 + 文件系统 git 化**。四个对症范式：

| 子问题 | 正解范式（项目：机制） | 真实代码位置 |
|---|---|---|
| 分层错位（edit 对 git 不可见） | letta **MemFS git 化**：文件系统即 git，每次写 commit，git status 天然可见，无「编辑层 vs git 视图」两层 | `letta-code/src/agent/memory-git.ts`（头部注释 + `commitMemoryWrite` L1340/`commitMemoryPaths` L1213/「uncommitted changes → 拒绝用 memory tools」L1273）、`memory-git-config-lock.ts`（`.git/config.lock` 独占 + 重试防并发写） |
| 物化翻转（陈旧快照覆盖新编辑） | orca **runtimeFence 单调代际**：每次 mutation 带 `expectedRuntimeFence`，过期代际写入被拒，绝不静默覆盖 | `orca/src/shared/agent-session-wire.ts` L164-217（四字段 envelope + `agent_session_checkpoint_stale` 拒绝码 + 回传 `currentFence`）、`agent-session-record.ts` L354（`head.mintedAtFence === lease.runtimeFence`） |
| 编辑数据回退（持久化版） | herdr **结构态分离 + 版本化 + 原子写 + 未来版本拒绝** | `herdr/src/persist.rs`（`session.json` 结构态 vs `session-history.json` 高 churn 态分离）、`persist/snapshot.rs` L447-456（`raw.version > SNAPSHOT_VERSION → Err`）、`persist/io.rs` L48-61（`.json.tmp` + `rename` 原子写） |
| git 是「坏神谕」 | opencode **content-addressed 文件快照**：独立 git 仓库做第二事实源，`files()/diff()` 交叉验证真实文件改动 | `opencode/packages/core/src/snapshot.ts`（`gitDirectory = global.data/snapshot/<project.id>/<hash(worktree)>`，capture/files/diff/restore） |

**共同点**：文件系统是唯一权威，git 只是视图（SWE-agent「视图变换不污染真相源」、gptme「master context 单层 append-only」同义）；陈旧状态永远拒绝而非覆盖（orca/lett/herdr 一致）；模型永远可重读文件自纠，而非信一个可能撒谎的 git status。

**对 Clawith 的直接含义**：①`edit_file` 直写后应立即落进 execute_code 沙箱可见的 git 视图（letta 方向），或至少给 read_file/execute_code 一个 content-addressed 第二事实源（opencode 方向）；②workspace 物化/回退路径加**单调代际**（orca runtimeFence），陈旧快照的物化写入被拒，堵死「b5751819 那种 checkout 后内容翻转」；③`workspace_file_revisions` 已是「材料进度」账本雏形，缺的就是「写前校验代际 + 过期拒绝 + 原子发布」。

## 六、证据出处

- `agent_tool_executions`（run_id=764eb591-a38a-48cb-9120-23c83c3b0bda）：execute_code 20 条 result_summary 全文、read_file result_metadata->>'content_hash' 分布。
- `workspace_file_revisions`：Calculator.kt 仅 2 条 edit（12:18:35 →f6bf8c18、12:36:31 →7e7e637d，before_len 均 11134）。
- 分支/commit 关系：`f_android_ai`/`fix/p1-repeating-display`/`fix/p1-display-fixedpoint` 同指 2782458；`backup/f_android_ai-2b6077f` 指 2b6077f「Initial commit」（另一根，本 run 前遗留）。
- reflog 铁证（execute_code 12:37:15）：`checkout: fix/p1-repeating-display→f_android_ai`、`checkout: f_android_ai→fix/p1-repeating-display`、`reset: moving to f_android_ai`。
