# WS 附着已处于 waiting 的 Run → 120s 误杀断流 根治方案

- 日期：2026-08-31
- 范围：Direct Chat `/ws/chat` 事件流状态机（`event_stream.py` + `chat_stream.py`）
- 状态：方案已定稿（grill 第 1 轮决定已并入），待实施
- 关联：`20260826-ws-idle-kill-waiting-dedup-deep-analysis.md`（A+C 已落地 85dc7b70）、`1f2cc7ce`（120s idle 双信号修复）、skill `clawith-runtime-triage`「WS 断流」节

## 1. 事故实录（生产 DB + 容器日志，2026-08-31）

run `bd25e829-0fea-43a1-aa61-709fefe3e8cf`（agent b1a73489 Android 工程师06，session b5e970c5）：

| 时间 (UTC) | 事件 |
|---|---|
| 01:44:52 | run 创建（用户发消息）；start 命令 ea879a19 |
| 01:46:13 | `waiting_started`（waiting_type=user，correlation 1e31a4d2，问 GitLab 凭据）+ `delivery_succeeded`(waiting)；start 命令 `applied`、claim_expires_at=NULL |
| 01:56:45 | WS 重连（trace 1d3163d2471c），前端 `attach_run`（cursor 在最后事件之后） |
| **01:58:45** | **`runtime_event_stream_idle_timeout`**：`No Runtime events received for 120s and the Run's command worker shows no liveness signal` → 前端渲染假助手消息「Warning: …」 |
| 03:07:47 | 再次重连（trace d1d91c81f5cd），同一形态 |
| **03:10:17** | **第二次同样误杀**（同 run、同 waiting） |
| 03:21:20 | 用户新消息 → resume 命令 34c4b818 → run 恢复（03:22:57 `resumed`）→ 第二次 waiting 正常投递 |

要点：run 全程健康（waiting 是合法长驻状态，等了 1.6h 后正常恢复），事件流却两次被判「无活性」杀死。

## 2. 根因链（代码级，逐环核实）

1. **waiting 是 Direct Chat run 的正常静止态**：图执行到 `interrupt(waiting_request)` 后挂起等用户回复（LangGraph 官方语义「waits indefinitely until resume」）。挂起期间 start 命令已 `applied`（claim 被清空，`persistence.py` mark_command_applied）、无运行中工具、无租约——**按设计就该没有任何活性信号**。
2. **`_worker_alive` 的活性模型缺 waiting 一类**（`event_stream.py:189-230`）：只有三种存活信号——command claim 未过期 / command pending / started 工具租约未过期。三者皆无 → 判死。waiting 状态的 run 三者皆无 → 必然误判。
3. **C 修复（85dc7b70）只覆盖「流在活着时观察到 waiting 边界」**（`chat_stream.py:300-309`）：grace 窗口只在流**看到** `waiting_started` 事件时武装。前端重连附着时 cursor 已在最后事件（delivery_succeeded）**之后**，流永远看不到这条历史事件 → grace 永不触发 → 120s 后走 idle kill。
4. **前端每次重连都附着**（`AgentDetailPage.tsx:3339-3346`）：`fetchSessionRuntimeState` 对 waiting run 返回 `canCancel=true` → 无条件 `attach_run`（带本地 cursor）→ 每次页面刷新/断线重连/机器睡眠唤醒，都会在 120s 后复现一次误杀 Warning（本次连复 2 次）。
5. 终端表现：`websocket.py:1189-1205` catch 后发 `error` 包 → 前端插入假 Warning 消息；socket 本身不断，但流附件已死，用户在 waiting 期间失去实时状态推送。

补充：`agent_tool_executions` 该 run 全部行 status∈{succeeded,failed} 且 lease_expires_at=NULL（工具结束后租约即清）——印证第 1 点的「无活性信号是设计使然」。

## 3. 根治方案

语义对齐 LangGraph 官方：**waiting 是合法长驻状态，不是失联**。两处改动，均落在 Direct Chat 事件流状态机内（`DatabaseRuntimeEventStream` 全仓唯一消费方 = `stream_web_chat_run`，爆炸半径确定）。

