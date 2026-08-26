# WS idle kill 缺口深度分析：二次 waiting 幂等去重 → 120s 断流

- 日期：2026-08-26
- 范围：Direct Chat `/ws/chat` 事件流状态机（`DatabaseRuntimeEventStream` + `stream_web_chat_run` + `WebSocketChatHandler`）
- 状态：分析已完成、方案已重评（会话内评估的方案 C+D 此前未成文，本文档为重建版）
- 对应遗留：第二十九次部署（`93b97a9c`）备注「WS idle kill 缺口（二次 waiting 幂等去重→120s 断流）方案 C+D 已评估未实施」

## 1. TL;DR

同 run 内第二次进入 `waiting_user`（合法场景，LangGraph 官方支持同 thread 多次 interrupt）时：

1. 第二次 `waiting_started` 事件**正常写入**（幂等键含 checkpoint_id，不冲突）；
2. 但第二次 waiting 的**投递被幂等去重**：`DeliveryRequest.idempotency_key = run:{run_id}:waiting:{correlation_id}` 与第一次相同 → `deliver_runtime_message` 命中 `_existing_receipt` 提前返回 → **没有第二条 `delivery_succeeded` 事件、没有第二条 ChatMessage（第二次提问不落库）**；
3. `stream_web_chat_run` 把「收到 delivery 事件」当作流终止条件 → 被去重后永远等不到 → 挂起；
4. 120s 无新事件且 `_worker_alive` 为假（command 已 applied → claim 清空、无 tool lease）→ 抛 `runtime_event_stream_idle_timeout` → WS 层回 error 包 → 前端渲染「Warning: No Runtime events received for 120s…」假助手消息——**用户可见断流**，而 run 实际仍挂 waiting（真相等用户取消）。

生产数据实锤：run `c9231cdd`（2026-08-21）两条 `waiting_started`（15:37:26 / 15:37:58，同 correlation `approval:f4708daf-…`）、waiting 投递只有 1 条（15:37:26）、第二次提问无对应 ChatMessage、22 分钟后才被 `run_cancelled`。

## 2. 事故证据（生产 DB 实录）

`agent_run_events`，run `c9231cdd-18ad-415c-bc8f-ac41a638f580`（L3 文件删除审批门）：

| 时间 (UTC) | 事件 | idempotency_key | correlation |
|---|---|---|---|
| 15:37:26.632 | `waiting_started`（第 1 次） | `checkpoint:1f19d763-…:waiting_started` | `approval:f4708daf-…` |
| 15:37:26.718 | `delivery_succeeded` (waiting) | `run:…:waiting:approval:f4708daf-…` | `approval:f4708daf-…` |
| 15:37:58.050 | `resumed`（用户批准，command 54af5226） | `command:54af5226-…:resumed` | — |
| 15:37:58.050 | `waiting_started`（**第 2 次**） | `checkpoint:1f19d764-…:waiting_started` | **`approval:f4708daf-…`（相同）** |
| （缺失） | ~~第二次 `delivery_succeeded`(waiting)~~ | — | 被幂等去重 |
| 15:59:51 | `run_cancelled` + terminal delivery | — | 22 分钟后用户取消 |

`chat_messages` 佐证：该会话中「File deletion requires approval」消息在 15:37:26 有一条（第一次 waiting 投递落库），15:37:58 的第二次提问**没有对应消息**。

用户可见症状链（前端）：收到第二次 `waiting_started` 的 `runtime_status` 包 → `fetchSessionRuntimeState` 能从 REST 看到 pending 审批卡；120s 后流抛错 → 后端回 `error` 包（`runtime_event_stream_idle_timeout`，stage=stream）→ 前端在聊天里插入「Warning: …」假助手消息；run 状态实际是 waiting_user，直到用户手动取消。观感 = 断流。

## 3. 根因链（代码级，逐环核实）

### 3.1 二次 waiting 是合法且真实发生的场景

