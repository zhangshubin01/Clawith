# 2026-09-01 工作区发布冲突第三代修复（Runtime 直写 seam + 熔断重置语义）

- **状态**：待评审
- **前置**：
  - [ADR-0011](../adr/0011-workspace-direct-write-run-refresh.md)（直写工具刷新 Run 工作区，646be775 已部署）
  - [20260901-workspace-publication-p0-fix.md](./20260901-workspace-publication-p0-fix.md)（P0 分层发布 + P0.5 文案与熔断，63b70e91 已部署）
  - 事故：2026-09-01 12:19–12:21 UTC，run be39c1ad（agent 950a1943，goal「做 1、2、3、5」）8 次 execute_code 连败 `workspace_sync_conflict`
- **范围**：两个 P1（直写刷新 seam、熔断重置语义）+ 一个 P2 安全网（冲突后重新物化）+ ADR-0011 勘误

## 修订记录

| 版本 | 日期 | 说明 |
|---|---|---|
| v1 | 2026-09-01 | 初稿：事故证据链 + 两处设计缺陷 + 修复方案 |
| v2 | 2026-09-01 | R2 评审并入：①指纹改熔断器内计算 sha256(name+error_code+content)（content_hash 不在工具消息里，`_result_message` 只透传 4 个 metadata 字段）；②指纹熔断升为必做（封堵「任意写成功即重置」的 ping-pong 洞）；③重置谓词独立 frozenset（不复用 `_WORKSPACE_WRITE_TOOLS`，语义不同） |

## 1. 问题陈述（已核实的事实）

### 1.1 现象

部署 63b70e91（含 P0 分层发布 + P0.5 熔断）之后，真实 run 仍出现连续 8 次
`workspace_sync_conflict`（12:19:22 → 12:21:59，每次 attempt=1，8 个独立 execute_code 调用，
`content_hash` 全等 `b592ba57…`，即模型反复重跑同一段脚本）。冲突路径为 3 个**源码**文件
（CalculatorReducer/UiState/ViewModel.kt），不在 L2 派生产物黑名单内。模型最终在第 8 次后
改走 edit_file（12:24:02 成功）才脱困——「自愈」是模型换工具，不是状态自愈。

### 1.2 根因链（两层，每环有证据）

**层一：ADR-0011 的刷新钩子接在了错误的 seam 上，对 Runtime 是死的。**

1. `use_run_workspace` 按 run 每进程只物化一次临时工作区，manifest 的
   `base_version_token/base_hash` 是物化时刻快照；execute_code 发布时用 manifest token 做 CAS。
2. 直写工具走 typed-outcome 路径：Runtime 的 `execute_builtin_tool_outcome`
   （agent_tools.py:5162）把 `edit_file` 分发给 `_edit_file_outcome`（:4490）、`write_file` 给
   `_write_file_outcome`（:3823）、`move_file`/`delete_file` 给 :4181/:4338。**这四个函数没有
   任何 `_refresh_run_workspace_after_direct_write` 调用**（全文件 grep 仅 :2185/:2191 位于
   `flush_temp_workspace`，:3317/:3348/:3375/:3426 位于 legacy `_execute_workspace_mutation`，
   :5091 位于 android_compile APK 回传）。
3. ADR-0011 选择的接缝 `_execute_workspace_mutation` 如今只剩两个调用方：审批后执行
   `_execute_tool_direct`（无 run 上下文，refresh 必然 no-op）与 legacy 字符串分发——Runtime
   从不经过它。typed-outcome 迁移早在 3d359c28/ad606146 完成，**ADR-0011 撰写时前提已过时**。
4. 事故实锤：run be39c1ad 中 12:04:42/12:14:23 的 edit_file（Reducer.kt/ViewModel.kt）写
   storage 后，12:00–12:19 窗口容器日志只有 android_compile 的 2 条 APK
   `[RunWorkspaceRefresh] refreshed path`，直写刷新为零条。第 1 次冲突的候选 manifest
   （容器内 `/data/agents/private/workspace-reconciliation/…/fc2cd9d7…/manifest.json`）显示
   Reducer.kt 的 base 冻结在 `1788263697920366943:7601:…`（=11:54:57.9，恰为 11:57:19
   重新物化时的文件 mtime），而 storage 实际版本为 12:14:23（`1788264863446149173:7808:…`，
   stat 取证）。8 条 `[WorkspaceFlushConflict]` 日志的 expected/current 完全一致——陈旧 base
   从未被刷新，每次 CAS 必败。
5. 诱因（非根因）：63b70e91 的 L2 派生产物过滤把 `.git` 排出沙箱物化 → 模型在沙箱内
   `git init` 重建并 fetch/checkout（12:16–12:18）→ 代码改掉 Calculator 四件套 .kt。冲突本身
   是**保护性**的（挡下了用远端内容覆盖 12:14 的本地新编辑），损失只是 8 次白跑。

