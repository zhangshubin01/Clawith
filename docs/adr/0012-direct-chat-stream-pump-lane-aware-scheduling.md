# ADR-0012: Direct Chat 消息泵车道感知调度（根治排队 run 流悬挂）

- **状态**: 已接受（2026-08-31）
- **前置**: 2026-08-31 事故（session `e47a3f7e`，run `6b6dd377`/`d94aef3f`，前端「思考中」冻结 8 分钟；根因链与 Langfuse 埋点证据见 `docs/technical-plans/20260831-queued-run-stream-hang-root-fix.md` §1–§2）
- **废止**: 无

## 背景

Direct Chat 的 `/ws/chat` 是单 socket 串行消息泵：`message_loop`（websocket.py）一次只跑一条 `stream_web_chat_run` 流，流未结束时新消息只入 `pending_runs` 队列。事故链路：

1. run `6b6dd377` 停驻 waiting（LangGraph interrupt，合法长驻）后，泵自动开始流式排队 run `d94aef3f`；
2. `d94aef3f` 的 start 命令永远无法被认领——车道只在终态释放（`scheduling_lane.py` `_TERMINAL_STATUSES={completed,failed,cancelled}`，waiting 不算），waiting 的 `6b6dd377` 永久持车道——而 `_worker_alive` 的「command pending」分支把该悬挂流判为存活，120s idle 杀不掉；
3. 用户 10:19:16 回复（resume）被 worker 正常执行（Langfuse 埋点：每分钟 8–43 条 span 连续产出 6 分钟），但它的流排在悬挂流之后，socket 零事件 8 分钟；前端 `isWaiting` 只有 WS 包能清 → 「思考中」冻结；
4. 10:25:53 车道释放 → 排队 run 执行 → 历史事件爆发式重放，画面才恢复。

关键不变量（本 ADR 的地基）：**车道持有者最多一个，且只有它能「执行」或「停驻 waiting」**——waiting 的 run 持有车道是有意设计（共享 `runtime_thread_id` 上的 checkpoint 是持久化游标，放行其他 run 同 thread 执行会污染 resume 点，见「多 run 共 thread 上下文污染」根治）。因此其余 run 此刻必然零事件。

## 决策

消息泵只流「此刻可能产生活动」的 run：起流前做一次**入流裁决**（只读探测），不可能活动的排队 run 在泵外挂起等待重探，不占用泵。

- 本 run `lane_held == True` → 立即流；
- 本 run 已终态（最新生命周期事件 ∈ 终态集）→ 立即流（重放不被占道邻居挂起）；
- 存在其他 `lane_held` run → 挂起；
- 车道空闲且本 run 是 lane 内**最早**未终态 run → 立即流（「最早 start」准入：跨 socket 下 worker 按 FIFO 认领，lane 空 ≠ 本 run 下一个被认领，防多 tab 空等流复挂）；
- 车道空闲但存在更早未终态 run → 挂起。

挂起项进独立 `deferred` 队列：流结束回环是主重探点（循环入口立即重探队首），`receive_json` 包 2s `wait_for` 只作空闲兜底（消息优先），defer 不 append+continue（防忙循环）。defer 落地补发 `runtime_status(event="queued")` 包（泵空闲路径原本无此锚点）。abort 命中挂起项时先探 start 命令状态：claimed/applied → 改起流；pending → 移除 + cancel 命令。流异常且 run 非终态 → 回队重探（上限 2 次）。探测失败 → 3 × 0.5s 退避重试，仍失败才 fail-open。仅 defer 决策打一条 `[WS] stream_gate` 日志。

（2026-08-31 四视角并行审查后修订：新增「最早 start」准入、探测退避重试、流异常回队、abort 竞态处理、defer 补发 queued 包；裁剪队列上限/拒绝包、rejected 移队、挂起超时告警、每流日志。2026-09-01 实施阶段双轴 code-review 后修订：流结束回环主重探、F8 非终态前置、探测终态分支、start 状态探测下沉 service 层。）

## 安全边界（为什么不改执行层）

- **不释放 waiting 的车道**：waiting 是合法长驻态，其 checkpoint 挂在共享 thread 上；放行他 run 执行会覆盖 resume 点。车道语义（终态才释放）不动。
- **不杀悬挂流**：排队 run 终将执行（事故中 10:25:53 已证），command-pending 保活正确；挂起在泵外即可，无流可杀。
- **不引入 checkpoint 读取**：探测只读 `AgentRun.lane_held` + start 命令状态行，延续「polling over stable product events, never checkpoint internals」模块原则。
- 投递串行与执行串行保持同构（车道已把执行串行化），改动面收敛在 `message_loop` + 一个探测方法 + 测试。

## 可靠性

- 探测失败退避重试 3 次后才 fail-open：吸收瞬时抖动（否则会出现「探测失败但后续流内读正常」的悬挂回归窗口）；持久故障时流内轮询立刻抛错走异常分支，不吞 run。
- 挂起项有 queued 包锚点（前端可消化）；流异常回队重探（上限 2 次）把「流被杀」与「投递永久丢失」解耦。
- 已知残余（本期接受）：挂起队列是 socket 内存态，重连后新 socket 不自动补挂 session 内 pending run——实时流丢失、终态靠 REST 兜底。根治并入层 2 演进票。

## 业界对照（决策依据）

| 路线 | 结论 |
|---|---|
| LangGraph 官方语义（interrupts.md） | 采纳为语义地基：waiting 无限期停驻、resume 一等公民、event streaming 驱动 |
| OpenHands software-agent-sdk 事件总线（`PubSub` + WS 订阅 + `resend_mode='since'` 游标续传，WS 不驱动执行） | 层 2 目标形态（投递与执行全解耦、per-run 流多路复用），本次不采纳——车道已把执行串行化，串行泵 + 入流裁决是消除悬挂的最小步 |
| openai-agents-python per-run 流对象（`RunResultStreaming.to_state()` resume） | 佐证 per-run 流模型；不直接迁移 |
| waiting 即释放车道（直觉方案） | 否决——共享 thread checkpoint 污染，见「安全边界」 |

## 长期方向（记档，不在本次范围）

层 2 演进票：`stream_web_chat_run` 升级 per-run 流任务 + 泵 `asyncio.wait(FIRST_COMPLETED)` 多路复用（fan-in），前端在 runtime-state 发现新 run 执行而本 socket 无流时补发 `attach_run`；群聊 `/ws/group` 同型悬挂审计。
