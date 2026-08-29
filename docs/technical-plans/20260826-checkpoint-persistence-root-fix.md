# Selective Checkpoint 断链事故与 checkpoint 持久化根治方案

日期：2026-08-26
状态：研究结论（待评审）
关联事故：run `947a28ee`/`234c9ac7`（「再优化一下app项目」）以 `model_call_failed (KeyError: 'snapshots')` / `reconciliation_required` 失败，用户侧表现为「任务执行完没有总结」。

## 一、根因（已用真实数据 + 官方源码坐实）

### 1. LangGraph 1.2.11 的 checkpoint 持久化是「增量」设计

（证据：容器内 `/usr/local/lib/python3.12/site-packages/langgraph/pregel/_checkpoint.py:162`、`langgraph/channels/delta.py`）

- `create_checkpoint` 只把**本 super-step 版本有变化**的通道写入 `channel_values`；
- **delta 通道**（`messages`，`add_messages` reducer）默认 `snapshot_frequency=1000`：几乎从不写快照，状态**靠 `checkpoint_writes` 沿 parent 链重放**（「the ancestor walk reconstructs their state」）；
- **LastValue 通道**（`snapshots`、`lifecycle` 等）同样只在本步有版本变化的 checkpoint 中出现，恢复时**沿 parent 链找到最近的值**（实测：成功 run 的 terminal checkpoint `channel_values` 仅 4 个通道，不含 `snapshots`/`messages`）；
- 结论：**checkpoint 行、checkpoint_writes 行、parent_checkpoint_id 链三者的完整性是状态恢复正确性的硬前提。**

### 2. 选择性保存器（`selective_checkpointer.py`）在 1.x 语义下结构性错误

- 它「跳过」非关键 checkpoint 并**吞掉对应的 `aput_writes`**（`_skip_flags` → `return None`）；
- 后果 1：**writes 重放链断裂** → delta 通道（messages）历史残缺；
- 后果 2：**parent 链断裂** → 下一个 essential checkpoint 的 `parent_checkpoint_id` 指向被跳过的 checkpoint（DB 不存在）→ LastValue 通道（snapshots）无法重建 → `state["snapshots"]` **KeyError**；
- 实锤证据：run `947a28ee` 的 terminal checkpoint `channel_values` 只有 `['branch:to:terminal', 'lifecycle']`，`parent_checkpoint_id=NULL`，整链无 `snapshots`；后端日志两次 `KeyError: 'snapshots'` → `model_call_failed`。

### 3. 为什么 wrapper 层无法安全修复

`create_checkpoint` 的通道快照由引擎在**内存 channels 对象**上计算；wrapper（checkpointer 接口）只拿到序列化后的 checkpoint dict，**无法补全被省略通道的值**。因此「跳过」在 1.x 下没有安全实现——这不是实现 bug，是**架构不可行**。

## 二、根治方案：移除选择性保存器 + 官方 `durability="exit"`

### L1（核心）：`durability="sync"` → `"exit"`，删除选择性保存器

Clawith 执行模型：**一个 run = 一个 start command**（`langgraph_driver._execute_inner` 一次 `ainvoke` 跑完整个图直到 terminal），waiting 场景由 `interrupt()` + resume command 推进。

- `"exit"` 是 LangGraph 官方 durability 模式：**执行过程中不写任何 checkpoint/writes，仅在退出（terminal / error / human-in-the-loop interrupt）时写全量 checkpoint**（含完整 writes 锚定，引擎 `_loop.py` `_exit_delta_writes` 保证「checkpoint 可见前 writes 已持久化」）。
- 收益（已按实测数据修正）：checkpoint 行数从现状 7092（selective 已启用）降到 **~每 run 1 个（历史 run 总量 1104）**，即 **~6.4×**；`checkpoint_blobs` 快照数同比例下降（messages 快照 2483 → ~1104 个）；**`checkpoint_writes` 行数数量级不变**（exit 模式仍把本 run 的 delta writes 逐行持久化并锚定 parent，且与 pending writes 双写——真正的下降来自 checkpoint 行与快照 blob，不是 writes）。
- 恢复语义完整：每个 run 边界的 checkpoint **可完整重建**（LastValue 通道直写；未达快照 cadence 的 delta 通道靠退出时全量落盘的 writes 沿 parent 链重建），无任何断链。

### L2：崩溃恢复语义（已知权衡，机制已具备）

