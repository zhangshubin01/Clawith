# 命令重试预算烧尽后永久半态卡死 — 深度分析

日期：2026-08-26（事故 run `fe8d6e16`，2026-08-26 02:41 UTC，Android 工程师06「再优化一下app项目」）
状态：根因已实锤（含代码级时序推演），修复方案 A+B 待决策
关联：`command-claim-lease-loss-race`（d1dabb29）、SECRET_KEY 环境事故（同窗口诱因）、`cancel-running-run-gap`

## 1. 事故时间线与证据

- 02:41:59 创建 run，start 命令 `681bede8` claim lane 成功；随后模型调用 `HTTP 401`（诱因=SECRET_KEY 占位符解密事故，已修复）
- 401 是 non_retryable，模型步毫秒失败；start 命令 5 次 attempt 快速烧光（`max_attempts=5`）
- **02:44-03:58 约 20+ 分钟**：命令停在 `pending + attempt_count=5 + claim_expires_at=NULL`，无人再 claim，前端「思考中」永续
- 手动重置 attempt 后 20 秒内再烧 5 次复现；claim 查询快照（8s +20 次）证明 worker 活着、SQL 每轮都在跑——是**门槛挡死**，不是 worker 卡死

## 2. 缺陷语义建模（代码级时序）

命令生命周期：`pending →(claim)→ claimed →(begin_attempt: attempt+1)→ 执行 → applied | rejected | (失败) release_for_retry→pending`。

关键事实（已逐一验证）：

| 位置 | 代码 | 语义 |
|---|---|---|
| `persistence.py:640` | claim 门槛 `attempt_count < max_attempts` | 被选中时 attempt ≤ max-1 |
| `command_worker.py:1059` | `exhausted = command.attempt_count >= self._max_attempts` | 判定在 claim 后、`_begin_attempt` 前 |
| `persistence.py:806` | `begin_command_attempt`：attempt ≥ max 抛 `command_reconciliation_required`（注释："must be quarantined"） | 设计者意图=烧尽后隔离处理 |
| `persistence.py:1050` | `release_command_claim`：回 pending **不改计数** | 失败重试靠计数自然增长 |
| `command_worker.py:1081` | `exhausted → _process_exhausted_locked else _process_locked` | 终态化路径 |
| `command_worker.py:1115+` | `except RetryableCommandError → release_for_retry`；`except Exception → release_for_retry("command_execution_failed") + raise` | 任何异常都回 pending 重试 |

**死锁证明**（恒等式矛盾）：
- claim 选中条件 `attempt < max` ⇒ 被选中时 attempt ≤ max-1
- exhausted 判定 `attempt >= max` ⇒ 被选中时恒 false
- ∴ `_process_exhausted_locked`（终态化路径）**不可达**；`begin_command_attempt` 的 quarantine 抛错分支同样是死代码
- 唯一结局：attempt = max 的命令停在 pending，无任何代码路径处理它 —— 半态卡死

## 3. 根因三层

**L1（核心）**：claim 门槛 `<` 与 exhausted 判定 `>=` 自相矛盾，quarantine 设计意图在实现上不可达。设计意图本身是对的（`command_reconciliation_required` "must be quarantined"），是门槛多打了 `<` 而非 `<=`。

**L2（放大器）**：`except Exception → release_for_retry` 使 **non_retryable 失败也烧满全预算**。模型 401 不会自愈，5 次重试纯浪费且加速进入半态。理想语义：命令级重试只应服务「可自愈」错误（transient、锁竞争、checkpoint 未稳定）；确定性失败应直接 reject + 通知。

**L3（体验）**：半态期间无任何用户通知。run 无终态事件、delivery 永不发生、前端只显示「思考中」。（`_reject → _mark_rejected → _rejection_handler` 的通知链路存在，只是到不了。）

**L4（待验证的次层）**：non_retryable 模型步失败（401）未在图内确定性转 terminal。按 `6dc6f33c` 设计，non_retryable 应让 run 快速失败（terminal checkpoint → 命令 applied → delivery failed 通知）。本次 401 后图异常终止（无 terminal checkpoint、无 run_completed 事件），命令层只得走 `except Exception`。修复 B 前需验证：`node_executor` 对模型步失败的捕获是否应把 run 标 failed 并落 terminal checkpoint。

## 4. 参考资料对照

