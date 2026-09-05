# 技术方案：workspace 编辑数据回退防线（flush 区分「有意改动」vs「git 回退到 HEAD」）

- **关联根因**：`docs/analysis/2026-09-05-workspace-revert-rootcause5.md`
- **状态**：已实读代码核对（见「已核对代码事实」），待实现。
- **范围**：只做根因文档 §五「方向 1」（flush 防回退）；方向 2/3/4/5 挂起。

---

## 一、问题

execute_code 沙箱内有一个「第二真相源」：从 git bundle/remote 恢复的 `.git` 仓库。模型在沙箱内跑 `git checkout/reset` 会把工作树文件回退到 git HEAD（一个**从未包含 edit_file 未提交改动**的旧提交）。flush 时 `_collect_temp_workspace_files` 把这些「回退后」的文件当成 cas_files，`base_hash`（=edit 后 storage 哈希）≠ `current_hash`（=git HEAD 哈希）→ 误判为「模型有意改动」→ CAS 写回 storage → **storage 上的编辑数据被回退**。

CAS `version_match` 守卫失效的本质：git 操作只改沙箱文件、不推进 storage 版本 token，所以 flush 时 token 仍匹配，`base_hash` 比较又无法区分「有意改动」与「git 回退」。

## 二、判据（核心）

对每个 cas_file（source 类文件），若同时满足：

1. **沙箱文件内容 == git HEAD 版本**（`current_hash == git_head_hash`，sha256 逐字节相等）；
2. **storage 当前内容 != git HEAD 版本**（`storage_hash != git_head_hash`，实时读 storage 确认）。

→ 判定为「回退」，**拒绝发布 + 告警**（`[WorkspaceFlushRevertGuard]`），路径进返回值的 `reverted` 列表。

判据 1 用「沙箱 == HEAD」而非「沙箱 == base_hash」的原因：回退后沙箱文件恰好等于 HEAD，而 base_hash 是 edit 后的值——两者必不等，这正是被误判的根子。

判据 2 用**实时读 storage**而非 `entry.base_hash` 快照：`base_hash` 只在物化/refresh 时更新，若 edit_file 直写 storage 后 refresh 未同步（方向 4 的 `no_run_id` 变体），`base_hash` 会是陈旧的。实时读保证「storage 此刻真的 != HEAD」才拦，避免漏拦，也避免把「storage 本就 == HEAD」的合法 no-op 误拦。

**有意回退（模型显式 `git checkout -- file`）** 与 bug 签名相同，会被同样拦截。这是**可接受的取舍**：模型想回退时改走 edit_file（直写 storage，本防线不覆盖 edit_file 路径），数据保护优先。

## 三、实现落点

### 3.1 物化时捕获 git HEAD 树哈希（一次，per run）

- 新函数 `gitlab_workspace.capture_head_tree_hashes(repo: Path, scratch_dir: Path) -> dict[str, str]`：
  - `git archive -o <scratch>/head.tar HEAD`（argv 数组，经 `_run_git`，输出写文件、不经 stdout，**规避 `_run_git` 的 4096 字符截断**）；
  - 用 Python `tarfile` 解包，对每个 regular file 计算 `content_hash_bytes`（sha256，与 flush 端同口径）；
  - 返回 `{repo 相对路径 → sha256}`；任何 git 失败返回 `{}`（best-effort，防线静默失效，不影响物化）。
- `_prepare_temp_workspace`（`agent_tools.py:1892`）在 `restore_git_metadata_from_remote` 之后，遍历 `gitlab_workspace._iter_repo_copies(temp_ws)`，把 repo 相对路径拼成 `normalize_workspace_path(f"{repo_rel}/{path}")`（与 cas_files 的 rel_path 同命名空间），聚合成 `git_head_hashes: dict[str, str]`。
- `TempWorkspace` dataclass（`agent_tools.py:1769`）新增字段 `git_head_hashes: dict[str, str] = field(default_factory=dict)`（带默认值，不破坏既有测试构造）。

### 3.2 flush 时拦截

`flush_temp_workspace`（`agent_tools.py:2099`）cas_files 循环，在 `storage_key` 计算后、`conflict_mode == "overwrite"` 分支前，插入：

```python
git_head_hash = git_head_hashes.get(rel_path)
if entry is not None and git_head_hash is not None and current_hash == git_head_hash:
    storage_hash = await _current_storage_hash(storage, storage_key)
    if storage_hash is not None and storage_hash != git_head_hash:
        reverted.append(rel_path)
        logger.warning(
            "[WorkspaceFlushRevertGuard] run_id={} agent_id={} path={} "
            "sandbox_hash={} git_head_hash={} storage_hash={}",
            run_id, temp_workspace.agent_id, rel_path,
            current_hash, git_head_hash, storage_hash,
        )
        continue
```