- 图结构允许同 run 多次进入 wait：`graph.py` wait 节点 `interrupt(waiting_request)` 挂起后，resume 时 `interrupt()` 返回恢复值继续执行，可再次走到 `intent="wait"`（`node_executor.py:717-731`）。
- 事故触发源 = L3 删除审批门 `_delete_autonomy_gate`（`tool_step_service.py:1841-1931`）。correlation 来自 `autonomy_service.check_and_enforce`，其 `_runtime_approval_identity`（`autonomy_service.py:29-49`）：
  `approval_id = uuid5(run_id, f"runtime-approval:{action_type}:{tool_call_id}")` → `correlation_id = f"approval:{approval_id}"`。
  同 run 同 `tool_call_id` 再次进同一审批门（模型在 resume 后重发同一删除调用/同 call_id 重放）→ 返回**相同的 approval_id 与 correlation**。同类确定性 correlation 还有：`tool-confirm:{run_id}`、`tool-reconcile:{run_id}`、`model-provider-retry:{run_id}:{model.id}`（`model_step_service.py:2297/2304/2599`）、飞书审批门（`tool_step_service.py:773-786`）。

### 3.2 `waiting_started` 事件本身未被去重（正确）

`_record_lifecycle_events`（`checkpoint_side_effects.py:622-707`）：
- 事件 id = `uuid5(run_id, f"lifecycle-event:{key}")`，key = `checkpoint:{checkpoint_id}:waiting_started`；
- 不同 checkpoint_id → 不同 id/唯一索引 `uq_agent_run_events_checkpoint_type_non_delivery` 不冲突 → 第二次事件照常落库（证据见第 2 节表格）。

### 3.3 第二次 waiting 投递被幂等去重（缺陷本体）

- `DeliveryRequest.idempotency_key`（`delivery.py:84-92`）：waiting 类 = `run:{run_id}:waiting:{interrupt_id}`，interrupt_id = waiting_request 的 correlation_id。
- `deliver_runtime_message`（`delivery.py:839-841`）先查 `_existing_receipt`，命中即返回旧 receipt，不产生新事件、不创建新 ChatMessage。
- 第二次 waiting 与第一次 correlation 相同 → 键相同 → **投递被吞**。这是「幂等去重」在语义上的过拟合：幂等键只按 (run, correlation) 区分，把「同一次 waiting 的崩溃重放」与「同 correlation 的第二次 waiting」混为一谈。

### 3.4 事件流终止条件与 idle 误杀（断流的最后一环）

- `stream_web_chat_run`（`chat_stream.py:111-389`）：收到 `waiting_started(user)` → `terminal_status = "waiting_user"`（227-234）；但只有收到 `delivery_succeeded/delivery_failed` 才返回 outcome（247-256、288-384）。投递被去重 → 流永不返回。
- `DatabaseRuntimeEventStream.stream_run`（`event_stream.py:232-304`）：`idle_timeout_seconds=120.0`（`chat_stream.py:124-127`）。120s 无新事件时判 `_worker_alive`（`event_stream.py:189-230`）：command 已 applied（`mark_command_applied` 把 `claim_expires_at` 置 None，`persistence.py:852`）→ 非 pending、无 claim；无 started tool lease → 返回 False → 抛 `runtime_event_stream_idle_timeout`。
- `_run_runtime_and_stream`（`websocket.py:1188-1205`）catch 后发 `error` 包（"Runtime execution continues, but its live event stream was interrupted"）；前端（`AgentDetailPage.tsx:3720-3729`）渲染 Warning 假消息，`runtimeErrorDisablesReconnect` 不含此码、socket 不断但流附件已死。

## 4. 方案重建与重评（原会话 C+D 未成文，此处按证据重建全空间）