- run 执行中途进程崩溃（部署重启/引擎重启）：中间无 checkpoint → 该 run **整体重跑**（从上一个 run 边界的 checkpoint 恢复）。
- 已有机制兜底：工具执行台账幂等（agent_tool_executions + lease 对账）、模型重调用费用可接受（DeepSeek）、command 心跳续期（d1dabb29）。
- 相比现状（sync+selective 断链 → run 必然失败），重跑语义**可预期且最终可达**。
- **已知崩溃窗口（需注明）**：全新 thread 首次 run 时，退出阶段先补写合成 empty stub checkpoint（`step=-2`）再写 final checkpoint；进程恰在两步之间崩溃会使该 thread 的最新 checkpoint 成为**空 stub**（不是上一个 run 边界），「整体重跑」将退化为「空状态重跑」。窗口毫秒级（uuid6 时序保证 stub < final）；实施时应在结算侧检测 step=-2 的 stub head 并记录告警。
- **上游未修复 bug 关联**：PR #8634（exit 模式 cancel 运行后 `get_state().tasks` 错误记录丢失，2026-08-17 关闭未合并）仍存在于 1.2.11。Clawith 的 cancel 是控制面（不读该字段），影响低；实施清单加入 cancel 回归测试。

### L3：存量清理与线程回收（**必要配套**，非可选）

- 现状：7092 checkpoints / 95MB + writes 15219 行 / 104MB + blobs 11117 行 / 659MB（合计 ~858MB；blobs 活数据实测 ~109MB，其中 messages 快照 2483 个占 100MB，其余为表结构/TOAST 开销）。
- **为什么必要**：blobs 大头是 messages 全量快照，且随 thread 消息累积单调增长——exit 模式只放缓增速（每 run 一跳），**不改变长 thread 快照持续膨胀的趋势**。没有线程回收，空间长期曲线仍向上。
- 被选择性保存器破坏的线程**无法修复**（writes 已被吞）；其 run 均已终结（delivered）→ 可经 `adelete_thread` 清理释放空间。
- 长期：定期回收「已终结且 N 天无活动」线程的 checkpoint（chat_messages 历史独立于 checkpoint，删除不影响会话展示；代价是该 run 不可 resume）。
- 注意：多 run 共 thread 的活跃 session 线程不可删。

## 三、exit 模式的兼容性核验（研究时已完成）

| 依赖方 | 行为 | 结论 |
|---|---|---|
| waiting_user / waiting_external interrupt | exit 模式在 interrupt 时持久化（官方文档 + `_loop.py` 确认） | ✅ resume 可用 |
| run_state_reader | 按 `checkpoint_id` 读 applied Command checkpoint，不依赖「运行中最新」 | ✅ |
| 前端 runtime 状态 | 由 event ledger + agent_runs 驱动（非 checkpoint） | ✅ |
| answer stream / tool_output 事件 | 独立于 checkpoint（agent_run_events） | ✅ |
| checkpoint_side_effects（结算） | 读 run 结束后的 terminal checkpoint | ✅ |
| 图内 `_load`（model/tool 步台账） | 内存 state，不读 DB checkpoint | ✅ |

## 四、实施步骤（评审通过后）

1. 移除 `selective_checkpointer` 的使用（`checkpointer.py` 直接返回 `AsyncPostgresSaver`），`CHECKPOINT_SELECTIVE_ENABLED`/`CHECKPOINT_WATERMARK_STEPS` 配置下线；选择性保存器文件保留待并行会话确认是否删除。
2. `langgraph_driver._execute_inner` 三处 `durability="sync"` → `"exit"`。
3. 回归测试：现有 checkpoint/运行时测试全量；新增「exit 模式下 interrupt checkpoint 全量自足」（真实图 + 内存/测试 saver 验证恢复后 `snapshots`/`messages` 完整）。
4. 测试栈验证：跑一个多工具长任务（>20 模型步），确认 waiting 恢复、terminal 结算、刷新后历史完整；追加 cancel 回归（exit 模式下取消 run 后结算与状态展示正常）。
5. 结算侧检测：reconciler 遇到 `step=-2` 的 stub head 时记录告警（stub 崩溃窗口的兜底观察）。
6. 存量清理（L3）另立项，需用户对「删除终结线程」拍板。

## 五、参考依据