### 3.1 改动 1（主修复）：附着探测 —— 已处 waiting 的 run，附着立即以 waiting_user 边界正常结束

`event_stream.py`：`DatabaseRuntimeEventStream` 新增只读探测方法：

```python
async def current_waiting_boundary(self, handle) -> RuntimeEvent | None
```

- 查该 run **最新一条非 delivery 事件**（`event_type NOT IN ('delivery_succeeded','delivery_failed')`，按 created_at/id 倒序 limit 1）——delivery 事件永远比 waiting_started 晚 20ms 级，必须跳过。
- 返回条件：`waiting_started` 且 `payload.waiting_type ∈ {"user", "waiting_user"}`（与 `runtime-state` 权威口径一致；C 活路径维持 `== "user"` 不动，DB 现存聊天等待均为 user，此处取并集是防御性对齐），**且该 run 不存在任何 resume 命令行**（`NOT EXISTS command_type='resume'`）。严格守卫的意义：resume 行存在 ⇒ run 已恢复过（可能正处恢复执行中、也可能二次 waiting），此时绝不能短路（`resumed` 事件在下一 checkpoint 边界才落库，实测延迟可达 97s，事件序不可依赖）；无 resume 行 ⇒ run 必在**首次 waiting**，铁定挂起。
- 命中返回 `_runtime_event(row)`，否则 None。

`chat_stream.py`：`stream_web_chat_run` 开头（**仅 `after is not None`** 时——无 cursor 的完整重放路径维持现状，由 C 的 grace 处理）：

```python
probe = getattr(source, "current_waiting_boundary", None)  # 测试替身无此方法则跳过
boundary = await probe(handle) if probe else None
if boundary is not None and _boundary_at_or_before(after, boundary):
    return ChatRuntimeStreamOutcome(status="waiting_user",
        content=waiting_content(boundary.payload),
        cursor=_cursor(boundary),
        correlation_id=<payload.correlation_id，缺失则按活路径同款报 runtime_wait_correlation_missing>)
```

- 位置比较 `(boundary.created_at, boundary.event_id) <= (after.created_at, after.event_id)`：边界在 cursor **之后**（客户端还没看过）→ 不短路，走既有重放（会重放 waiting_started+delivery → done 包 → 前端恢复漏掉的消息）。
- 短路时不发任何包——前端 REST 已知道 waiting 状态，与 C 兜底路径「不发 done 包」行为一致。

### 3.2 改动 2（兜底）：`_worker_alive` 承认 waiting 为存活

`event_stream.py` `_worker_alive` 在 claim/pending/租约检查之后追加：最新非 delivery 事件为 `waiting_started`（**任意** waiting_type）→ 返回 True（重置 idle 时钟，不杀）。

- 与 20260826 文档方案 D 的区别：该文档当时否决 D 是「单独用 D 会死锁」——其前提是 C 尚未落地（user-wait 流无 grace 出口）+ 未做附着探测。现在：user-wait 活路径 1.5s grace 出口、user-wait 首次附着由改动 1 立即出口；**改动 2 实际只对「恢复历史中的二次等待附着」和「非 user 类等待（approval/external）活路径」生效**，而这两类等待都由外部/DB 驱动的 resume 命令唤醒（worker 轮询 DB 命令，不依赖 WS 处理器队列）→ 事件到达后流自然继续，无死锁。
- 效果：消灭该类残余误杀（二次等待重连、L3 审批等待期重连/驻留）。
- **可观测护栏（评审决定 Q2）**：waiting 信号触发的每次保活（约每 120s 一次、仅在有客户端附着且 run 挂起时发生）打一条 `logger.warning`，含 run_id 与 waiting_type。理由：worker 死于等待期时不再有 120s 报警，此日志是唯一取证点——正常时低频无害，异常时（连续保活日志 + 永不恢复）即为 worker 已死的现场证据。不设保活上限计时器（复杂度不值，ponytail 原则）。
- 已知残余风险（写入代码注释）：worker 在 waiting 期间死亡且无人 resume 时，流会静默挂住而非 120s 报错；该场景下 run 本身已卡死（等待无到期机制），由上述 warning 日志兜底取证，且部署杀/重启走重放重建 waiting_started、客户端重连由改动 1 收敛，可接受。

