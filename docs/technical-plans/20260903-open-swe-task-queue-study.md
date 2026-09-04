# Open-SWE 任务队列源码研究报告

日期：2026-09-03
状态：**完成**（分析基于本地仓库 `/Users/shubinzhang/Documents/UGit/open-swe` HEAD `1463e3705bb1989e69464b143b11b97d17eb1bb3`，分支 `main`，与 origin/main 同步、工作树干净）
定位：参考资料研究，非实现方案。对照 Clawith `agent_runs` 队列与 run 生命周期（pending→running→终态、排队中无法取消、调度/租约）。

## 0. 项目概览

- **是什么**：Open SWE（`open-swe`）——GitHub/Linear/Slack 多 surface 编程 Agent 平台。单仓 Python（`agent/` 约 255 个 .py），构建在 **LangGraph Platform**（`langgraph_sdk`）之上。
- **核心结论**：open-swe **没有自建队列/worker**。入队-分发-执行-终态生命周期整体外包给平台四原语——`runs`（run 队列）、`crons`（定时/一次触发）、`threads`（状态容器 + busy + 分布式锁）、`store`（持久化 KV）。自研增量只是三层薄胶水：**① dispatch 统一入队、② completion webhook 终态保证、③ reconcile 兜底清扫**。
- **对标关系**：Clawith 自研 `agent_runs`+`agent_run_commands`+command worker；open-swe 是「平台原语 + 薄胶水」。**可借鉴的是胶水层设计契约（幂等、fail-closed、兜底清扫、租约语义），不是队列实现**。

## 1. 统一入队契约（dispatch 层）

`agent/dispatch.py` 是所有 run 触发点的唯一入口（Slack/Linear/GitHub/dashboard/schedule/baby-sit/background），替代了历史上一堆 `runs.create`+busy-check+自建 store-queue 的分叉实现：

- `create_durable_run()`（`dispatch.py:233-275`）固定四个耐久默认值：`multitask_strategy="interrupt"`（follow-up 打断在途 run、进度由 sync checkpoint 保留后重启，后台 follow-up 改 `"enqueue"` 排队，`:242`/`:292`）、`durability="sync"`（每步 checkpoint，崩溃续跑，`:243`）、`webhook=COMPLETION_WEBHOOK_URL`（终态回调，`:262-263`）、`stream_resumable=True`（保留事件流供晚接入 dashboard 回放，`:260`）。
- `dispatch_agent_run()`（`dispatch.py:278-327`）路由到 `"agent"`/`"reviewer"` 图。
- **回环 webhook 防护**：`_resolve_completion_webhook_url()`（`:177-201`）检测 relative/localhost 回环 URL，拒绝则降级为不挂 webhook（带告警）——否则平台会 422 掉每个 run。这是「失败不破坏创建主路径」的 fail-soft 样本。

## 2. 终态保证（completion webhook + 幂等失败回复）

`agent/completion.py` 承接平台回调 `/webhooks/run-complete`，核心 `handle_run_completion()`（`:290-344`）：

- 终态判定 `_TERMINAL_FAILURE_STATUSES = {"error","timeout"}`（`:40`）；`interrupted` **刻意排除**——interrupt 策略下正常 follow-up 打断前一 run 是健康流程非失败（`:36-39` 注释）。
- 失败回复按 `run_id` 幂等去重（`_FAILURE_REPLY_RUN_IDS` 有限列表上限 20，`:42-47`/`:182-190`）；无 run_id 回退 thread 级 flag（`:324-330`）。webhook token fail-closed：无 secret 即拒绝一切回调（`:61-70`）。
- 乱序防护 `_settle_code_channel_session()`（`:217-234`）：完成回调到达时若已有更新的 pending/running run，不把 UI 置回 active。

**对 Clawith**：对应「部署杀在途 run → 全量重放 → 分叉撞幂等账本」教训。open-swe 解法是「平台回调 + run 级幂等去重」，Clawith 已改「复用账本终态 + 审计」。可借鉴轻量幂等记账（去重 id 落 thread metadata 有限列表）与「interrupted ≠ failure」语义边界。

