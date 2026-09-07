# 根治修复方案：workspace run 关联静默失效 → 可观测性兜底（方向 4 收尾）

> **状态**：已实现 + Phase 5 `code-review` 复核通过（Spec/Standards 双轴 PASS）。按 `clawith-fix-plan` 四件套产出。
> **上游**：`docs/analysis/2026-09-05-workspace-revert-rootcause5.md` §五「方向 4」；上一轮代码审查结论。
> **红线**：测试环境不灰度（2026-08-30）；DB/Redis 只读；后端改动前 `scripts/arch-guard.sh`。

---

## 0. 裁决（先说结论）

**方向 4「refresh 显式传 run_id」不是已发故障，是 P2 预防（臆想风险）。** 近 3 天运行日志 `[RunWorkspaceRefreshSkipped] reason=no_run_id` **零发生**（13 条全部是设计内的 `never_materialized`）。因此：

- **不做的**：把 `run_id` 显式参数贯穿 legacy `_execute_workspace_mutation` 全链路——那是对「未发生故障」的投机式加固（宪法 II），且 legacy approval 路径本就没有 run 上下文，显式传参无值可传。
- **要做的**（最小根治）：把「run 关联丢失」从**静默 info** 升级为**可见 warning**，补齐 bisheng 式「contextvar + 防静默失效」配套里 Clawith 缺失的那一刀。

---

## Phase 1 — 参考对比结论（决策点：run/请求级上下文关联怎么传、失效怎么处理）

| # | 项目 | 做法 | 结论 |
|---|---|---|---|
| 1 | orca | `runtimeFence` 单调代际**显式传递**（消息/参数字段），陈旧执行体拒绝 | 显式传参 + 失效即拒绝 |
| 2 | LangBot | `placement_generation` 单调代际贯穿全链路，`assert_execution_active` 陈旧自灭 | 显式传参 + 失效即自灭 |
| 3 | **bisheng**（Clawith 同 org） | **ContextVar 租户隔离** + `do_orm_execute` 自动注入 `tenant_id`，**配 `_TENANT_AWARE_MODEL_MODULES` 强制 import 防静默泄漏 + 注释直点 v2.5 泄漏教训** | 隐式 contextvar，但**有防静默失效配套** |
| 4 | herdr | 状态「hook 权威 + screen fallback」双源显式仲裁 | 显式 |
| 5 | deepagents | 文件状态放 graph state `files` channel 随 checkpoint 持久 | 显式 state |
| 6 | OpenHands | event-stream + 显式状态对象传递 | 显式 |
| 7 | opencode | client/server 分离，工具执行显式携带会话上下文 | 显式 |
| 8 | codex | 会话/上下文 id 显式传递 | 显式 |
| 9 | conductor | `workflowId`/`taskId` 显式随任务传递 | 显式 |
| 10 | letta-code | 单用户 harness，无多租户跨请求 run 关联 | 诚实负结论：无此机制 |
| 11 | 12-factor-agents | ownership boundary 原则 | 方法论：状态 owner 显式 |

**对比结论**：9/11 显式传参；唯一用隐式 contextvar 的 bisheng **配了防静默失效**（强制 import 防泄漏 + 泄漏教训注释）；1 不适用。共同底线：**无论显式还是隐式，关键上下文关联都不能静默失效**。Clawith 用隐式 contextvar（对齐 bisheng），但缺「防静默失效」配套——这是唯一真实缺口。故根治 = 补失效检测，而非改成显式传参（orca/LangBot 模式对 legacy approval 路径无值可传，不普适）。

## Phase 2 — 双源根因

### 代码侧（已 read_file 核实，函数名定位）

- `sandbox_run_scope_id` 是 `ContextVar[str]`（`app/services/sandbox/run_scope.py`），**唯一 set 点 `command_worker.py:1013`**，`try` 包裹整个 `_command_executor.execute(...)`，`finally` reset。→ 所有工具执行（flush 的 4 个调用点 + legacy 直写）都在该 context 内，`await`/`asyncio.wait_for` 不破坏 context。
- typed outcome 出口（`_run_with_temp_workspace_outcome` 系）**已显式传** `run_id=runtime_run_id`（6a5a9928）。
- **未显式传、靠 contextvar 兜底**的调用点：
  - `flush_temp_workspace` 尾部 ADR 0011 同步段（L2436/L2442）——函数内 L2157 已读 `run_id` 却未传；
  - legacy `_execute_workspace_mutation` 直写路径（L3695/L3726/L3816 等）；
  - **legacy approval 直接执行**（`autonomy_service.py:384` `_execute_tool_direct`，`resolve_approval` 的 legacy 分支，**不在 command_worker context 内、不 set contextvar**）。