### 3.3 改动 3（取证辅助）：附着日志记录 cursor

`websocket.py` `_attach_runtime_run` 在解析 `after` 后补一条 `logger.info`，含 run_id 与 cursor 原始值（无则记 `None`）。动机：本次根因链里「cursor ≥ 边界」是从行为反推的唯一推断点（cursor 不落日志），下次同类事故可直接观测。

### 3.4 明确不做

- 不动 `delivery.py` 幂等（A 已落地，二次 waiting 投递正常，本次实录第二次 waiting 投递成功）。
- 不动前端（无 cursor 重放、waiting_user outcome、error 包处理均已具备；改动后误杀包不再产生）。
- 不引入 checkpoint 读取（`event_stream.py` 模块原则是「polling over stable product events, never checkpoint internals」；事件表信号已足够精确——78 个现挂起 run 的最新非 delivery 事件 100% 是 waiting_started，仅 2 个 2026-08-10 老 run 为 status_changed 且属终态缺失历史遗留，不影响判定）。

## 4. 测试清单

`test_agent_runtime_event_stream.py`（`_DispatchSession` 按列数区分新查询）：
- `current_waiting_boundary`：waiting_started(user)+无 resume → 返回事件；waiting_started(waiting_user)+无 resume → 返回事件；waiting_type=external → None；存在 resume 行 → None；最新非 delivery 为 status_changed → None。
- `_worker_alive`：最新事件 waiting_started（user/external 各一）→ 流不抛 idle 且发出保活 warning 日志（caplog 断言含 run_id）；命令 applied + 无租约 + 无 waiting → 仍抛（现有 `test_idle_stream_raises_when_no_liveness_signal_remains` 保持）。

`test_agent_runtime_chat_stream.py`（fake source 增加 probe 方法）：
- 边界在 cursor 之后 → 不短路，重放路径不变。
- 边界在 cursor 处/之前 → 立即 waiting_user outcome、零包、不挂（`wait_for` 兜底证明）。
- probe 缺失（现有 `_EventSource`）→ 行为不变。
- boundary correlation 缺失 → `runtime_wait_correlation_missing`。

回归：全量后端基线（当前 3267 绿）+ `scripts/arch-guard.sh` + 关联模块 `test_agent_runtime_event_stream/chat_stream/chat_intake/websocket_runtime_chat/delivery` + 附着日志改动处 `test_websocket_runtime_chat`。

## 5. 部署与验证

- 单 commit 单部署（测试环境红线：不灰度，一步全量）。
- 部署后复现验证：对 waiting run 用「cursor=最后事件之后」重连 → 断言 ① 无 error 包、无 Warning 假消息；② 流立即结束；③ 用户回复后 resume 正常流式。
- 观察项：后端日志 `Runtime event stream failed` 计数在 waiting 场景归零；保活 warning 日志仅在有附着且挂起时低频出现。

## 6. 评审决定记录（grill 第 1 轮，2026-08-31）

用户逐题拍板（结论已并入上文）：

- **Q1 守卫语义**：采纳现方案——「无 resume 行」严格守卫 + 二次等待附着靠改动 2 保活驻留（驻留成本=现状，语义最简单，不引入 checkpoint 读取）。
- **Q2 残余风险护栏**：加保活 warning 日志（改动 2 内），不设保活上限计时器。
- **Q3 口径**：探测取 `{"user", "waiting_user"}` 与 runtime-state 对齐；C 活路径维持 `== "user"` 不动。
- **Q4 方向**：确认后端根治（前端不动）。
- **Q5 落地**：直接 implement（red-green + code-review 两轴审查后提交），不走 to-spec/to-tickets。