- **command-claim-lease-loss-race（d1dabb29，本仓库先例）**：教训①短租约（claim）与长互斥（thread lock）分工——本缺陷同源：attempt 预算是「业务重试预算」，其终态化必须**不依赖再次 claim**（d1dabb29 的 B「结算解耦」同思路：applied 结算在锁内、锁即权威）。教训②「结算权威」：reject 在锁外也有调用、claim 是唯一权威——修复后 exhausted-reject 仍走 `_mark_rejected`（含 claim 校验），无需改结算语义。
- **cancel-running-run-gap（4b249a56）**：superseded/终态化的先例——半态命令的终态化手法一致（reject + lane 释放 + 事件）。
- **参考项目清单（reference-projects）**：LangGraph/OpenHands/deepagents/gemini-cli **均无「命令预算层」**——这是 Clawith 自研层（上游 `checkpoint-postgres` 无先例）；Anthropic《Building Effective Agents》的「失败快速反馈人类」原则被 L3 违反。结论：修复无上游可抄，按本仓库自己的原语（claim/attempt/reconcile）闭环。

## 5. 修复方案

### 方案 A（核心，小改动）：让 quarantine 路径可达 + exhausted 内任何失败都终态化

1. `_claim_statement` 门槛 `<` → `<=`（同时 `begin_command_attempt` 的抛错分支可保留作防御）。
   语义：max 次执行尝试 + 第 max+1 次 claim 专门用于终态化（quarantine 处理），不执行。
2. **坑 1（必须一起修）**：`_process_exhausted_locked` 里 `_validate_checkpoint` 对 inconsistent checkpoint 抛 `RetryableCommandError`，会被 `run_once` 的 `except RetryableCommandError → release_for_retry` 接住——attempt 不变仍 =max → 再 claim → 再抛 → **无限循环**。修法：exhausted 处理中的任何 `RetryableCommandError`/`CommandWorkerError` 都改为 `_reject`（error_code 保留原码或 `reconciliation_required`），不再 release。
3. **坑 2（语义核对）**：exhausted 时 checkpoint 为 waiting/terminal → `_mark_applied`（现状）。语义正确：图已到稳定边界，命令视为完成（waiting 场景用户本就在等回复）。注意 `command_type != "cancel"` 分支保留现状。
4. 存量半态清理：修复上线时若已有 attempt=max 的 pending 命令，claim `<=` 后会被自动拾起终态化，无需迁移。

改动面：`persistence.py`（1 行）+ `command_worker.py`（exhausted 异常处理）+ 回归测试。
风险：低。claim 多选中「待终态化」命令时不再执行（exhausted 分支不 begin、不跑图）；reconcile 幂等（`read_for_command` 只读）。

### 方案 B（增强，中改动）：non_retryable 失败不烧预算

`except Exception` 分支按错误类别分流：
- `LLMError`/模型层 non_retryable（401/403/400 参数错）→ 直接 `_reject`（error_code 沿用 `model_call_failed`）+ 用户通知，不再 release；
- transient/未知 → 保留 release_for_retry（历史教训：重试曾自愈，见 claim-lease-race）。

前置：验证 L4——模型步失败在图内的传播与 terminal 落库现状；若图内已能确定性转 terminal，命令层理想路径是「执行结束 → checkpoint terminal → applied → delivery failed」，B 反而只需保证 401 不中途炸出图。
改动面：`command_worker.py`（异常分流）+ 可能 `node_executor`/`model_step_service`（L4 验证结果决定）。
风险：中。错误分类边界要防误伤 transient（如 429 限流、DNS blip——后者 `6dc6f33c` 已归 RETRYABLE 且有 run 级退避）。

### 方案 C（收尾，零代码）：手动终态化手法脚本化

已在本事故中使用：`UPDATE commands SET status='rejected'` + `UPDATE runs SET lane_held=false, delivery_status='failed'` + 插 `run_failed` 事件（`agent_run_events` 需 `summary` NOT NULL + `idempotency_key` NOT NULL）。可沉淀为 `app/scripts/terminalize_stuck_commands.py` 供运维使用；方案 A 落地后不再需要。

## 6. 推荐与决策点

**状态（2026-08-26）**：方案 A 已实现并通过 code-review（Standards+Spec 双轴，评审发现的活口均已闭合）：
- claim 门槛 `<=` + exhausted（quarantine）处理中任何失败终态化 reject（含 `_load_run` 失败路径与无 `.code` 异常兜底 `reconciliation_required`）；
- reject 只透出稳定 error_code（`_terminal_error_code`，≤100 字符校验），内部异常原文仅留日志；
- ThreadLockNotAcquired（瞬态锁竞争）保持 retry 语义；
- 回归测试 4 个新增用例（parametrize×3 + load_run 失败）+ SQL 文本断言更新；全量 2978 passed。

**未决**：方案 B（non_retryable 失败不烧预算，依赖 L4 验证）、方案 C（运维脚本化）、`max_attempts=5` 默认值评估、ADR 落不落。