**层二：P0.5 熔断的重置语义与它自己的 remediation 文案自相矛盾。**

`apply_workspace_sync_conflict`（tool_repair_budget.py:52）对**任意**非冲突工具结果清零
streak（:70-72），测试 `test_workspace_sync_conflict_breaker_resets_on_other_tool_result`
（backend/tests/test_workspace_publication_filter.py:500）把「read_file 成功 → 清零」写成预期
行为。而 P0.5 文案指示模型「请 read_file 读取工作区当前最新内容 → 再 edit_file」——**照文案
做，read_file 就重置计数，熔断永远到不了 3**。账本实锤：8 次冲突之间每次都有 read_file
成功间隔（12:21:23 冲突 → 4×read_file → 12:21:40 冲突 → …），count 最高到 1。

### 1.3 已核实事实清单（方案依据）

| 事实 | 证据 |
|---|---|
| typed 四函数零 refresh 调用 | 全文件 grep `_refresh_run_workspace_after_direct_write` 仅 7 处，均不在 typed 路径 |
| legacy 钩子对 Runtime 不可达 | `_execute_workspace_mutation` 仅 `_execute_tool_direct` 与 legacy 分发调用 |
| manifest base 陈旧是 8 连败直接原因 | 容器内 manifest.json + stat + 8 条 WorkspaceFlushConflict 日志，expected/current token 全同 |
| 直写刷新确实没发生 | 12:00–12:19 日志窗口零直写 refresh 行（只有 2 条 APK） |
| 熔断被 read_file 重置 | 账本序列 + 测试 500 行把该行为写成预期 |
| typed 直写函数已接收 `runtime_run_id` | `_edit_file_outcome` 等签名含 `runtime_run_id: str | None`，钩子可显式透传 |
| group 写路径不经 typed 四函数 | `group_runtime_tools.py` 无 `_write_file_outcome/_edit_file_outcome` 引用 |

## 2. 目标与非目标

### 目标

- 同 run 直写（runtime typed 路径）后，execute_code 再改同路径**不再**出现
  `workspace_sync_conflict`（第一代事故的「待观察」闭环）。
- 熔断器在「冲突 + 读文件探查」的真实模型循环下仍能在 3 次冲突时熔断。
- 所有写路径（含未来新增）有回归测试兜底，不再出现「接错 seam」。

### 非目标（明确不做）

- 直写工具下沉执行环境 / 共享文件视图（ADR-0011 长期方向，记档不动）。
- 改动 CAS 冲突保护的语义（第三方/人类写仍宁败勿覆盖）。
- 改动 L2/L3 分层发布与 P0.5 文案（本轮已证实生效）。

## 3. 修复一（P1）：直写刷新接入 Runtime typed 路径

### 3.1 设计

1. `_refresh_run_workspace_after_direct_write`（agent_tools.py:2207）增加
   `run_id: str | None = None` 参数：显式传入时优先于 contextvar（`sandbox_run_scope_id`），
   `None` 时回退 contextvar（兼容审批路径等无 run 上下文调用点，行为不变）。
2. 四个 typed 直写函数在成功出口（`discard_candidate` 之后、`return _typed_success(...)`
   之前）各加一个 refresh 调用，显式传 `run_id=runtime_run_id`：
   - `_edit_file_outcome`：`await _refresh_run_workspace_after_direct_write(agent_id, result.path, run_id=runtime_run_id)`
   - `_write_file_outcome`：同上（`write_result.path`；append 模式 refresh 读的是写后终态，天然正确）
   - `_move_file_outcome`：目标 `data` 刷新 + 源 `deleted=True` 刷新
   - `_delete_file_outcome`：`deleted=True` 刷新
3. legacy `_execute_workspace_mutation` 内 :3317/:3348/:3375/:3426 的调用改为走同一 helper
   （不加 run_id，回退 contextvar），删除重复逻辑——**两个 seam 一个实现**，杜绝第三次接错。
4. 不传 `skip_workspace`：直写工具的 workspace 对象不是 run 工作区，无身份判等问题。

### 3.2 代码落点（全量清单）

| 文件 | 位置 | 改动 |
|---|---|---|
| agent_tools.py | `_refresh_run_workspace_after_direct_write`（:2207） | 加 `run_id` 参数与取值优先级 |
| agent_tools.py | `_write_file_outcome`（:3823） | 成功出口加 refresh（run_id=runtime_run_id） |
| agent_tools.py | `_move_file_outcome`（:4181） | 成功出口加目标 refresh + 源 deleted 刷新 |
| agent_tools.py | `_delete_file_outcome`（:4338） | 成功出口加 deleted=True 刷新 |
| agent_tools.py | `_edit_file_outcome`（:4490） | 成功出口加 refresh（run_id=runtime_run_id） |
| agent_tools.py | legacy `_execute_workspace_mutation` | 调用收敛到同一 helper（无行为变化） |

