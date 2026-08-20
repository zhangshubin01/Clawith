# Checkpoint 保留策略深度研究（2026-08-19）

> 测试环境（用户 2026-08-19 确认）。数据快照取自当日实测。

## 1. 现状与问题

| 表 | 行数 | 大小 | 说明 |
|---|---|---|---|
| `langgraph_checkpoint.checkpoints` | 43,426 | 74MB | 每线程平均 **80.3 个**；top 线程 1815/1729/1341 个 |
| `langgraph_checkpoint.checkpoint_blobs` | 57k | **452MB** | 每个 checkpoint ~1.8 个 blob，随 checkpoint 线性增长 |
| `langgraph_checkpoint.checkpoint_writes` | 97k | 79MB | 每次 checkpoint 的待写/历史写记录 |

- 全部 checkpoint 的 `checkpoint_ns` 都是 `''`（单命名空间，无 planning 分支残留）。
- **无 `created_at` 列**，时间只能从 `checkpoint->>'ts'`（jsonb 内置字段，100% 存在）或
  `metadata->>'clawith_run_id'`（100% 存在）join `agent_runs.created_at` 获得。
- 年龄分布：7 天内 11,560（27%），7–30 天 31,269（72%），>30 天 0。
  **年龄型策略当前无效，按线程压缩才是主杠杆**。
- 增长速率：几小时内 +1,200 个 checkpoint（测试环境活跃时 ~数千/天）。

## 2. 上游 API 现状（langgraph-checkpoint-postgres 3.1.2 / langgraph-checkpoint 4.2.0）

- `AsyncPostgresSaver.aprune` / `BasePostgresSaver.prune` 均为 **`raise NotImplementedError`**——
  上游没有可用的保留实现，需要自研。
- 上游 docstring 警告 DeltaChannel：天真裁剪会截断 `checkpoint_writes` 链，
  使 delta 通道**静默重构为空**（无报错）。**本图不适用**：
  `RuntimeGraphState` 唯一聚合通道是
  `messages: Annotated[list[AnyMessage], add_messages]`（全量值重写，非 `BinaryOperatorAggregate`），
  无任何 `DeltaChannel`。安全结论：按 checkpoint 级联删除 rows + writes + 孤儿 blob 不会破坏状态重构。

## 3. 读取依赖分析（删了谁会受影响）

| 读取方 | 读取方式 | 保留策略下是否安全 |
|---|---|---|
| 新消息续跑 `read_latest` | `aget_state_history` filter clawith_run_id limit 1 → 线程最新 | ✅ 保留最新即安全 |
| `read_for_command`（命令级恢复） | 按 clawith_command_id 找历史 checkpoint | ⚠️ 旧命令的 checkpoint 被删后返回 None → `execute(checkpoint=None)` 从**线程最新态**续跑。行为变化：旧命令重试不再从原状态恢复，而是从当前状态续跑（有幂等键 + control_guard 兜底） |
| `product_reconciler.read_checkpoint(applied_checkpoint_id)` | 定点读 applied checkpoint | ✅ 待同步命令都是近期命令，其 applied checkpoint 即线程最新附近；护栏=**有 pending 命令的线程不剪** |
| 调试 UI `get_session_runtime_state` | `get_run_state` → 最新 | ✅ 只读最新 |
| 中断恢复（waiting_user / interrupts） | 线程最新 checkpoint | ✅ 保留最新即安全 |

**结论**：keep_last_n（n≥1）不破坏任何活跃流程；唯一语义变化是「很旧的命令重试从线程最新态续跑」，
测试环境可接受，生产上如需「从任意历史消息重试」需另行设计 checkpoint 归档。

## 4. 方案对比

| 方案 | 收益 | 风险/成本 | 结论 |
|---|---|---|---|
| A. **运维脚本 keep_last_n**（每线程留最近 N 个，N 默认 3） | 43k 行→~1.6k 行；452MB→~10MB；与现有 scripts/ 模式一致 | 低；dry-run 先行；DDL 之外的数据操作按 alembic 规范放脚本 | ✅ **推荐** |
| B. 运行时钩子（每次写后同步剪） | 实时防增长 | 热路径开销 + 并发风险（线程锁已存在，可借力但复杂） | ❌ 测试环境收益低 |
| C. 纯年龄删除（>30d 删线程） | 简单 | 当前 0 命中；且删整线程=丢会话续跑能力 | ❌ 现阶段无效 |
| D. 等上游实现 prune | — | NotImplemented 且 DeltaChannel 警告说明上游也在犹豫 | ❌ 不可等 |

## 5. 推荐实现（方案 A 细节）

脚本 `backend/app/scripts/prune_runtime_checkpoints.py`（沿用 orphan 清理脚本模式）：

1. **候选线程**：同时满足
   - 该线程无 `queued/claimed` 命令（join agent_run_commands → agent_runs.runtime_thread_id）；
   - 最新 checkpoint 的 `ts` 早于 N 天（默认 3 天，防误伤活跃线程）。