## 3. 兜底清扫：pending 超时取消（reconcile）

`agent/reconcile.py` 是 webhook 失效时的安全网，与 Clawith「排队中 pending 无法取消」痛点最直接对照：

- `reconcile_stale_runs(max_age_seconds=1800)`（`:37-119`）：分页遍历 `busy` 线程，列其 `pending` run，`created_at` 超龄 30 分钟者 `runs.cancel_many(action="interrupt")` 取消释放线程；每线程 try/except 隔离。由 `agent/scheduler.py:36-37` 的 `reconcile` 任务周期触发（`get_scheduler()` 是单 `launch` 节点的状态图，`:57-62`）。

**对 Clawith**：open-swe 是**粗粒度周期清扫**；Clawith 两层修复是**同步精确预抢**——`cancel_run()` 入队 cancel 后立即 `reject_unstarted_run_for_cancel()`（`persistence.py:896-948`）把「早于 cancel 的 pending/过期 claimed start」同步置 `rejected`/`cancelled_before_start`，规避「worker 先 start 后 cancel」的 FIFO claim 竞态（`adapter.py:380-390` 注释）。**Clawith 同步方案更强，是加分项而非缺口**。

## 4. 并发 worker 数量控制

- **沙箱后台命令**：`agent/tools/background_execute.py` 定义 `MAX_ACTIVE_TASKS = 4`、`DEFAULT_TIMEOUT_SECONDS = 3600`、`MAX_TIMEOUT_SECONDS = 86400`、`TASK_TTL_SECONDS = 604800`（`:19-24`）。`_launch_command()`（`:144-162`）在沙箱内 `mkdir` 抢锁 + 数 `state.json` 的 `"running"` 计数，超限拒（exit 72）。
- **每线程 cron 去重**：`ensure_background_task_cron()`（`background_tasks.py:27-53`）按 `metadata.kind+thread_id` 搜 cron，重复删除、无则建每分钟 cron。
- **调度 lane**：Clawith 有 `scheduling_lane_key`/`lane_held`/`lane_claimed_at`；open-swe 对应平台 `enqueue`（同线程 FIFO）+ baby-sit 的 `_watch_lock`（`baby_sit.py:46-67`，`threads.create(if_exists="raise", ttl=5min)` 实现 5 分钟 TTL 锁，`WATCH_LOCK_TTL_MINUTES=5`）。

**对 Clawith**：并发数 Clawith 显式可配 `AGENT_RUNTIME_COMMAND_CONCURRENCY=10`（`config.py:174`，10 个 `RuntimeCommandDaemon`，`worker_service.py:895-905`）。open-swe 的「cron 按 metadata 去重删多余」与 Clawith 唯一约束 `uq_agent_run_commands_run_idempotency`（`agent_run_command.py:46`）同思路。

## 5. 超时 / 取消 / 重试 / 失败恢复（middleware 栈）

装配在 `agent/server.py:1292-1348` 的 `create_deep_agent(middleware=[...])`：

| 机制 | 文件:行 | 要点 |
|---|---|---|
| 整 run 软超时 | `middleware/timeout_wrapup.py:43-74` | `TimeoutWrapupMiddleware`（默认 45min）到点注入 `<time_limit_warning>` 让模型「立刻收尾」，软超时非硬杀 |
| 单调用墙钟超时 | `middleware/model_call_timeout.py:42-60` | 默认 900s，把 provider 静默挂起转 `TimeoutError` 交 fallback/上报，防 run 无声卡死 |
| 工具重试 | `middleware/task_retry.py:62-66` + `server.py:1324-1331` | `ToolRetryMiddleware(max_retries=2, tools=["task"], retry_on=task_retry_on)`，仅 5xx/408/409/425/429/529 与传输类异常 |
| 模型调用硬上限 | `server.py:1314` | `ModelCallLimitMiddleware(run_limit=5000, exit_behavior="end")` |
| 模型 fallback | `server.py:1338` | 主模型失败降级备模型 |
| 沙箱失联告警 | `middleware/sandbox_circuit_breaker.py:139-186` | 沙箱无响应时向触发 channel 告警，**不擅自换新沙箱**（换空箱=毁未提交工作） |