### 3.3 安全与副作用排查（已逐项核实）

- **锁序**：typed 直写只开 DB 会话，不持 storage/workspace 锁；refresh 只取 run 工作区
  `state.lock`。flush 路径为 workspace_locks → state.lock，方向一致，不成环，无死锁。
- **group 写**：走 `group_tool_service.execute_scoped_workspace_tool`，不经 typed 四函数，
  零影响。
- **审批后执行**：不传 `runtime_run_id` 且无 contextvar → no-op，行为不变。
- **enterprise_info**：typed 写入口直接拒绝（read-only），到不了 refresh。
- **失败模式**：refresh 是 best-effort + 日志；失败不改变写结果，退回旧冲突保护（fail-safe）。
- **storage key 一致**：typed 写与物化共用 `_tool_storage_key`，refresh 的
  `normalize_storage_key(f"{agent_id}/{rel_path}")` 对非 enterprise 路径等价（本次事故 agent
  已取证一致）；enterprise 写不可达，无 key 错配面。

### 3.4 测试（红 → 绿）

- 新：typed `_edit_file_outcome`/`_write_file_outcome` 写成功后，同一 run 工作区 manifest
  对应条目 base_version/base_hash 更新为写后值；随后 execute_code 修改同路径 flush 不再冲突
  （复用 `tests/test_agent_tools_storage_workspace.py` 的 T1–T4 骨架，改为走 typed 入口 +
  `run_id` 显式传入）。
- 新：`_move_file_outcome` 目标刷新 + 源删除；`_delete_file_outcome` 删除路径后 manifest
  条目移除、temp 文件移除。
- 新：`runtime_run_id=None` 且无 contextvar 时 refresh no-op（审批路径行为不变）。
- 既有 T1–T4 与 `test_workspace_publication_filter.py` 全量保持绿。

## 4. 修复二（P1）：熔断重置语义收紧

### 4.1 设计

`apply_workspace_sync_conflict` 的重置条件从「任意非冲突消息」改为**「持久化进展」**：

- 重置：**独立谓词 frozenset** `_DURABLE_WORKSPACE_PROGRESS_TOOLS = {write_file, edit_file,
  move_file, delete_file, execute_code, android_compile}` 中 `execution_status=="succeeded"`
  的消息。注意**不**复用 node_executor 的 `_WORKSPACE_WRITE_TOOLS`（:46，仅为
  {write_file, edit_file}，是记忆门计数用途，语义不同）。
- 不重置：read_file/list_files/search_files/find_files 等只读结果，以及其他工具的失败结果。
- 判定实现为共享谓词 `_is_durable_workspace_progress(message)`，集中在 budget 模块，纯函数可测。
- **指纹熔断（必做，同票内）**：同指纹的冲突 ≥3 次直接 terminal。指纹在熔断器内计算
  `sha256(name + "\0" + error_code + "\0" + content)`——**不依赖 tool 结果 metadata 的
  content_hash**：`_result_message`（tool_step_service.py:456）只把 metadata 的
  execution_id/call_instance_id/provider_call_id/contract_version 四个字段拷进消息，
  content_hash 不在消息里、熔断器读不到。本事故 8 次冲突的 content 文本一致（同 3 个路径），
  内容级指纹天然相等，零管线改动。
- **为什么指纹必做**：「任意写成功即重置」存在 ping-pong 洞（冲突 Reducer.kt → 成功写
  colors.xml → 再冲突 Reducer.kt → …）。路径级重置（只认「先前冲突路径被成功写」）更精确，
  但需要把冲突路径结构化放进消息，成本高收益小——不做；指纹熔断是封堵该洞的最廉价手段
  （同脚本重跑是本次事故的最强信号，3 次即终）。

### 4.2 代码落点

| 文件 | 位置 | 改动 |
|---|---|---|
| tool_repair_budget.py | `apply_workspace_sync_conflict`（:52） | 非冲突消息先判持久化进展，是则清零，否则保持计数 |
| tool_repair_budget.py | 新增谓词 helper | `_is_durable_workspace_progress(message)`，集中在 budget 模块，纯函数可测 |
| tool_repair_budget.py | 新增指纹 helper | `_content_fingerprint(name, error_code, content)` = sha256(name + "\0" + error_code + "\0" + content)，纯函数可测 |
| node_executor.py | 无 | 接线不变（已逐消息喂给熔断器，指纹在熔断器内自算，零管线改动） |

### 4.3 测试（红 → 绿，含显式改写）

- **改写** `test_workspace_sync_conflict_breaker_resets_on_other_tool_result`（:500）：
  read_file 成功**不再**清零；新增 edit_file 成功清零用例。
