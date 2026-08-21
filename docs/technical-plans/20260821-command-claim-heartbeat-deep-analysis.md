# 命令 claim 租约丢失竞态深度分析（结合参考资料）

> 事故：2026-08-21 01:02–01:30 UTC，agent `82dc9a8a` run `5899881f`（"优化一下这个项目"）在重启恢复后执行期间，命令 `67901e70` 的 claim 在长模型调用中途过期，引发 split-brain（thread_lock_busy），`_mark_applied` 一度失败，最终靠重试自愈（attempt_count=2）。
> 结论先行：**60s 短租约跨 30min 图执行是反模式；durability 应来自 checkpoint 幂等续跑，互斥应来自 advisory lock（崩溃自动释放），而非长时持有的 claim 租约。**

## 一、事故时间线与证据

| 时刻(UTC) | 事件 | 证据 |
|---|---|---|
| 01:02:40 | 一次 deepseek-v4-flash 模型调用开始 | `RuntimeModelRequest` 日志 |
| 01:03:30 | larksuite 域名 DNS `Temporary failure in name resolution` | feishu WS 重连失败日志 |
| 01:02:40→01:26:08 | **~23.5min 无任何业务日志**（仅 5 条 feishu 重连） | 全量日志 grep |
| 01:26:08 | 心跳 `_renew_claim` 失败：`command is not currently claimed by this worker` | command_worker.py:484 |
| 01:26:54 | 模型调用 ReadTimeout（`attempt=1/4`） | `RuntimeModelRetry` 日志 |
| 01:30:19 | 图执行完成（checkpoint `1f19cffd` 已落）→ `_mark_applied` 失败（claim 已丢） | command_worker.py:934 |
| 01:30:20 | 重试自愈：第二次 claim → 识别 terminal checkpoint → `applied`（attempt_count=2） | DB 查询 |

关键 DB 快照：命令 `67901e70` 在事故窗口内呈 `status=pending, claimed_by=None, error_code=thread_lock_busy, attempt_count=1`——即被另一 worker 抢注后撞上 thread lock 又释放。

## 二、问题本质：租约粒度与工作量不匹配

Clawith 的命令生命周期 = `claim（60s TTL，20s 心跳续期）→ begin_attempt → 抢 thread lock → 执行图（可 30min+）→ mark_applied`。

- **claim 租约**（`agent_run_commands.claimed_by + claim_expires_at`）：60s TTL，心跳每 20s 续期。
- **工作量**：单次图执行可 30min+（Android 构建 `timeout=1800s`，模型 ReadTimeout 实测可挂 24min）。
- 结论：claim 必须**连续成功续期 ~90 次**才不掉，任何一次抖动 → 过期 → split-brain。

而 `_mark_applied`（command_worker.py:934 → persistence.py:850 `_require_claimant`）在**图执行结束后**仍严格要求 claim 仍被本 worker 持有——这正是脆弱点：它用「短租约」去保护「长工作」，尽管 worker 此时已经持有更强、且崩溃自动释放的 advisory lock。

## 三、参考资料对照

按 `reference-projects` 完整清单逐类对齐（本地源码 + 开源实战 + 官方文档）：

### 3.1 LangGraph 官方（一手，本地源码 `/Users/shubinzhang/Documents/UGit/langgraph`）

- **durability 模型 = checkpoint，不是租约**。官方文档「Persistence / Fault tolerance」明言 checkpointer 的用途之一是 "recover from a failure"，即崩溃后从 checkpoint 续跑；节点级 retry/timeout/error-handler 都**作用在单次 attempt 内**，没有「跨 30min 长时持有的租约」概念。
- **并发是调用方自己的事**。源码 `libs/checkpoint-postgres/.../aio.py`：`AsyncPostgresSaver` 只用进程内 `asyncio.Lock` 做并发控制，**没有任何 DB 级 advisory lock / 租约 / 心跳**（全库 grep `advisory_lock|pg_try_advisory` 零命中）。
- 即：**LangGraph 把「崩溃恢复」交给 checkpoint 幂等续跑，把「互斥」交给调用方**——Clawith 的 advisory lock（`thread_lock.py`）正是为此正确补上的一块；但 Clawith 又额外叠了一层「跨长时执行的 claim 租约」，LangGraph 没有这层，也从不依赖这层。

### 3.2 deepagents（本地源码 `/Users/shubinzhang/Documents/UGit/deepagents`）

- `libs/talon/.../cron/scheduler.py` 的 `PersistentCronScheduler`：单进程 ticker + `CronRepeat.claim()` 是**单调递增的 claim 计数器**（幂等，防重复执行），**不是租约/心跳**。job 的「已被认领」靠计数器推进，不靠 TTL 续期。
- 印证同一条原则：**幂等 claim 计数（fencing token）比 TTL 续期租约更稳**——它不需要「连续 90 次续期成功」这种脆弱假设。

### 3.3 OpenHands（本地源码 `/Users/shubinzhang/Documents/UGit/OpenHands`）

- 全库 grep `advisory_lock|heartbeat|lease|claim` 零命中：OpenHands 单进程事件驱动，靠 checkpoint/事件总线恢复，无分布式租约。

### 3.4 对比结论

| 维度 | LangGraph | deepagents Talon | OpenHands | **Clawith 现状** |
|---|---|---|---|---|
| 崩溃恢复 | checkpoint 幂等续跑 | 幂等 claim 计数 | 事件总线 + checkpoint | checkpoint（✅ 已有）+ claim 租约（❌ 冗余脆弱） |
| 互斥 | 调用方自备（无内置） | 单进程 | 单进程 | advisory lock（✅ 正确）+ claim 租约（❌ 冗余） |
| 长时执行 | 无长租约概念 | 无 | 无 | 60s 租约跨 30min 执行（❌ 反模式） |