| 方案 | 改动 | 效果 | 风险/代价 | 结论 |
|---|---|---|---|---|
| **A. 投递幂等键按 waiting 边界区分** | waiting 幂等键加 checkpoint_id：`run:{run_id}:waiting:{correlation_id}:{checkpoint_id}` | 第二次 waiting 产生新投递事件 + 新 ChatMessage（提问落库、前端收到 done 包） | 崩溃重放（同 checkpoint）仍正确去重；resume intake 从 checkpoint 状态读 correlation（`chat_intake.py` `_require_direct_resume_correlation` → `view.waiting_correlation_id`），不依赖投递键 → 无连锁改动 | **推荐（语义根治）** |
| **B. correlation 源头唯一化** | autonomy/飞书门同 call 重进生成新 approval_id | 只治 L3 审批门这一类 | `tool-confirm`/`tool-reconcile`/`model-provider-retry` 等确定性 correlation 仍是同类雷；且审批幂等性（同 call 重复申请返回同一审批）本身是设计意图 | 不单独做 |
| **C. 流在 waiting 边界正常结束** | `stream_web_chat_run` 的终止条件从「必须收到 delivery」改为「到达 waiting 边界即止」：收到 `waiting_started(user)` 后，短窗口内未见对应 delivery（或直接以 waiting_started 为终点）→ 以 waiting_user outcome 正常返回（correlation/question 都在 waiting_started payload 里，`checkpoint_side_effects._event_payload` 已并入） | 任何原因导致的投递缺失都不再挂 120s 误杀；对齐 LangGraph 官方 `stream.interrupted` 语义 | 不解决「第二次提问不落库」（需 A 补）；`done` 包缺 message_id，前端走已有 waiting_user 分支（`applySessionActiveRun` + `fetchSessionRuntimeState`） | **推荐（与 D 一体）** |
| **D. idle 判定识别 waiting 为存活** | `stream_run`/`_worker_alive` 把「run 生命周期为 waiting_*」视为合法静止，不抛 idle 超时 | waiting 期间不再误杀 | **单独用 D 会死锁**：`_run_runtime_and_stream` 的 `while not stream_task.done()` 等流结束，流永不结束 → message_loop 卡死、后续用户消息只入队不处理。D 必须与 C 组合（C 负责退出、D 负责判定依据） | 与 C 组合使用 |

**推荐组合：C+D（断流根治，改 WS 状态机，低风险）+ A（语义完整性，第二次提问落库/可见）。** B 可选，不必要。

- C+D 直接命中用户目标「消灭用户可见断流事故」：流的状态机语义从「事件→投递」改为「waiting 边界即终点」，与 LangGraph 官方 HITL 流式循环同构。
- A 是一次小改动（幂等键 + 相应测试），补齐「第二次提问出现在聊天历史」的完整语义，防止修了断流后出现「第二次提问幽灵可见（仅 pending 卡）但历史缺失」的次级问题。
- 实现顺序建议 A 先于 C+D 落地，或同 commit：A 落地后事故路径不再触发；C+D 作为防御性兜底（任何投递缺失都不再以 120s 误杀收场）。

实现要点（若实施）：
- A：`DeliveryRequest.idempotency_key` 对 waiting 类拼接 `checkpoint_id`（`delivery.py`）；确认 `_existing_receipt`/`_event_id`/`_message_id` 链路仅由 key 驱动即可。
- C：`chat_stream.py` 在 `terminal_status == "waiting_user"` 且已过 delivery 短窗口（如 2-3 个 poll 周期）后以 `ChatRuntimeStreamOutcome(status="waiting_user", correlation_id=..., cursor=latest_cursor)` 返回；`content` 取 waiting_started payload 的 question（此时不落库消息，A 已落地时该路径仅作兜底）。
- D：`stream_run` 在 idle 判定前先查 run 当前生命周期（或由 C 的短窗口逻辑吸收，避免 D 单独存在的死锁面）。

## 5. 参考资料对照

1. **LangGraph 官方 `interrupts.md`**（`docs.langchain.com/oss/python/langgraph/interrupts.md`，2026-08 验证有效）——本方案的权威语义基准：
   - `stream.interrupted` 为 True 时事件流**正常结束**、等待外部输入；graph「waits indefinitely」直到 resume。→ Clawith 把「waiting 期间无事件」当死 run 用 120s 杀，违背官方语义；C 正是把 `DatabaseRuntimeEventStream` 对齐 `stream_events` 的 interrupted 终止语义。
   - 官方 HITL 循环模式：`while True: stream; if not stream.interrupted: break; user_response = get_user_input(interrupt_info); stream_input = Command(resume=user_response)` —— 与 Clawith `message_loop → _run_runtime_and_stream → 等待下一条用户消息` 结构同构；差异仅在 Clawith 用 durable 事件表 + cursor 重放（更强的一致性与断线恢复）。
   - 「Handling multiple interrupts」：同 thread 多次 interrupt 是**一等公民**（interrupt 自带 id、按 id 配对 resume）→「二次 waiting」不是异常路径，传输层必须支持。
2. **reference-projects 清单**（本地 UGit 镜像）：
   - OpenHands / deepagents：本地 grep 无同构「waiting 期间 idle kill」逻辑（OpenHands 无 `awaiting_user_input` 关键字、deepagents 未直接调用 `interrupt()`）→ 说明该缺陷是 Clawith 传输层特有设计，无现成开源抄件，需按官方 stream_events 语义自行对齐。
   - Anthropic《Building Effective Agents》方法论的人机介入原则（高危操作审批）对应本事故的 L3 审批门场景。
