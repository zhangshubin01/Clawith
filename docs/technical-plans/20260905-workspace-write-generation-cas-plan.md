# 方案：workspace 写入单调代际 —— edit_file/write_file「漏 CAS」的精确补齐

> **状态**：方案（未实施）。只到「改点清单 + tdd 草图」，动手前需评审。
> **上游分析**：`docs/analysis/2026-09-05-git-branch-switch-loop-764eb591.md`（run 764eb591 三分支 ping-pong checkout 根因）。
> **姊妹研究**：`docs/technical-plans/20260905-run-state-generation-study.md`（run 所有权代际，claim/checkpoint/lease 三处仲裁）。本方案管的是 **workspace 文件代际**，与它互补、不重复。
> **红线**：测试环境不灰度（2026-08-30）；DB/Redis 只读；后端改动前跑 `scripts/arch-guard.sh`。

---

## 0. TL;DR

Clawith **已经实现了参考项目 ~90% 的原语**（单调 `version_token` CAS + `content_hash` 内容寻址 + 原子 git bundle + 冲突即丢弃重物化）。问题不是「缺单调代际」，而是「**漏 CAS**」——CAS 原语都在，但两个最高频的写路径 `edit_file` / `write_file` 绕过了它，做的是 check-then-act 的无条件写。

**唯一 P0 改点**：`edit_file` 读文件时捕获 `StorageVersion.token`，写时把它作为 `expected_version_token` 传给 `write_workspace_file`（后者 L595-596 已支持），冲突时对模型返回「文件已变，请重新读取后再改」而不是静默 clobber。约 10 行改动，tdd 可测，直接对应 orca 的 `expectedRuntimeFence`。

---

## 1. 重构结论：原语已存在，缺口是「漏 CAS」

| 参考项目范式 | Clawith 对应原语 | 位置（已 read_file 核实） | 状态 |
|---|---|---|---|
| orca `expectedRuntimeFence` 过期拒绝 + 回传 currentFence | 单调 `version_token` CAS：`WriteCondition.version_token` + `write_bytes_if_match` / `delete_if_match` | `backend/app/services/storage_runtime/base.py` L38-41、L102-135 | ✅ 已实现 |
| opencode content-addressed 快照 | `content_hash_bytes` = sha256；manifest 条目记 `base_hash`/`content_hash` | `base.py` L144-145；`run_workspace.py` `refresh_run_workspace_path` | ✅ 已实现 |
| herdr 结构态分离 + 版本化 + 原子写 | storage 层 `StorageVersion.token`（`version_id or etag or content_hash or modified_at:size`）+ `write_bytes_if_match` 原子写 | `base.py` L22-35、L102-117 | ✅ 已实现 |
| letta memory-git 每次写 commit、git status 天然可见 | `record_revision` + git bundle 物化 | `workspace_collaboration.py` L612-623 | ✅ 已实现 |
| （orca 过期 fence）冲突即丢弃重物化 | `close_run_workspace` 冲突即丢弃重物化 | `run_workspace.py` L121-138 | ✅ 已实现 |

**唯一没接上的线**：`StorageVersion.token` 在「读」的时候没有被「写」的路径带上。参考项目里 fence/token 是 read→write 全程携带的；Clawith 只有 `flush_temp_workspace`（`cas_files` 用 `write_bytes_if_match`）、`append` 模式、`move`/`delete` 携带了它，而 `edit_file` / `write_file`（overwrite）没有。

---

## 2. 精确缺口清单

### 缺口 1（P0，最高价值，小改）：`edit_file` check-then-act 竞态

`backend/app/services/agent_tools.py` `_execute_workspace_mutation` 的 `edit_file` 分支（L3671-3721）：

- L3692 `content = await storage.read_text(storage_key, ...)` —— **读，但没有捕获 `StorageVersion.token`**。
- L3701-3712 `write_workspace_file(...)` —— **不传 `expected_version_token`**。
- → `workspace_collaboration.py` L592 `condition = None` → L599-604 `write_bytes_if_match(condition=None)` → **无条件写**。

这是经典 check-then-act 竞态：模型读文件（看到 old_string），期间另一写者（并发 run、被部署杀掉后重放的 stale run、人机锁之外的直接写）改了同一文件，模型再写时把别人的改动 clobber 掉。这正是 764eb591 分析里「物化翻转 / 状态漂移」在 storage 层能落地的、可修复的那一刀。