- LangGraph 官方：`docs.langchain.com/oss/python/langgraph/checkpointers.md`（super-step 边界、pending writes、durability modes、StateSnapshot/parent_config 语义）
- 引擎源码（容器 1.2.11）：`pregel/_checkpoint.py`（delta snapshot/ancestor walk）、`pregel/_loop.py`（exit 模式 `_exit_delta_writes`）、`channels/delta.py`（`snapshot_frequency=1000`）
- 官方 API 面：`AsyncPostgresSaver` 仅 `adelete_thread` 线程级删除，无单 checkpoint 删除——wrapper 层不存在「安全跳过」的空间。

## 六、exit 模式的恢复重放语义（2026-08-26 补充，2026-08-30 修订，事故驱动）

**事故 1（provider_call_id 漂移）**：run f7e7e5a0 执行中途（18 个工具已 succeeded，全部只在台账）后端容器被重建 → 中间零 checkpoint → 恢复（attempt 2）时模型重新生成相同内容的消息：`assistant_message_id`/`tool_call_id` 为确定性 uuid5（相同输入→相同 id），而 `provider_call_id`（DeepSeek 每次响应的 `call_00_xxx`）是新随机值 → `_require_exact_request` 将 provider_call_id 差异当「不可变输入冲突」raise `tool_call_idempotency_mismatch` → run 被杀。

**修复（d6df1c11）**：provider_call_id 从幂等身份检查移除（它是 provider 相关性字段，非 runtime 身份——与 1dbad6d9 声明意图「separate Provider correlation from Runtime identity」一致，其实现当时存在偏差），差异降级为 warning；重放交给 `_decision_for_existing`：succeeded→复用结果、started→按 lease/attempt（blocked/reconcile/重试）、failed→prior_failure、unknown→reconciliation。其余身份字段（tool_name/assistant_message_id/arguments_hash/request_ref/contract_version/effect/retry_policy/sanitized_arguments）检查不变。

**事故 2（内容分叉，2026-08-29 run c83675d7）**：Direct Chat run 执行「P0」期间（17:52 创建）后端容器因部署被重建（8eb83643）→ attempt 1 在 android_compile 工具节点内被杀（账本孤儿 started 行）→ attempt 2 从起点全量重放：第 1 步 DeepSeek 缓存命中、输出逐字节一致（幂等命中），第 2 步采样分叉——同一位置派生 call id `6fed2097` 的调用从 `read_file` 再生成为 `list_files` → `_require_exact_request` 对 tool_name/arguments_hash/sanitized_arguments/contract_version 四项不一致 fail-closed → run_failed（checkpoint 1f1a3d2d 终态），用户看到「任务执行未完成」。上文「内容实质差异仍 fail-closed」的边界被实际事故击穿。

**修复（2026-08-30，本次变更）**：重放路径（`reserve_tool_execution` pre-check——行已存在即先前 attempt 的已提交账本，非并发争用）不再 fail-closed：分歧字段以 warning 完整记录审计，落入 `_decision_for_existing` 复用账本终态（succeeded→复用结果、started→lease/attempt 判定、failed→prior_failure、unknown→reconciliation）；真并发路径（IntegrityError 竞态：两个活 worker 同时认领同一 call id）保持 fail-closed 不变。依据：①账本是恢复正确性的权威（durable-execution 重放语义——Event History 是真相，已记录结果被复用而非重算）；②LLM 采样非确定性是常态而非故障，「参数不同可能是真不同请求」在重放场景不成立（位置派生 call id 已锁定请求身份）。配套：`scripts/deploy.sh` 增加在途检查（默认告警，`--require-idle` 中止），消除本事故最高频触发源（部署换容器）。

**剩余边界（设计权衡，非缺陷）**：①真并发竞态下两个活 worker 内容不一致仍 fail-closed（防同一调用双执行）；②重放撞上 attempt 1 的孤儿 `started` 行（被杀进程无法自行结算）时按 lease/attempt 判定：写类工具 blocked+reconciliation，不静默重开，safe-read 有界重试——不会静默重复执行；③若重放时 assistant message id 未复现，call id 全新、工具会真实重跑（at-least-once 重放固有代价），根治靠可续 checkpoint（与 checkpoint 表膨胀治理合并排期）。

**How to apply**：exit 模式下任何中途被打断的 run 都会以「重放」恢复；台账（agent_tool_executions）是恢复正确性的权威，任何「确定性 id 重放」路径都必须幂等复用而非失败。部署前跑 `scripts/deploy.sh`（在途检查默认告警）；长期任务（如 30 分钟编译）勿在部署窗口启动，或部署加 `--require-idle`。