**统一结论**：参考系里**没有任何一个项目用「短 TTL 租约 + 心跳续期」去保护长时执行**。它们要么单进程（无需跨进程互斥），要么用 checkpoint/幂等计数（崩溃安全）。Clawith 因多租户持久化 Command Inbox 而需要更强的原语，但正确做法是：**claim 只保护短临界区（claim→begin_attempt→抢锁），长执行用 advisory lock + checkpoint 幂等，结算不依赖 claim。**

## 四、Clawith 已有正确原语（未被充分利用）

1. **thread advisory lock**（`thread_lock.py`）：`pg_try_advisory_lock` session 级锁，`engine.connect()` 专用连接持有到 `finally` unlock；**连接断开即自动释放**——这正是「崩溃自动释放」的互斥原语，比 claim TTL 强得多。
2. **checkpoint 幂等结算**：`_process_locked` 读 checkpoint，若 `terminal/waiting` 直接 `_mark_applied` 不重跑图（command_worker.py:807）——事故中 self-heal 正是靠它。
3. **双活性信号已有先例**：`event_stream._worker_alive`（event_stream.py:189）已经用 `claim_expires_at` **与** `tool lease`（`agent_tool_executions.lease_expires_at`）双信号判活；但**结算路径 `_mark_applied` 仍只认 claim 单信号**，与「双信号」设计不一致。

## 五、根因链（精简）

```
长模型调用（24min ReadTimeout，无 tool lease）
  → 唯一活性信号只剩 claim 心跳
  → 心跳首次续期失败即 return（command_worker.py:487 永久停跳）
  → claim 60s TTL 过期
  → worker B 抢注 → 抢不到 thread lock → release_for_retry("thread_lock_busy")
  → 原 worker 心跳报「not currently claimed」
  → 原 worker 图跑完 → _mark_applied 要求 claim → 失败
  → 重试识别 terminal checkpoint → 自愈（attempt_count=2）
```

两个可修点：(1) 心跳 `return`（脆弱）；(2) `_mark_applied` 强依赖 claim（把短租约当长执行的权威）。

## 六、修复方案对比（按参考对齐度排序）

### 方案 B（推荐·结构根治）：结算与 claim 解耦，以 advisory lock 为权威

- `_mark_applied`/`mark_command_rejected` 不再 `_require_claimant`，改为**幂等**（已在 thread lock 内，worker 持有锁即持有所有权；保留「already applied 短路」幂等分支）。claim 仅负责 FIFO + claim 阶段的崩溃恢复。
- 优点：根治 split-brain；claim 过期对结算无害；与 LangGraph「checkpoint 幂等 + 调用方互斥」完全对齐。
- 代价：需论证「结算必须发生在 thread lock 内」这一不变量（当前已成立，可加断言/测试固化）；claim 的「可见性」语义需另由 `_worker_alive` 双信号兜底（已有先例）。

### 方案 A（最小·症状级）：心跳韧性

- `_heartbeat` 续期失败改「有上限重试」（连续 N 次才放弃）而非 `return`；配合 `_mark_applied` 对 claim 过期的宽容（幂等）。
- 优点：改动小，直接消除「一次抖动永久停跳」。
- 代价：只治标——若事件循环阻塞 >60s（本次 24min 模型调用的疑似机制）或 DB 短不可用，claim 仍会过期；重试若 claim 真被抢占会刷日志（需设上限）。

### 方案 C（最彻底·fencing token）：用单调 token 取代 TTL 租约

- 借鉴 deepagents Talon 的幂等 claim 计数 + 分布式系统 fencing token：`attempt_count` 作为单调令牌，`_mark_applied` 校验「本 worker 的 token 是否仍是最高/当前」，过期不靠时钟 TTL。
- 优点：彻底消除 TTL 时钟竞态。
- 代价：改动面大，需动状态机 + 结算 + 活性信号，属 ADR 级决策。

## 七、推荐

**先落方案 A + B 的最小交集**（两步、都可独立提交、互不阻塞）：

1. **A：心跳韧性**——`_heartbeat` 续期失败改有上限重试（不再 `return` 永久停跳），并补回归测试（模拟续期偶发失败后仍保持 claim 新鲜）。
2. **B：结算解耦**——`_mark_applied`/`mark_command_rejected` 在 thread lock 内走幂等结算，去掉对已过期 claim 的强依赖；用测试固化「结算必须持锁」不变量。

方案 C（fencing token）作为后续 ADR 单独立项（需与 `_worker_alive` 双活性信号、cancel-queued-run 等既有机制协同，不宜本此一并动）。

---

*参考资料：LangGraph `libs/checkpoint-postgres/.../aio.py`（AsyncPostgresSaver 单进程 asyncio.Lock，无 DB 租约）、官方「Persistence / Fault tolerance」文档（checkpointer=fault tolerance+recover from failure；节点 retry/timeout 作用于单 attempt）；deepagents `libs/talon/deepagents_talon/cron/scheduler.py`+`jobs.py`（幂等 claim 计数）；OpenHands（无 lease/heartbeat）。Clawith 侧：`thread_lock.py`、`command_worker.py`（_heartbeat/_mark_applied）、`persistence.py`（_require_claimant/mark_command_applied/release_command_claim）、`config.py`（CLAIM_TTL=60s/RENEW=20s）、`event_stream.py`（_worker_alive 双活性信号）。*