**异步退避重试**（`session_cost.py`）：`schedule_session_cost_refresh()`（`:57-82`）用 `runs.create(..., after_seconds=_RETRY_DELAYS_SECONDS[attempt], on_completion="delete")` 实现无状态延时重试，退避 `(15,30,60,120,240)`（`:21`），`run_session_cost_refresh()`（`:150-182`）判定 `pending→retry_scheduled→exhausted`。

**后台任务取消/失败终态**：终态集 `{completed,failed,timed_out,stopped,lost}`（`background_execute.py:19`）；`_runner()`（`:31-141`）沙箱内起 subprocess+selectors 轮询，`stop`/`timed_out` 走 `killpg(SIGTERM→SIGKILL)`（`:120-129`）；`_control_script.load()` 把「runner 进程已死但状态 running」标 `lost`（`:180-194`）——对应 Clawith 孤儿 tool 结算 `tool_lease_reconcile.py`（docstring `:1-25`，`_owner_run_terminal` 判 lane 释放即终态 `:86-109`）。

## 6. 幂等与持久化

- `agent/store.py` 的 `TypedStore`（`:128-186`）把 namespace 绑 Pydantic 模型读回即校验；错误策略「**missing 读 None，其余失败必抛**」——store 故障≠空记录，坍缩会藏数据丢失（docstring `:1-14`）。搜索遇不可读记录跳过记日志，单条坏不打垮整表（`:174-186`）。
- 后台通知用 `mkdir`/`rmdir` 做**原子认领**：`_claim()`（`background_tasks.py:92-95`）`mkdir notify.claim` 成功即独占；`_mark_delivered()` 把 `notify.claim` `mv` 成 `notify.done` 即终态幂等（`:105-112`）；`_control_script` 据目录推导 `notification` 状态（`background_execute.py:226-231`）。
- 调度状态两个 store namespace（`agent_schedules`/`agent_schedule_run_state`，`dashboard/schedules.py:33-34`），cron 创建失败回滚删 record（`:340-345`）。

## 7. Clawith 侧对照（工具核实）

**表结构**（`backend/app/models/agent_run.py`、`agent_run_command.py`）：

- `agent_runs`：产品侧**身份/投递事实**表，**无 status 列**。含 `source_type ∈ {chat,trigger,task,a2a,heartbeat}`、`run_kind ∈ {foreground,background,delegated,orchestration}`、`delivery_status ∈ {not_required,pending,delivered,failed}`、`runtime_thread_id`、`graph_name/version`、排队 lane 三件套 `scheduling_lane_key`/`scheduling_position_created_at`/`scheduling_position_id` + `lane_held`/`lane_claimed_at`（`:27-159`）；幂等唯一索引 `uq_agent_runs_source_execution`（`:174-179`）。
- `agent_run_commands`：**持久化命令收件箱**，`command_type ∈ {start,resume,cancel}`、`status ∈ {pending,claimed,applied,rejected}`、`claim_expires_at`/`claimed_by`/`attempt_count`/`idempotency_key`/`deferred_until`（`:25-105`）；唯一约束 `uq_agent_run_commands_run_idempotency`（`:46`）。

**run 生命周期由 checkpoint + 生命周期事件推导**（非 `agent_runs` 列）：终态事件集 `{run_completed,run_failed,run_cancelled}`，`run_is_terminal()`（`event_stream.py:305-313`）取最新非投递生命周期事件判终态。

**调度/租约**（`persistence.py`、`command_worker.py`）：