- 新：`冲突 → read_file 成功 → 冲突` 循环推进到 3 次时 terminal=True（模拟 be39c1ad 序列）。
- 新：其他工具失败（非冲突 error_code）不重置也不计数。
- 新（必做）：同一内容指纹 `sha256(name + "\0" + error_code + "\0" + content)` 的冲突 ≥3
  直接 terminal；用例覆盖 be39c1ad 的 8 次同脚本重跑（content 文本一致 → 指纹相同）。

## 5. 修复三（P2 安全网）：flush 冲突后自动重新物化

### 5.1 设计

`flush_temp_workspace` 对 run 工作区返回 `conflicted` 时，将该 run 工作区标记为「下次调用
前重新物化」（或直接丢弃并重建 `_run_workspace_tasks` 条目）。效果：N 连败变 1 败。

**不牺牲第三方写保护**：重新物化读的是当前 storage（含第三方最新内容），下一次 execute_code
在最新视图上重跑、CAS 打在新 base 上——不是盲覆盖，保护语义原样保留；若第三方持续写入，
冲突仍会发生，由修复二熔断兜底。

### 5.2 代码落点

| 文件 | 位置 | 改动 |
|---|---|---|
| run_workspace.py | `_run_workspace_tasks` 生命周期 | 增加 `mark_stale(run_id)`（或 close+重建路径），带锁 + identity 校验 |
| agent_tools.py | `flush_temp_workspace` 冲突分支 | run 工作区身份下调用 `mark_stale` |

### 5.3 测试

- 新：同 run 直写（绕过刷新模拟）→ execute_code 冲突 1 次 → 下次调用自动重新物化 →
  再执行不再冲突。
- 新：重新物化后第三方持续写入仍冲突，且熔断仍可触发（与修复二联测）。

## 6. ADR-0011 勘误（随实施一起落库）

对 `docs/adr/0011-workspace-direct-write-run-refresh.md` 追加勘误段：

1. 「Mutation 工具族（`_execute_workspace_mutation`）」前提已过时：Runtime 自
   3d359c28/ad606146 走 typed-outcome 路径，646be775 的钩子对真实 run 从未生效。
2. 修正后的接缝清单 = 本方案 §3.2（typed 四函数 + legacy 收敛 + flush 钩子）。
3. 经验条款：接缝类修复必须配「路径级回归测试」（断言刷新生效），防止迁移改道后钩子死亡
   无人知晓。

## 7. 部署、验证与回滚

- 按仓库部署惯例（skill clawith-prod-deploy）上线前：ruff check + arch-guard + 全量测试。
- 上线后验收：
  - 合成验证：同 run 直写 → execute_code 同路径发布成功（容器内真实 LocalStorageBackend）。
  - 真实 run 观察 24–48h：Langfuse `tool_failure` code evaluator 中
    `workspace_sync_conflict` 计数归零或降到个例且 1 次即自愈；`workspace_sync_conflict_limit_reached`
    terminal 出现且路径正确（有 read 间隔也熔断）。
  - 回滚标签按部署脚本惯例留存（修复二仅影响熔断计数，回滚 = 恢复旧重置语义，无状态迁移）。

## 8. 参考资料与决策依据（reference-check 纪律）

| 决策点 | 依据 | 结论 |
|---|---|---|
| 直写刷新接缝选择 | ADR-0011（仓库内）+ run_workspace.py/agent_tools.py 源码 | 沿用 ADR 决策，修正接缝清单 |
| 熔断重置语义 | P0 方案文档 §4 + tool_repair_budget.py + 事故账本 | 「只读不重置」是对 P0.5 文案自洽性的修正 |
| 重新物化安全网 | 第一代事故「方案B」存档结论（否决原因=只治标）+ 本方案补充论证（与熔断互补） | 降级为 P2 兜底，不与修复一重复 |
| 业界对照（外部） | reference-projects inventory：ADR-0011 业界对照表（OpenHands bind mount / CubeSandbox / gptme bwrap / SWE-agent 上传） | 无新增外部决策；维持「不退回裸挂载」结论 |
| claw-code 血缘上游核查 | claw-code 源码（本仓库血缘上游） | 上游无直写工具实现、无 CAS/manifest（单一共享视图，与 OpenHands/CubeSandbox/E2B/gptme/SWE-agent 同类），印证 ADR-0011 业界对照表；发布机制无法从上游借鉴，不改变任何决策 |
| 熔断重置语义外部对应物 | 外部 skills / 参考仓库检索 | 无权威对应物（N/A）；「只读不重置」决策仅以内源证据为准（P0.5 文案 + 事故账本），已显式声明 |
| 评估基准（外部） | SWE-bench / Terminal-Bench / RE-Bench | 不适用：本决策的验收标准是真实 run 行为（Langfuse/账本），已显式声明 |
