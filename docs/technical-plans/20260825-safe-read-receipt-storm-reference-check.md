# Safe Read Receipt 风暴修复方案 — 参考资料对照审查（reference-check）

日期：2026-08-26
事故：2026-08-25 09:04–09:09 UTC，4 个 heartbeat run 的 `duckduckgo_search`（read+safe）第 9 次尝试时执行者进程死亡，留下 status=started、lease 未续期的孤儿 receipt；后续 command 重试在 300s lease 窗口内以 0.1–4s 间隔反复撞 `safe_read_attempt_active` fence（464 次 ERROR + 约 16 万 token 模型重放），lease 到期后经 reconciliation 自然收敛。

## 修复方案要点（待审查）

1. **defer 等待 lease 到期**（核心）：`safe_read_attempt_active` defer 后，command 的 retry 延迟至少覆盖到 `lease_expires_at`，而非 0.1–4s 立刻重试。
2. **defer 熔断**：defer 不消耗 attempt 是设计（保留 business attempt），需补一层上限。
3. **孤儿 receipt 恢复**：进程死亡后目前只能等 300s lease TTL，考虑缩短检测窗口。
4. **token 再烧**：数据里每次重试疑似重跑 model 节点，需确认并纳入方案。
5. **可观测性**：defer 控制流不应记 ERROR span。

## 决策点对照表

### D1. 退避形态（等 lease 到期 + jitter）

- **参考**：
  - LangGraph 官方 `fault-tolerance.md`（已 fetch）：`RetryPolicy(initial_interval=0.5, backoff_factor=2.0, max_interval=128, max_attempts=3, jitter=True)`，jitter 默认开启。
  - 本地源码 `langgraph/libs/langgraph/langgraph/types.py:418` RetryPolicy NamedTuple（字段与文档一致，jitter 字段注释 "Whether to add random jitter to the interval between retries"）。
  - Temporal 官方 `encyclopedia/retry-policies.md`（已 fetch）：Initial Interval=1s、Backoff Coefficient=2.0、Maximum Interval=100×Initial；`encyclopedia/detecting-activity-failures.md`：Start-To-Close Timeout 负责 worker crash 检测。
- **结论**：方案与两大框架的指数退避同构，但把「max_interval 封顶」替换为「上限 = lease 剩余时间」——语义更精确：重试目标是等 fence 消失，而不是无限退避。**必须补 jitter**（10 个 daemon 同时 defer 到同一 lease_expires_at 会同时醒来，LangGraph 和 Temporal 都默认带 jitter，不能省略）。
- **偏离说明**：LangGraph 的 RetryPolicy 作用在 graph 节点层（同进程内 sleep），我们的 defer 作用在 command claim 层（跨进程）。退避机制相同、挂载点不同，属于适配而非偏离。

### D2. defer 熔断上限（时长 vs 次数，放哪层）

- **参考**：
  - Temporal retry-policies：Maximum Attempts 默认 ∞，官方明确建议「用 Schedule-To-Close Timeout 限制总时长，而非次数」——「In most cases, we recommend using the Workflow Execution Timeout ... to limit the total duration of retries, rather than using this attribute」。
  - gemini-cli 本地源码：`max_turns` + `DEFAULT_MAX_TURNS`（run 级回合上限）；Clawith 已有 `model_turn_limit`（run 级）与 `AGENT_RUNTIME_RECURSION_LIMIT`（graph 级 recursion）。
  - LangGraph 官方 `errors/GRAPH_RECURSION_LIMIT.md`（索引在案）：递归限制防死循环。
- **结论**：defer 上限应按**时长**（defer 窗口总时长上限），不按次数——与 Temporal 建议一致，且与「defer 不消耗 business attempt」的语义兼容。挂载点选 **command 层**：defer 是 command 重试现象（同一 command 反复 claim），不是 execution 重试；execution 层的 attempt 语义必须保持纯净。
- **注意**：上限必须区分「等待 lease 到期的合理时长」（≈剩余 lease，最长 300s）与「无休止 defer」（当前事故）。一个自然的界：**defer 总等待不得超过 lease TTL 本身**，超时后把 command 置为可重试错误（`_release_for_retry` 消耗 attempt）或直接 reject，而不是无限 defer。

### D3. 进程死亡检测（孤儿 receipt 恢复窗口）

- **参考**：Temporal `detecting-activity-failures.md`（已 fetch）：
  - Start-To-Close Timeout：worker crash 后 server 强制重试（对应我们的 lease 300s）；
  - **Activity Heartbeats + Heartbeat Timeout**：官方原话 "For long-running Activities, we recommend using a relatively short Heartbeat Timeout and a frequent Heartbeat. That way if a Worker fails it can be handled in a timely manner." —— heartbeat timeout 独立于 start-to-close，检测窗口远小于任务时长（throttle = heartbeatTimeout × 0.8）。