3. **Clawith 内部对照**：`1f2cc7ce`（120s idle 双信号修复）只修了「长工具误杀」，未覆盖「waiting 是合法长驻状态」这一类；本缺口是该修复的遗留边界。

## 6. 实施后验证清单（供落地时用）

- 单测：
  - `delivery.py`：同 run 同 correlation 两次 waiting（不同 checkpoint_id）→ 两条投递事件/两条消息；同 checkpoint 重放 → 仍去重。
  - `chat_stream.py`：`waiting_started` 后无 delivery → 流以 waiting_user outcome 返回，不抛 idle；`_run_runtime_and_stream` 不再因 waiting 挂起。
  - `event_stream.py`：run 生命周期 waiting 时 idle 判定不杀。
- 集成/真实数据：对 c9231cdd 同形态场景（同 call_id 二次进 L3 审批门）端到端复现 → 第二次提问落库 + 前端正常显示 + WS 不再出现 `runtime_event_stream_idle_timeout` error 包。
- 契约：`resume` intake（`_require_direct_resume_correlation`）不受幂等键变更影响——回归 `test_agent_runtime_chat_intake` 与 `test_websocket_runtime_chat` 全绿。
- arch-guard（`scripts/arch-guard.sh`）与全量后端测试基线（当前 2934 passed）。

## 7. 定稿与实施记录（2026-08-26 grill 后）

**Grill 结论（Q1-Q4 用户全部确认）**：范围 = A+C（D 并入 C、B 不做）；C 形态 = 1.5s 宽限窗口、兜底不发 done 包；兜底不落库消息（完整性由 A 负责）；A+C 单 commit 单部署。

**已实施**：

- **A（`delivery.py`）**：`DeliveryRequest.idempotency_key` 对 waiting 类追加 `:checkpoint_id`——`run:{run_id}:waiting:{correlation_id}:{checkpoint_id}`（缺 checkpoint_id 显式报错）。同 checkpoint 崩溃重放仍去重；第二次 waiting 边界获得独立键 → 新投递事件 + 新 ChatMessage（第二次提问落库）。
- **共享提取**：`waiting_content(request)`（question/prompt/reason 优先级 + 兜底文案）入 `delivery.py`，`checkpoint_side_effects._waiting_delivery` 与 `chat_stream` 兜底共用（消灭与 `_WAITING_PROMPT` 的重复，`_WAITING_PROMPT` 已删）。
- **C（`chat_stream.py`）**：`stream_web_chat_run` 改为手动迭代 + `_next_stream_event` 辅助（grace 窗口内 `asyncio.wait_for(anext, timeout=remaining)`；窗口耗尽先以 0.05s 排空已缓冲事件再结束，同批 delivery 不被跳过；无等待计时时阻塞取件）；观察到 `waiting_started(user)` 后设 `grace_deadline`（默认 1.5s，`waiting_grace_seconds` 可注入），窗口耗尽或源结束且仍处 waiting 边界 → 以 `waiting_user` outcome 正常返回（content=共享 `waiting_content(payload)`、不发 done 包）；`resumed` 清除 deadline；所有 break 出口经 `_close_iterator_quietly` 关闭迭代器（窄忽略、文档化）。
- **回归测试**：
  - `test_agent_runtime_delivery.py`：键格式断言更新 + `test_waiting_idempotency_key_pins_each_boundary_to_its_checkpoint` + `test_second_waiting_under_same_correlation_delivers_a_distinct_message_and_event`。
  - `test_agent_runtime_chat_stream.py`：`_EventSource(events, hang=True)`（模拟投递被去重后源不再产出）+ 四个用例：挂起源宽限返回（外层 `wait_for` 兜底证明不再挂 120s）/交错事件不重置窗口/源自然结束优雅返回/`resumed` 清除等待边界（结束仍 loud fail）。
- **验证**：ruff check 通过（无格式噪音混入）；arch-guard P0 全过；全量后端 **2974 passed, 0 failed**；关联六模块（event_stream/checkpoint_side_effects/chat_intake/websocket_runtime_chat/delivery/chat_stream）116 passed。

**遗留说明**：兜底路径（C 触发时）第二次提问仅 pending 卡可见、不落库——A 落地后正常路径不受影响，C 是保险丝。