2. **排序**：`ORDER BY (checkpoint->>'ts')::timestamptz DESC, checkpoint_id DESC`（checkpoint 内置 ts，无 created_at 列也可序）。
3. **级联删除**（每线程，事务内）：
   - `checkpoint_writes`：删 `checkpoint_id IN (被删集合)`；
   - `checkpoints`：删 row_number() > keep 的行；
   - `checkpoint_blobs`：**版本可达性 GC**——删除该线程中
     `(channel, version)` 不被任何保留 checkpoint 的 `checkpoint->'channel_versions'` 引用的行。
     这是最大头（452MB），不能只删 rows。
4. **参数**：`--dry-run`（默认）/ `--apply` / `--keep-per-thread 3` / `--min-age-days 3`。
5. **收尾**：大清理后建议 `VACUUM (ANALYZE)`；若想回收磁盘文件可择期 `VACUUM FULL`（需独占锁，测试环境可接受）。
6. **测试**：pytest 覆盖排序/候选过滤/版本可达性 GC 逻辑（用临时表或 mock SQL 文本）。

## 6. 风险护栏汇总

- pending 命令线程不剪（护栏 1）；
- 最新活动 <3 天线程不剪（护栏 2）；
- dry-run 默认 + 先出清单人工确认（护栏 3）；
- 保留 N=3 而非 1，调试留余量；出问题可重建（测试环境无备份包袱，但重建成本=旧会话续跑从空态开始）。

## 7. 落地顺序

1. 实现脚本 + 测试（本文批准后）。
2. dry-run 出清单 → 用户确认 → `--apply`（预期 452MB→~10MB，checkpoints 43k→~1.6k）。
3. `VACUUM ANALYZE`，观察一周增长曲线，决定是否做成定时任务（PenguinHarness scheduled task 或 cron）。

## 8. 根因与根治方案（2026-08-19 补）

**根因**：LangGraph 默认每个 super-step 全量落 checkpoint。Clawith 的控制循环
（control_guard → model/tool/verify → control_guard）每轮消息产生 4-6 个 super-step，
全部落库——平均 80/线程、top 1815。保留脚本只是症状治理。

**上游钩子不可用**：`should_checkpoint` 在 langgraph 1.2.11（镜像最新版）与
langgraph-checkpoint 4.2.0 中均不存在；官方文档对"checkpoint 无限增长"的答复也只是
cron 清理。升版等上游 = 不可行。

**根治方案：SelectiveCheckpointSaver（checkpointer 包装层，自研 selective persistence）**

在 `create_checkpointer` 处包一层，选择性落库：

- **必须持久**（essential）：
  - 有中断：`checkpoint["pending_sends"]` 含 task id `__interrupt__`（waiting_user 状态永远不丢）；
  - 运行终态/waiting：`channel_values["status"]` 为终态或 waiting_user；
  - 水位：每 K 个 super-step（进程内计数器，K 默认 5，配置项）；
  - 强制标志：config metadata `clawith_ckpt_essential=true`（平台在 settle/apply 边界显式要求）。
- **跳过时**：`aput` 不落库（返回上一 essential checkpoint 的 config），并吞掉配套的
  `aput_writes`（避免 writes 挂到不存在的 checkpoint）；内存态继续，下一个 essential
  一次性落全量。
- 读取方法（get/aget/list/alist/delete_thread/…）原样委托。
- 单 worker 部署下进程内计数安全（记忆已确认生产单 worker）；重启后计数清零，
  最坏多落几个 checkpoint，无害。

**预期效果**：每 run 落库从数十个降至 3-6 个（初始 + 水位 + 终态/wait），
checkpoint 增长速率降 ~90%。

**风险与对策**：
- 崩溃丢最多 K 步 → 从上一 essential 续跑重执行（LLM 调用重计费；工具副作用已有
  幂等表兜底）——K 可配置，成本/持久性权衡显式化；
- 调试 UI 中途状态滞后到上一水位点——waiting_user 永远实时，可接受；
- 与 C1 无冲突：产品状态仍只存产品表，checkpoint 只是执行状态快照。

## 9. 实现状态（2026-08-19 已落地，默认关闭）

- `backend/app/services/agent_runtime/selective_checkpointer.py` — SelectiveCheckpointSaver
  包装层（essential 判定 + 跳过时吞 writes + 进程内计数含 4096 线程上限驱逐）。
- `create_checkpointer` 按 `CHECKPOINT_SELECTIVE_ENABLED`（默认 False）包装；
  `CHECKPOINT_WATERMARK_STEPS` 默认 5。
- 测试 `tests/test_selective_checkpointer.py` 16 用例全过；checkpointer 相关回归 26 用例全过；
  ruff + arch-guard P0 全绿。
- 启用方式：测试环境 `.env` 加 `CHECKPOINT_SELECTIVE_ENABLED=true` 重启后端，
  观察 checkpoint 增速（预期降 ~90%）。
- 数据修正：早前"checkpoint_blobs 慢 INSERT 507ms"为毫秒单位误读，实际 mean 0.48ms、
  max 11ms——checkpoint 写入本身无性能问题，本方案收益在存储增长与生产铺路。