- **结论**：Clawith 的 lease（300s）+ renewal（每 100s）就是 Temporal 的 start-to-close + heartbeat 对偶。但**我们把两者合并成一个 TTL**：执行者活着时 renewal 续期、死了就只能等完整 300s。Temporal 的做法是 heartbeat timeout（短，如 60s）独立：worker 死 → 60s 后即可检测，而 start-to-close 可以很长。
- **偏离说明**：单 TTL 设计更简单，但把「检测延迟」钉死在 TTL 上（数据实证：3 个孤儿全部 300–302s 才恢复）。**改进选项**（进 grill 拷问）：给 renewal 心跳独立的短超时（heartbeat_timeout << lease TTL），心跳断了就提前触发接管，而不是等 lease 自然到期。

### D4. 重试从 tool 节点恢复（token 再烧）

- **参考**：
  - LangGraph 官方 `persistence.md`（已 fetch）：checkpointers 的用途之一就是 "recover from a failure"；checkpoint 存于 superstep 边界。
  - Clawith 本地 `graph.py:249-291`：节点拓扑 START → control_guard → {model|compact|tool|verify|wait|terminal} → 回 control_guard，每个节点独立 superstep。
- **结论**：按 LangGraph 语义，tool 节点抛异常后 checkpoint 回滚到 tool 节点入口，恢复时重放 pending writes、**不应重跑 model 节点**。但事故数据里 09:05–09:09 单分钟最高 10.5 万 token 的 GENERATION，说明 model 可能被重跑了——这是**实现层偏离框架语义**的嫌疑，需在 grill 阶段确认 checkpoint 实际回滚位置（`command_worker._process_locked` 每次 claim 重新 `graph.invoke` 的恢复路径）。
- **如果确认偏离**：修复应让「撞 fence 的 command」在 tool 节点处等待而非整图重放——这与 D1 的退避是同一处改动的两面。

### D5. 可观测性（defer 不记 ERROR span）

- **参考**：
  - 项目内已有约定：`test_observe_run_retry_control_flow_not_error`（`backend/tests/test_observability_tracing.py:105`）——说明「重试控制流不算错误」是本仓库已确立的观测契约，只是 `safe_read_attempt_active` 这条路径没套用。
  - reference-projects 六.3：Langfuse（langfuse.com/docs）为项目可观测选型；框架层面无「控制流信号级别」的权威标准。
- **结论**：以仓库既有契约为准（把 defer 的 span level 从 ERROR 降级，或复用既有 not-error 处理路径）。**该类（可观测性信号级别）无权威外部参考，显式声明。**

### D6.（新增建议）优雅停机

- **参考**：LangGraph 官方 `fault-tolerance.md` Graceful shutdown（已 fetch）：`RunControl.request_drain()` + `GraphDrained`，SIGTERM 时在 superstep 边界保存 checkpoint 后再停，避免「执行到一半进程死掉」。
- **结论**：事故的直接触发是「执行者进程在第 9 次尝试中死亡」。若死亡源于部署重启，Clawith 应实现 drain 式停机（停前把 in-flight execution settle 或标记 retry_pending）。这是根治「孤儿 receipt 产生」的一层，与 D3 的「更快检测」互补。

## 参考资料覆盖声明

| 类别 | 覆盖情况 |
|---|---|
| 本地源码库（langgraph/langchain/deepagents） | ✅ 已读 langgraph `types.py` RetryPolicy、`pregel/_retry.py` |
| 开源实战项目（OpenHands/codex/gemini-cli/SWE-agent 等） | ⚠️ 部分覆盖：gemini-cli `max_turns` 已查；OpenHands 本地镜像现为 TS 前端仓库（`electron/`、`src/`），python 侧无 `max_iterations`；codex-rs 未检索到对应熔断参数（关键词 max_turns/max_iterations）。熔断语义由 Temporal 官方与 gemini-cli 补足。 |
| 官方文档/方法论（URL，已 fetch 非跳过） | ✅ LangGraph fault-tolerance.md、persistence.md；Temporal detecting-activity-failures.md、retry-policies.md |
| 评估基准（SWE-bench/Terminal-Bench/RE-Bench） | ❌ 无相关参考：本方案是运行时容错机制，不涉及 agent 能力评测，该类显式排除 |

## 交给 grill 阶段的开放问题

1. D1+D4 是否同一处改动：撞 fence 时把等待前移到 tool 节点重试层，能否同时消除 464 次整图重放和 model 重跑？
2. D3 的 heartbeat timeout 独立化：值与 lease TTL 的比例、renewal 失败（DB 抖动）时如何避免误杀长工具（android_compile 300s 案例是反例）。
3. D2 的时长上限放 command 层，与 `_max_attempts`、claim TTL（60s）的交互；claim TTL 短于 defer 等待时会不会导致 defer 失效（defer 等待 300s 而 claim 60s 过期，command 被其他人 claim 的竞态）。
4. defer span 降级后的 Langfuse 判据：用 span level 还是 name 前缀区分，与既有 `test_observe_run_retry_control_flow_not_error` 契约一致。