- `git_head_hashes = temp_workspace.git_head_hashes`（`getattr` 带默认 `{}` 兜底，兼容 run_workspace 协议对象）。
- 新 helper `_current_storage_hash(storage, storage_key) -> str | None`：`read_bytes` + `content_hash_bytes`，异常/空返回 `None`（不拦）。
- 拦截只 `continue`（跳过该文件写入），**不**进 `conflicted`（避免触发 workspace conflict breaker / stale discard 连锁），不 early-return——其余文件正常发布。
- 三个 `return` 点统一加 `"reverted": reverted`。

## 四、边界与取舍

| 场景 | 行为 | 依据 |
|---|---|---|
| 模型有意改文件（沙箱 != HEAD） | 正常发布 | 判据 1 不满足 |
| git checkout/reset 回退 edit（bug） | 拦截 + 告警，storage 保留 edit | 判据 1+2 满足 |
| 模型显式回退到 HEAD（同签名） | 拦截（可接受） | 数据保护优先，改走 edit_file |
| storage 本就 == HEAD，沙箱 == HEAD | 被 `base_hash` 跳过（早于 guard） | 无数据可回退 |
| 物化时 git 不可用 / 无 repo | `git_head_hashes` 空，防线静默失效 | best-effort，不阻断物化 |
| 文件是 artifact（L3）| 不覆盖（guard 只在 cas_files 循环）| artifact 是 LWW 语义 |

## 五、测试策略（tdd 红绿）

1. **红**：`test_workspace_flush_revert_guard.py`（新）：
   - `capture_head_tree_hashes`：真实 `git init + commit + file`，断言返回 `{path: sha256}`，且非 tracked 文件不在其中。
   - flush 拦截：构造 TempWorkspace（manifest base_hash=edit 版，`git_head_hashes[path]=HEAD 版`，沙箱文件写 HEAD 版），storage 写 edit 版；断言 flush 后 storage 仍为 edit 版、`reverted` 含 path、无 `updated`。
   - 不误拦：沙箱文件 != HEAD 版 → 正常发布进 `updated`；storage == HEAD 版时（base_hash==current_hash）→ 走 skip。
2. **绿**：实现 3.1/3.2。
3. **回归**：`test_workspace_publication_filter.py`、`test_workspace_reconciliation.py`、`test_agent_tools_storage_workspace.py`、`test_agent_tools_typed_content_outcomes.py` 全过（TempWorkspace 新字段带默认值，不应破坏构造）。

## 六、验证清单

- `cd backend && .venv/bin/python -m pytest tests/test_workspace_flush_revert_guard.py -q`
- 全量 `pytest -q`（或受影响子集）
- `ruff check`（**禁 `ruff format`**）
- `scripts/arch-guard.sh`

## 已核对代码事实（读实，禁凭记忆）

- `content_hash_bytes` = `sha256(data).hexdigest()`（`storage_runtime/base.py:144`）。
- `_run_git` 返回 `(rc, stdout, stderr)`，stdout 截断 4096 字符 + redact（`gitlab_workspace.py:96-126`）→ 读 blob 内容不能走它。
- `_iter_repo_copies` 迭代 `root/workspace/*` 下含 `.git/HEAD` 的 repo（`gitlab_workspace.py:318`）。
- `TempWorkspace` 字段在 `agent_tools.py:1769`；构造点 `_prepare_temp_workspace` L1938 + 测试 5 处（都用关键字参数）。
- `flush_temp_workspace`：cas_files 循环 L2167-2275，`base_hash==current_hash` 跳过 L2171，`storage_key` L2179，CAS 写 L2192；三个 return L2258/L2348/L2379。
- `_collect_temp_workspace_files` 返回 `(cas_files, overwrite_files, derived_paths, git_repos)`（L2696）；`classify_publish_path` 对 source→cas（`workspace_policy.py:57`）。
- `redact_git_secrets` 只 redact `.git` 元数据，source 文件原样返回（`workspace_policy.py:32-54`）→ 沙箱/HEAD/storage 三端 sha256 同口径。
- `dataclass, field, replace` 已在 `agent_tools.py:17` 导入；`gitlab_workspace` 已导入 L123；`normalize_workspace_path` 已导入 L86。