- `claim_next_command()`（`persistence.py:701-739`）：`_claim_statement()`（`:585-662`）按 `(created_at,id)` 全局 FIFO + `with_for_update(skip_locked=True)` 抢最早命令；start 额外走 `_acquire_start_lane()`（`:665-698`）抢排队 lane（唯一部分索引 `uq_agent_runs_active_lane` 防双 holder）。认领后 `status=claimed`、`claim_expires_at=now+60s`（`AGENT_RUNTIME_COMMAND_CLAIM_TTL_SECONDS=60`，`config.py:175`）。
- **心跳韧性**：`_heartbeat()`（`command_worker.py:520-561`）执行全程每 20s（`CLAIM_RENEW_SECONDS=20`）续租，连败 `_max_heartbeat_failures` 次才停——修掉「60s 短租约跨 30min 图执行」反模式。
- **结算解耦**：`mark_command_product_synced()`（`persistence.py:742-749`）+ post-checkpoint handler 分离「提交 checkpoint」与「产品侧结算」。
- **cancel pending 两层修复**：`cancel_run()`（`adapter.py:367-391`）入队 cancel 后若 created 即 `reject_unstarted_run_for_cancel()`（`persistence.py:896-948`）同步预抢。
- **daemon 编排**：`running_runtime_worker_context()`（`worker_service.py:875-944`）起 10 个 `RuntimeCommandDaemon` + Supervisor（stall=300s）+ ChannelDelivery/AsyncToolPoll/ToolLeaseReconcile 旁路 daemon。

## 8. 可迁移点 → Clawith 映射

| # | open-swe 机制（文件） | Clawith 对标点 | 可借鉴要点 |
|---|---|---|---|
| 1 | `create_durable_run` 统一入队（`dispatch.py:233-275`） | `RuntimeCommandIntake`（`adapter.py`） | interrupt=正常流程非失败；stream_resumable 保证外部触发 run 可回放 |
| 2 | completion webhook + run 级幂等失败回复（`completion.py:290-344`） | 部署杀→全量重放→分叉撞幂等账本 | run_id 落 thread metadata 有限列表去重；乱序完成不清理新 run loading |
| 3 | pending 超时周期清扫（`reconcile.py:37-119`） | pending 无法取消两层修复 | open-swe 粗粒度；**Clawith 同步 `reject_unstarted_run_for_cancel` 更强** |
| 4 | 软超时包裹（`timeout_wrapup.py:43-74`） | DeepSeek 长跑收尾 | 到点注入收尾指令，软超时优于硬杀 |
| 5 | 单调用墙钟超时（`model_call_timeout.py:42-60`） | provider 静默挂起→无声卡死 | 挂起转 TimeoutError 交 fallback/上报 |
| 6 | `lost` 终态 + 孤儿租约结算（`background_execute.py:180-194`/`tool_lease_reconcile.py`） | tool lease reconcile（已自研） | 「进程死但状态 running→保守标 lost，绝不重放原 receipt」 |
| 7 | `TypedStore` missing≠empty（`store.py:1-14`/`:128-186`） | store 读取层 | 单条坏记录跳过不拖垮整表；缺失读 None、其余失败必抛 |
| 8 | 无状态退避重试 `after_seconds`+退避序列（`session_cost.py:21`/`:57-82`） | 延时/退避任务 | 一次 run+`after_seconds` 造退避，`pending→retry_scheduled→exhausted` |
| 9 | 后台并发 `MAX_ACTIVE_TASKS=4`+mkdir 认领（`background_execute.py:21`/`:144-162`） | 并发 worker 控制 | `CONCURRENCY=10` 已显式；cron 按 metadata 去重删多余可参考 |

## 9. 局限（诚实记录）

- open-swe **把队列/worker/生命周期外包给 LangGraph Platform**，并发、锁、checkpoint 全在平台内部；对自研队列的 Clawith，其队列实现几乎无可抄，仅胶水层设计契约可迁移。
- 本次未深入：`slack/webhook.py`、`github/webhook.py`（channel 入队）、`review/`（reviewer 队列）、`webhooks/common.py`（5.5 万行聚合路由）、沙箱 provider 层。
- open-swe 单仓单部署，多租户隔离/限流/审批非其重点（Clawith 已自研更深）。
- reconcile 是 30 分钟窗口粗粒度兜底，时效性不如 Clawith 同步预抢。