**修复**（约 10 行，改 `edit_file` 一个分支）：

```python
# 现状
content = await storage.read_text(storage_key, encoding="utf-8", errors="replace")
...
write_result = await write_workspace_file(..., content=new_content, ...)  # 无 token

# 改后
content = await storage.read_text(storage_key, encoding="utf-8", errors="replace")
ver = await storage.get_version(storage_key)          # 捕获 token
...
write_result = await write_workspace_file(
    ..., content=new_content, expected_version_token=ver.token,
)
```

冲突时 `write_workspace_file` L605-606 已返回 `WorkspaceWriteResult(False, path, "Conflict detected while writing ...")`，`edit_file` L3720 会把它转成 `❌ ...` 返回给模型。可选增强：冲突消息里附上「文件已被并发修改，请重新 read_file 后再 edit」的提示 + 当前 `content_hash`，让模型重读（对齐 orca 回传 currentFence）。

**TOCTOU 注意**（实现时决定，方案先写清）：`read_text` 与 `get_version` 是两次独立调用，中间文件可能变化，导致「读到旧 content + 捕获到新 token → 拿新 token 写旧 content」成功 clobber。稳妥做法是 **读前捕获 token、读后再比对**（乐观锁标准读）：

```python
ver_before = await storage.get_version(storage_key)
content = await storage.read_text(...)
ver_after = await storage.get_version(storage_key)
if ver_before.token != ver_after.token:
    return "❌ file changed while reading, retry"
# 写时传 ver_after.token；写侧 CAS 再兜底一次「读之后、写之前」的变化
```

最低限度（如果不想三次往返）也至少要「读后捕获 token」——写侧 `write_bytes_if_match` 的内部 `get_version`（base.py L110）仍能兜底「捕获后、写前」的变化。

**key 一致性已核实**：`edit_file` 读用的 `_tool_storage_key`（agent_tools.py L9624-9633，返回 `normalize_storage_key(f"{agent_id}/{normalized}")`）与 `write_workspace_file` 写的 `join_storage_key(agent_id, normalized)`（workspace_collaboration.py L572）对同一 path 产同一 key，token 可直接对齐。

### 缺口 2（P1，语义澄清，非缺陷）：`write_file` overwrite = 故意 LWW

`write_file` 的 overwrite 模式不传 token（agent_tools.py L3591-3603），这是**有意为之的 last-writer-wins**（新建/整文件覆盖的语义）。`append` 模式**已经**带 CAS（workspace_collaboration.py L597-598 `condition = WriteCondition(version_token=current_version.token)`）。

**结论**：不改代码。在 `write_workspace_file` docstring / 相关注释里补一句「overwrite 是 LWW，需要乐观锁请用 edit_file 或传 expected_version_token」，消除未来误判。真正需要 CAS 的「读-改-写」场景只有 `edit_file`，由缺口 1 覆盖。

### 缺口 3（候选，未完全确认）：android_compile 双写者协调的 manifest 漂移

`_android_compile_outcome`（agent_tools.py L5287 起）不走 `_prepare_temp_workspace`/`flush_temp_workspace`，而是 docker `backend.execute` 直接对 `resolved_path` 编译，成功后用 `_refresh_run_workspace_after_direct_write`（L5440-5443）**best-effort** 刷新 run-scoped manifest。这是 ADR 0011 + P0 L3 的「双写者」设计，已有 LWW 兜底。

**是否要动**：属于「两套物化视图（run-scoped vs per-call）内容分歧」的已知设计取舍，非本轮根因。**建议单独立项**，不在本方案范围内。

### 缺口 4（候选，未完全确认）：`restore_git_metadata_from_bundle` mixed reset 基线是否陈旧

git bundle 恢复时的 reset 基线若陈旧，会让 execute_code 的 git 视图与 storage 视图再分叉（764eb591 分层错位的另一面）。**未读完，标记为待核实**，不在本方案改动范围。

---

## 3. 推荐改点排序

| 优先级 | 改点 | 改动量 | tdd | 说明 |
|---|---|---|---|---|
| **P0** | 缺口 1：`edit_file` 传 `expected_version_token` | ~10 行 | ✅ | 直接对应 orca `expectedRuntimeFence`，修复 check-then-act clobber |
| P1 | 缺口 2：overwrite=LWW 语义文档化 | 注释 | — | 澄清，不改逻辑 |
| 后续 | 缺口 3 / 4 | — | — | 单独立项，不在本轮 |