- `no_run_id` 分支（`_refresh_run_workspace_after_direct_write` L2481-2485）只记 `logger.info`；`flush_temp_workspace` 内 `run_id=None` 时 L2434/L2179 两处 `if run_id` 静默跳过（同步段 + `_discard_stale_run_workspace`）。

### 日志侧（运行日志，双源之二）

```
$ docker logs --since 72h clawith-agent-backend-1 | grep RunWorkspaceRefreshSkipped | grep -oE 'reason=[a-z_]+' | sort | uniq -c
13 reason=never_materialized
```

近 3 天 13 条全部 `never_materialized`（heartbeat run 物化前写 memory 文件，设计内）；**`no_run_id` 零发生**。

### 根因（可证伪）

**症状**：run 关联（`run_id`）在 refresh/flush 中靠隐式 ContextVar 传递，唯一 set 点在 command_worker；部分路径（legacy approval）不在此 context 内，refresh 静默 no-op。**追一层**：为什么「静默」？因为 `no_run_id` 只记 info，无告警/计数/断言。**最深层因**：关键不变量「run 关联」既无全路径显式传递（部分路径），也无失效检测（丢失时静默）——两头不设防。

**证伪式**：若根因是「contextvar 传播不可靠」，则 command_worker context 内的工具执行也应出现 `no_run_id`；但近 3 天零 `no_run_id`，说明 contextvar 在**现有调用结构内传播可靠**，真正缺口是「**非 context 路径（legacy approval）+ 无失效检测**」这一组合——legacy approval 是设计内历史行为（`autonomy_service.py` 注释明示），但它与「静默」叠加后，未来任何新增非 context 调用路径都会**静默丢 run 关联且不可见**。

## Phase 3 — 修复方案（最小、可回退）

| 改动 | 位置 | 说明 |
|---|---|---|
| **① no_run_id 升 warning（核心）** | `_refresh_run_workspace_after_direct_write` L2481-2485 | `logger.info` → `logger.warning`，把「run 关联丢失」变可见 |
| **② flush run_id=None 升 warning（核心）** | `flush_temp_workspace` L2157 后 | `if not run_id: logger.warning("[WorkspaceFlushNoRunId] ...")`——同步段 + `_discard_stale_run_workspace` 静默跳过的上游告警 |
| ③ 显式传参（一致性收尾，可选） | `flush_temp_workspace` 尾部 L2436/L2442 | 传 `run_id=run_id`，消「读两次 contextvar」，与 typed 出口风格统一 |

**明确不做**（宪法 II 最小改动）：
- run_id 显式参数贯穿 legacy `_execute_workspace_mutation`——legacy approval 无 run 上下文，显式传无值可传，且是设计内历史行为；
- 移除 contextvar 依赖——bisheng 同用 contextvar，错不在 contextvar 而在「静默」。

**回归测试（tdd）**：
1. `test_refresh_warns_on_missing_run_id`：monkeypatch `sandbox_run_scope_id` 为 `""`，调 `_refresh_run_workspace_after_direct_write`，断言 `caplog` 捕获 `warning`（非 info）且含 `no_run_id`。
2. `test_flush_warns_on_missing_run_id`：构造 `run_id=None` 的 flush 场景（monkeypatch contextvar），断言 `[WorkspaceFlushNoRunId]` warning 发出、且原有 `if run_id` 分支行为不变（不回归）。

**影响面**：零契约变更（只改日志级别 + 2 处传参）；爆炸半径零（warning 不改变任何流程控制/返回值）。改动集中在 `agent_tools.py` 单文件。

## Phase 4 — 7 角度评审

**Q1 根因是否正确？** —— **通过**。正向依据：根因「关键不变量 run 关联无失效检测」能解释双源全部证据——日志侧 `no_run_id` 零发生（现有路径可靠）+ 代码侧 legacy approval 非 context 路径 + no_run_id 只记 info。负向探针（反例测试）：若根因是「contextvar 传播本身不可靠」，则 command_worker 内的工具执行应出现 `no_run_id`；对照 72h 日志 13 条全是 `never_materialized`、零 `no_run_id`——**推翻**「传播不可靠」，坐实「非 context 路径 + 无检测」组合，根因成立。

**Q2 方案是否根治？** —— **通过**。正向依据：方案直接补「失效检测」（①+②），正是根因缺失的那一刀；不投机加固传递机制。负向探针（删除测试）：把方案删掉，问「未来新增非 context 调用路径时 run 关联会不会再静默丢」——**会**（no_run_id 仍是 info，无告警）；保留方案则丢了立即可见。故是根治。

