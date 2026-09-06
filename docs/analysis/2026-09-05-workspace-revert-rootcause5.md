# 根因⑤深挖：workspace 编辑数据被回退的机制定位

- **关联**：`2026-09-05-compaction-amnesia-764eb591.md`（§四根因5 / §七待深挖链路，本文是其深挖结论）
- **口径**：只读实读源码 + 运行时日志 + DB 账本 + Langfuse 推理，全部实锤。回退机制已决定性确认（见 §三/§四）。

---

## 一、已确认的写入架构（三个写入者，同一个本地文件系统）

`WORKSPACE_ROOT = Path(STORAGE_LOCAL_ROOT or AGENT_DATA_DIR)`（`agent_tools.py:163`）；`_agent_workspace_root(agent_id) = WORKSPACE_ROOT / str(agent_id)`（`:2835`）。本地模式下，edit_file / android_compile / execute_code 三者都落在这个根下，**不是「本地 FS vs 对象存储」的分层，而是同一文件系统里的三种写入路径**：

| 写入者 | 路径 | CAS/版本保护 | 代码 |
|---|---|---|---|
| **edit_file** | 直写 storage（读 `before` → 无条件写 `after`） | **无 CAS**（`condition=None`） | `_execute_workspace_mutation`（`agent_tools.py:3671-3721`）→ `write_workspace_file`（`workspace_collaboration.py:599`） |
| **android_compile** | **裸 bind-mount** `_agent_workspace_root`，不经 manifest/CAS | **无**（只对产物做事后 refresh） | `_android_compile_outcome`（`agent_tools.py:5310` `ws=_agent_workspace_root` → `:5391` `backend.execute(project_path=resolved_path)`）→ `AndroidBuildBackend`（`android_build_backend.py:387` `volumes={host_project_path: /workspace rw}`） |
| **execute_code** | run-scoped 临时工作区 → flush 回 storage | **有 CAS（source 类）** | `use_run_workspace`（`run_workspace.py:101`）→ `_prepare_temp_workspace`（`agent_tools.py:1891`）→ `flush_temp_workspace`（`:2098`） |

关键点：**execute_code 用的是「一次性物化的 run-scoped 临时工作区」，edit_file 和 android_compile 都绕过它直写 storage**。所以 edit_file 与 execute_code 天然是「两个视图」，必须靠 `_refresh_run_workspace_after_direct_write` 事后同步（ADR-0011）。

---

## 二、四个新确认的代码级事实（比报告 §四根因5 更进一步）

1. **edit_file 的写是「无条件写」，没有 CAS**。`write_workspace_file` 对 `operation="edit"`（`append=False`、`expected_version_token=None`）→ `condition=None` → `storage.write_bytes_if_match(..., condition=None)` 无条件覆盖（`workspace_collaboration.py:592-604`）。它读的 `before`（`:587`）只用于 `record_revision` 记账，**不用于并发校验**。即 edit_file 的 read-modify-write 存在「读后写前被其它写入者覆盖」的窗口。
2. **android_compile 对 source 文件零保护**。它把 `_agent_workspace_root` 直接 rw bind-mount 进构建容器（`android_build_backend.py:387`），事后只有「two-writer coordination」对 **产物（apk/aab）** 调 `_refresh_run_workspace_after_direct_write`（`agent_tools.py:5430-5443`），**source 文件不在其中**。gradle 本身不改 source，但它的「物化→构建→（若触发 git 操作）→退出」全程无 manifest/CAS 记账。
3. **refresh 有六类 no-op 路径**（`run_workspace.py:164-193` + `agent_tools.py:2404-2434`）：`no_run_id` / `never_materialized` / `is_dir` / `per_file_limit` / `version_invisible` / `skip_workspace`。其中 **`no_run_id` 是隐患点**：`_refresh_run_workspace_after_direct_write` 的 `run_id` 来自 `sandbox_run_scope_id` 这个 **ContextVar**（`run_scope.py`），它由 `command_worker.py:1008` `sandbox_run_scope_id.set(str(run.run_id))` 设置。ContextVar 是**任务局部**的——若 edit_file 的工具步骤运行在与设置该变量的 run 循环不同的 asyncio task 里，edit_file 读到的是默认空串 → refresh 直接 no-op → 临时工作区停在物化时的旧快照。（**本 run 未触发此变体**，refresh 实际成功，见 §三 3.5。）
4. **execute_code 的 flush 对 source 类走 CAS（fail 模式）**：`.kt` → `classify_publish_path` → `"source"`（`workspace_policy.py:86`）→ `cas_files` → `write_bytes_if_match(condition=WriteCondition(version_token=manifest.base_version_token))`（`agent_tools.py:2166-2195`）；只有 `build/outputs/**` 是 LWW（`artifact`）。且 flush 前有 `base_hash == current_hash` 跳过（`:2151/2170`）——**临时工作区里没变的文件不会写回**。

---