### 3.1 缺口 1 tdd 草图

现有测试基建已齐备（`tests/test_agent_tools_storage_workspace.py`）：
- `MemoryStorageBackend` + `_patch_storage`（L973-981，同时 patch `agent_tools` 与 `workspace_collaboration` 的 `get_storage_backend`，解决「两模块 key 混用」）
- `_materialize_run_workspace`（L984-1009）+ `sandbox_run_scope_id`
- 已有 CAS 单测可参照：`test_write_workspace_file_fails_on_expected_version_conflict`（L722）、`test_write_workspace_file_appends_with_version_guard`（L371）、`test_write_workspace_file_append_does_not_overwrite_a_concurrent_change`（L428）

新增用例（red）：

1. `test_edit_file_conflicts_when_file_changes_after_read`：
   - 物化 run workspace，`edit_file` 改 `notes.txt`。
   - 关键：在 `edit_file` 内部「读之后、写之前」注入一次并发写（用 `MemoryStorageBackend` 直接改 `storage.files[storage_key]`，或 monkeypatch `storage.get_version`/`read_text` 触发第二次变更）。
   - 断言：`_execute_workspace_mutation` 返回「冲突/文件已变」，且 storage 内容 = 并发写者的新内容（**未被 clobber**）。
2. `test_edit_file_succeeds_without_concurrent_change`：无并发写时正常返回 `✅ Replaced 1 occurrence(s)`。
3. （TOCTOU 版）`test_edit_file_detects_change_during_read`：若采用「读前+读后比对」，断言读到中途变化的文件被拒绝。

实现（green）后跑全量 `test_agent_tools_storage_workspace.py` + `scripts/arch-guard.sh`。

---

## 4. 映射参考项目（为何这样改是对的）

| 参考项目 | 范式（file:line，见分析文档 §七） | Clawith 对应动作 |
|---|---|---|
| **orca** | `expectedRuntimeFence` 过期拒绝 + 回传 `currentFence`（`orca/src/shared/agent-session-wire.ts` L164-217） | `edit_file` 传 `expected_version_token`；冲突时回传当前 `content_hash` + 「请重读」提示 |
| **letta** | 每次写 commit，git status 天然可见（`letta-code/src/agent/memory-git.ts`） | 已由 `record_revision` + git bundle 覆盖；本轮不重复 |
| **herdr** | 结构态分离 + 版本化 + 原子写（`herdr/src/persist.rs` / `snapshot.rs` L447-456 / `io.rs` L48-61） | 已由 storage 层 `StorageVersion.token` + `write_bytes_if_match` 覆盖 |
| **opencode** | content-addressed 快照独立 git 目录（`opencode/packages/core/src/snapshot.ts`） | 已由 `content_hash_bytes` + manifest `base_hash`/`content_hash` 覆盖 |

一句话：四个参考项目的共同原则「单一权威 + 单调代际 + 原子写 + 内容寻址」，Clawith 的 storage 层都已落地；本轮唯一要补的是把「单调代际」从 `flush`/`append`/`move`/`delete` 延伸到 `edit_file`——正是 orca `expectedRuntimeFence` 的 workspace 文件版。

---

## 5. 红线与后续步骤

**不改的红线**：
- 不碰 DB/Redis 写；不动 sandbox 灰度；不重构 `run_workspace.py` / `gitlab_workspace.py` 的物化结构（缺口 3/4 单独立项）。
- 保持 `write_file` overwrite 的 LWW 语义（缺口 2 只加注释）。

**后续步骤（需用户评审后推进）**：
1. **方案评审**：确认缺口 1 的 TOCTOU 取舍（读前+读后比对 vs 读后捕获）。
2. **tdd**：按 §3.1 先 red（冲突用例），再 green（约 10 行改动）。
3. **验证**：跑 `test_agent_tools_storage_workspace.py` 全量 + 相关 `test_group_file_service.py` / `test_workspace_reconciliation.py`；`scripts/arch-guard.sh`。
4. **回归**：重点看 `test_edit_file_direct_write_refreshes_run_workspace_so_flush_does_not_conflict`（L1013）与 `test_edit_file_direct_write_updates_run_workspace_view`（L1050）仍绿——确认「直写刷新 manifest」路径没被 CAS 破坏。

> 本方案**只到改点清单**，未实施任何代码改动。是否进入 tdd 实施，等评审结论。