**Q3 参考资料是否正确？** —— **通过**。正向依据：orca/LangBot（显式代际）、bisheng（contextvar+防静默泄漏）、letta（诚实负结论）均读自已核实的整库研究报告（`20260905-orca-study.md`/`20260905-langbot-study.md`/`20260905-bisheng-study.md`/`20260903-letta-code-study.md`），非 README 摘要。负向探针：我特意核过 bisheng 的 contextvar 机制是否已改名/迁移——`_TENANT_AWARE_MODEL_MODULES` + `do_orm_execute` 在本地 `UGit/bisheng` 源码存在，无误。

**Q4 副作用与爆炸半径？** —— **通过**。正向依据：①副作用面——改动无外部写、无缓存/连接/资源操作，仅日志级别；②影响面——改动只在 `agent_tools.py`，无契约/状态变更，`detect_changes` 爆炸半径零（warning 不影响 return 值、不影响 `if run_id` 分支逻辑）。负向探针：我特意找过「warning 会不会被某处日志管道当 error 上报/告警风暴」——`no_run_id` 零发生，warning 不会触发风暴；且 `[WorkspaceFlushNoRunId]` 为新增 key，与既有 `[RunWorkspaceRefreshSkipped]` 区分，不污染既有监控。

**Q5 是否最优且必要？** —— **通过（定性 P2 预防）**。正向依据：①枚举 ≥3 候选——a) 只改 2 行显式传参（更简单）；b) 本方案（warning 检测，当前）；c) run_id 显式参数贯穿全链路（更彻底）。从 Ponytail 最低档起：a) 只治 flush 尾部、覆盖不了 legacy approval 且无检测价值 → 不够；c) 对未发生故障投机加固、legacy 无值可传 → 过度；b) 最小且普适。②修的是「臆想风险」非「已发故障」——删除方案后观察到的 72h 故障（零 `no_run_id`）不变，故**诚实定性 P2 预防**，不当 P0/P1 已损。负向探针：我试过用「更简单一档（a：只补 2 行显式传参）」能否解决——**不能**，因为它覆盖不了 legacy approval 非 context 路径、也补不上「失效检测」这个根治点。

**Q6 是否有可复用逻辑？** —— **通过**。正向依据：知识图谱/代码库检索——`logger.warning` 级别切换无等价既有件，但 warning 埋点先例在 `flush_temp_workspace` 已大量存在（`[WorkspaceFlushConflict]`/`[WorkspaceFlushRevertGuard]` 均 warning）；本方案复用同一 `logger`（loguru）与同一日志 key 命名约定。负向探针：我查过是否已有「no_run_id 计数/告警」等价逻辑——`[RunWorkspaceRefreshSkipped] reason=no_run_id` 只有 L2482 一处 info，无既有告警件，确认需新增。

**Q7 是否破坏 Clawith 特性？** —— **通过**。正向依据：逐条过宪法 C1–C6——C1 证据先行（双源已核）；C2 最小改动（只日志级别+2 传参）；C3 契约与状态所有权（零契约变更，不碰 checkpoint/run 状态）；C4 测试证行为（tdd 2 用例）；C5 保留既有工作（不删任何现有路径，legacy approval 行为不变）；C6 模块化边界（单文件，不跨模块）。工作区红线——durable run/checkpoint、多租户隔离、exactly-once、前缀缓存、WS 状态机、飞书通道，均不触及（日志级别无关）。负向探针：我试过把方案对「前缀缓存稳定性」「checkpoint 语义」逐条过——warning 日志不影响任何字节/消息，不碰缓存前缀、不碰 checkpoint 写入，结论不碰。

---

## 交付结论

**评审通过**。四件套齐全，7 角度全过（含负向探针），定性 **P2 预防**（非 P0/P1 已损）。

**已实现**（tdd）：改动 ①+②（核心）+ ③（一致性收尾）+ 2 个回归测试，均落在 `backend/app/services/agent_tools.py` 与 `backend/tests/test_agent_tools_storage_workspace.py`；`ruff check` 通过、`scripts/arch-guard.sh` P0 全绿、`test_agent_tools_storage_workspace.py` 59 passed。

**Phase 5 复核**（`code-review` 双轴，并行子代理）：Spec 轴 PASS（缺项 0 / 范围外 0 / 实现错误 0，含对「明确不做」未做的确认）；Standards 轴 PASS（0 硬违规；5 条判定性意见均已核对——其中「N+1 重复告警」不成立，因 run_id=None 时 ADR-0011 同步段 `if run_id and ...` 跳过、不会二次调用 refresh；另按复核意见把 `_refresh_run_workspace_after_direct_write` docstring 补齐「missing run link now warns」）。**闭环达成**。