## 三、回退机制（已实锤：git 探索把沙箱工作树回退到 HEAD，flush 用 base_hash 误判发布）

### 3.1 回退是真实的（ledger + 内容双铁证）

- **字节级**：`workspace_file_revisions` 12:18:35 Calculator.kt `before=11134/after=12103`，12:36:31 再 edit `before=11134`（`before` 即 `storage.read_text` 返回值，`workspace_collaboration.py:587`）→ storage 真回到 11134。
- **内容级（本轮新增，比字节数更硬）**：`agent_tool_executions.sanitized_arguments` 完整保留 edit_file 的 `old_string`/`new_string`（**未 redact**；`_SENSITIVE_PATHS` 只 redact `execute_code` 的 `code/env/environment`）。对比 12:18:35 与 12:36:31 两次 Calculator.kt 编辑的 `old_string` **逐字一致**（都是「优化项 5」原始版），而两次 `new_string` 措辞不同（「P1 循环小数退化修复」vs「P1·展示」）。→ 12:36:31 时 storage 真回到原始内容，且模型失忆后用**不同措辞**从零重做，坐实「回退 + 失忆」双因。

### 3.2 回退精确时间窗 = 12:21:26 → 12:22:41

read_file 读的是 storage（result_summary 行数铁证）：Calculator.kt 12:20:44=287 行、12:21:26=287 行（新），**12:22:41=268 行（旧）**起持续到 12:39；CalculatorTest.kt 12:20:44=262 行（新）→**12:23:32=234 行（旧）**。此窗内只有两个 execute_code：12:21:27（git status/branch/remote/log）与 12:22:22（git branch/log/remote/user），输出均显示 HEAD=2782458、`=== status ===` 空（clean）。

### 3.3 回退无 revision 记录 → 必走 flush_temp_workspace 路径

`workspace_file_revisions` 全程仅 6 条 edit 记录（actor_type=agent）。edit_file 路径（`write_workspace_file`）必 `record_revision`；回退却无任何 revision → 写入者不是 edit_file，而是 **flush_temp_workspace 的 `storage.write_bytes_if_match` 直写**（`agent_tools.py:2098`，成功时无 info log、无 conflict 日志）。

### 3.4 候选 B（remote restore 的 mixed reset）排除

后端日志只有两条 `[GitLabBinding] creds injected into temp workspace`（12:13:54、12:46:22），**无「remote restore」/「adopted」日志**；.git 从 bundle 恢复走 `restore_git_metadata_from_bundle`（`git reset --mixed`，不动工作树）。候选 B 排除。

### 3.5 候选 A（refresh no-op）的「no_run_id 变体」也否定

refresh **实际成功了**：日志 `[RunWorkspaceRefresh] refreshed path: run_id=764eb591 ... Calculator.kt size=12103` 在 12:18:35。ContextVar `no_run_id` 变体未触发。

### 3.6 触发机制（refined 候选 A，高置信实锤）

1. agent 的 edit_file 直写 storage（无条件写、无 CAS），refresh 同步到 run-scoped 临时工作区（12103）。
2. 模型因压缩失忆「去找丢失的工作」，在 execute_code 沙箱内跑 git 探索。reflog 铁证（12:37:15 输出）：`2782458 HEAD@{2}: reset: moving to f_android_ai` + `checkout -b fix/p1-repeating-display`（12:29:16）+ `checkout f_android_ai`（12:30:25）。
3. git 命令把沙箱工作树回退到 commit 2782458（其中从未含 edit_file 的未提交改动，Calculator.kt=11134），flush 把回退文件 CAS 写回 storage。
4. **CAS version_match 守卫失效原因**：manifest 的 `base_version_token` 在 refresh 时=edit 后的版本；git checkout 只改沙箱文件、不改 storage 版本，故 flush 时 token 仍匹配 → CAS 通过；`base_hash` 检查把「git 回退」误判为「模型有意改动」而发布。

**时间相关性（只有编译前文件回退）的解释**：编译前 Calculator.kt/CalculatorTest.kt 在物化时点 S0 后被 edit，refresh 同步到临时工作区后，12:22 的 git 探索把这两个文件的沙箱副本回退到 2782458 版本（=原始），flush 发布；编译后 CalculatorReducer*.kt 在 android_compile 之后才 edit，且未落入 12:22 的 git 回退窗口，故无重做签名。

---

## 四、决定性取证记录（已全部实锤）

### 4.1 回退时间窗（read_file result_summary 行数铁证）

Calculator.kt 12:20:44=287 行、12:21:26=287 行（新）→ **12:22:41=268 行（旧）**起持续到 12:39；CalculatorTest.kt 12:20:44=262 行（新）→ **12:23:32=234 行（旧）**。窗内仅两个 execute_code（12:21:27 / 12:22:22），输出 HEAD=2782458、status 空（clean）。

### 4.2 内容级铁证（sanitized_arguments 未 redact）

工具「输入」的权威来源是 `agent_tool_executions.sanitized_arguments`（按 `tool_execution_id`/`tool_call_id` 关联），**不是** `events_full`——`tracing.py` 的 `observe_tool` docstring 明确「Tool arguments are intentionally not captured」。查实：`_SENSITIVE_PATHS`（`builtin_tool_definitions.py:4007-4019`）只 redact `execute_code` 的 `code/env/environment`、`import_mcp_server` 的 `config.api_key/token/password/authorization`、`vercel_set_env.value`、`neon_create_database.password`、`feishu_approval_create.form_data`——**edit_file 的 `old_string/new_string` 不在其中，完整落账**。12:18:35 与 12:36:31 两次 Calculator.kt 编辑 `old_string` 逐字一致、`new_string` 措辞不同。

### 4.3 reflog reset 铁证（12:37:15 execute_code 输出）

`2782458 HEAD@{2}: reset: moving to f_android_ai` + `checkout -b fix/p1-repeating-display`（12:29:16）+ `checkout f_android_ai`（12:30:25）——git 探索把沙箱工作树带回 2782458。

### 4.4 日志排除项

- `[RunWorkspaceRefresh] refreshed path: run_id=764eb591 ... Calculator.kt size=12103`（12:18:35）→ refresh 成功，`no_run_id` 变体排除。
- 仅两条 `[GitLabBinding] creds injected`（12:13:54 / 12:46:22），无「remote restore」/「adopted」→ 候选 B 排除。
- 无 `[WorkspaceFlushConflict]` → flush 未冲突，CAS version_match 通过（§三 3.6.4）。

### 4.5 确认的写入架构代码事实（实读）

- `WORKSPACE_ROOT = STORAGE_LOCAL_ROOT or AGENT_DATA_DIR`（`agent_tools.py:163`）；`_agent_workspace_root`（`:2835`）。
- edit_file 直写 storage 无条件写（`workspace_collaboration.py:592-604 condition=None`）；android_compile 裸 rw bind-mount（`android_build_backend.py:387`）；execute_code 用 run-scoped 临时工作区+flush（`use_run_workspace` `run_workspace.py:101`；`_prepare_temp_workspace` `agent_tools.py:1891`；`flush_temp_workspace` `:2098`，source 类 CAS fail 模式、artifact LWW）。
- refresh 六类 no-op：`no_run_id/never_materialized/is_dir/per_file_limit/version_invisible/closed`；`sandbox_run_scope_id` 是 ContextVar（`run_scope.py`，`command_worker.py:1008` 设置）。
- execute_code 装配 `_execute_code_with_workspace_outcome`（`agent_tools.py:3104`），flush 在 `gateway_publish`（`:3393`）与后置（`:3473`）。

---

## 五、结论（已实锤）

- **高置信（实锤）**：写入架构是「三写入者同文件系统、edit_file 无条件写 + android_compile 裸 bind-mount + execute_code 临时工作区 CAS flush」；根因⑤的「两层分裂」本质是 **edit_file 与 execute_code 的「storage 直写视图 vs run-scoped 临时工作区视图」不一致**。
- **高置信（实锤）**：具体回退 = 模型压缩失忆后，在 execute_code 沙箱内 git 探索（checkout/reset）把沙箱工作树带回 HEAD 2782458（未含 edit_file 未提交改动），flush 把回退文件 CAS 写回 storage；`base_hash` 把「git 回退」误判为「模型有意改动」而发布，`version_match` 因 manifest token 未被 git 推进而放行。
- **CAS 守卫失效本质**：沙箱内 git 仓库（bundle 恢复的 .git）是与 edit_file 直写并立的**第二真相源**；flush 的 `base_hash` 比较无法区分「模型有意改动」vs「git checkout/reset 回退到 HEAD」，这是比「refresh no-op」「remote mixed reset」更根本的缺陷。

**修复方向（更具体，四条 + 一条新增）**：
1. **【P0·防回退】flush 需区分「模型有意改动」vs「git 操作把沙箱文件回退到 git HEAD」**——沙箱内 git 仓库是第二真相源，flush 的 base_hash 比较无法区分两者。方案：flush 前对比「沙箱文件 vs storage 当前」，若沙箱内容等于「git HEAD 版本」而 storage 不等于，视为回退而非有意改动，**拒绝发布并告警**（或要求模型显式确认）。
2. **【P0】edit_file 补 CAS**：用 `expected_version_token`（read 时的 version）做条件写，消除「无条件写」的 read-modify-write 竞态窗口。
3. **【P1】android_compile 纳入 manifest/CAS 记账**（至少 source 文件），消除裸 bind-mount 的零保护。
4. **【P1】refresh 显式传 run_id**，不依赖 ContextVar 跨 task 传播（消 `no_run_id` 类 no-op）。
5. **【P1·新增】git 探索类命令（checkout/reset/restore/clean）对 source 文件的副作用需告警或记账**：execute_code 沙箱内 git 命令改动 source 时，flush 应识别并告警，而非静默按「改动」发布。
