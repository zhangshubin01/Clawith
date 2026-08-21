# Clawith Agent 运行可观测性集成 — 深度分析

> 日期：2026-08-22
> 状态：Phase 1（LLM 调用 span）已实施；Phase 2/3（节点/工具/多租户隔离）待实施
> 范围：backend（LangGraph Runtime v2）的可观测性缺口、候选工具对比、精确埋点方案、多租户隔离与成本边界

## 0. 结论摘要

**首选 Langfuse（自托管）**，备选 **OpenTelemetry + OpenInference/Phoenix**（要厂商中立标准时），LangSmith 仅在可接受「数据出网到 SaaS」时考虑。

决定性前提：Clawith 的 LLM 调用**不走 langchain-openai**，而是自定义 `httpx.AsyncClient` 直连（`backend/app/services/llm/client.py`）。因此 LangChain 系 `CallbackHandler` 只能捕获 LangGraph 节点级事件、**抓不到原始 LLM 调用**——而这恰恰是 agent 可观测性的核心。正确选型是「框架无关的手动埋点」类工具。

---

## 1. 现状盘点：已有能力 vs 缺口

Clawith 已经具备相当完整的「记录型」可观测，但缺「追踪型（trace-level）」可观测：

| 已有能力 | 位置 | 性质 |
|---|---|---|
| LangGraph checkpoint 持久化（Postgres） | `langgraph-checkpoint-postgres` + `checkpointer.py` | 状态级，可断点续跑 |
| checkpoint metadata（`clawith_run_id`/`clawith_command_id`） | `runtime_command_config()` (checkpointer.py:59) | 把图调用绑定到 Run/Command |
| 每 agent token 记账（含 DeepSeek cache 命中） | `token_tracker.py` `record_token_usage()` → `Agent.tokens_used_*`、`cache_read/creation_tokens_*` | 汇总计量（日/月/累计） |
| 持久化 Tool Ledger | `tool_execution.py`、`agent_tool_executions`；`execute_pending` (tool_step_service.py:1295) | 工具级：call_id、side_effect_classification、retry_policy、result_summary |
| 聚合指标（task/approval/audit 计数） | `agent_metrics_dao.py` | 计数型看板 |
| Redis 运行时事件/租约 | runtime event intake | 事件流/租约 |

**缺口**：没有「一次 Run 的完整旅程」可查询、可视化的 trace——即 **node → LLM 调用 → 工具调用** 之间带 延迟 / token / 入参出参 / 错误归因 的跨步骤关联视图。现有记账是「总量」而非「单次执行路径」，排障时无法回答「这个 run 第 3 步 model 调用为什么慢 / 哪个 tool 调用错了 / 错误发生在哪个节点」。

这正是要接入的外部工具补上的部分。

---

## 2. 决定接入方式的关键架构事实（读代码核实）

1. **LLM 层**：`caller.py` `call_llm()`（:478，含 tool-calling 循环）→ `client.stream()`（:664）→ `client.py` 各 `LLMClient` 子类的原始 httpx 调用。`LLMResponse` 带 `finish_reason` + `usage: dict[str,int]`。
   → LangChain CallbackHandler 只 hook LangChain 自己的模型封装，这里没有，所以 **LLM 调用必须手动埋点**。

2. **图是确定性状态机**，不是 `create_react_agent` 的 LLM-in-loop：`graph.py` 里 7 个节点（control_guard/compact/model/tool/verify/wait/terminal）都由 `route_after_control` 按 `lifecycle.next_route` 路由，`model` 节点才显式调 LLM。节点签名是 LangGraph 1.x 的 `(state, Runtime[RuntimeContext])`。

3. **`RuntimeContext`（state.py:198）已携带全量身份**，且「永不 checkpoint、按调用注入」：
   `tenant_id` / `run_id` / `command_id` / `agent_id` / `session_id` / `parent_run_id` / `root_run_id` / `model_id` / `actor_user_id` / `actor_agent_id` / `goal` / `run_kind` / `source_type` / `graph_name` / `graph_version`。
   → 埋点时身份零成本可得，无需新开 DB 查询。

4. **图调用入口**：`langgraph_driver.py` `execute()` → `graph.compiled.ainvoke(..., config, context=context, durability="sync")`，其中 `config = runtime_command_config(...)`（含 thread_id + metadata）。这是开「根 trace」的唯一权威位置。

5. **节点分发**：`node_executor.py` `execute(node, state, context)` → `model`→`complete_once`、`tool`→`execute_pending`、其余同理。这是节点级 span 的唯一 choke point。

---

## 3. 候选工具深度对比（针对 Clawith 形态）

| 维度 | Langfuse ⭐ | OpenTelemetry + OpenInference/Phoenix | LangSmith |
|---|---|---|---|
| 能否抓 raw httpx LLM 调用 | ✅ SDK 框架无关：`@observe`/`start_as_current_observation`/手动 span 直接包 `call_llm`/`client.stream` | ✅ `opentelemetry-instrumentation-httpx` 自动埋 httpx + 手动 span | ⚠️ 需 `langsmith.@traceable` 手动包（LangChain callback 抓不到） |
| 节点级事件 | LangChain callback 或手动 span 均可 | OTEL 手动 span / LangGraph event | ✅ 原生最好（`config.callbacks` 即接 graph 事件） |
| 开源 / 自托管 | 开源（core），Docker Compose/Helm/Terraform 官方支持 | 开源，Phoenix 自托管 | 主要 SaaS；自托管为企业付费版 |
| 自托管资源 | 重：Postgres + **ClickHouse + S3/MinIO + Redis**（6 容器） | 轻：Postgres + Phoenix server | SaaS 零部署 |
| 多租户隔离 | 原生 project + 每 project 独立 API key；`user_id`/`session_id`/`tag`/metadata | OTEL 属性 + 后端按属性隔离 | `metadata`/`tags` + anonymizer |
| 生态 / 活跃度 | 33.5k★，v4（实时，宣称 165× 提速） | openinference 1.2k★ / phoenix 11.1k★ | 闭源，LangGraph 第一方 |
| 附带能力 | evals、prompt 管理、playground、成本追踪 | eval/会话分析为主（OTEL 标准，无锁定） | 图执行可视化、数据集/eval |

**Clawith 约束下的取舍**：
- 企业多租户平台大概率要求数据留在自己机房 → 排除 LangSmith SaaS。
- 想要「全功能 + 自托管」→ Langfuse，但接受新增 ClickHouse + S3 两个运维面。
- 想要「标准开放 + 最轻运维 + 避免锁定」→ OTEL + Phoenix（Clawith 已有 Postgres，Phoenix 仅再起一个 server）。

---

## 4. 精确埋点设计（4 层 + 身份映射）

统一原则：**在已有 choke point 埋，身份全部取自 `RuntimeContext`；埋点异步/批量上报，绝不阻塞 `durability="sync"` 的图执行路径。**

### 层 0 — 根 trace（Run 级）
位置：`langgraph_driver.execute()` 里 `graph.compiled.ainvoke(...)` 之前开根 span/trace，结束后关闭。

身份映射（Langfuse 语境；OTEL 用等价 attribute）：
```
tenant_id      → Langfuse project（多租户隔离的边界）
run_id         → trace name / trace-level metadata（一次 Run = 一条 trace）
session_id     → session_id（把同一 thread 的多个 run 归组）
agent_id       → tag（按 agent 过滤）
actor_user_id  → user_id（按终端用户过滤）
goal/run_kind/source_type/model_id/graph_name/graph_version → metadata
command_id / parent_run_id / root_run_id → metadata
```

### 层 1 — 节点级 span
位置：`node_executor.execute(node, state, context)`（或 `graph.py` 的 `_execute_node`）。
capture：`node`（span 名）、`lifecycle.next_route`/`status`、`model_step_count`、`verification_attempt_count`、latency、异常（如 `RuntimeGraphContractError`）。
- 说明：这一层也可走 LangChain callback（`config.callbacks`），但手动 span 更省事——`node_executor` 已持有 `context`，且能拿到 `state["lifecycle"]`。

### 层 2 — LLM 调用 span（最核心）
位置：`caller.py` `call_llm()`（覆盖整个 tool-calling 循环）或更细地包 `client.stream()/complete()`（单次 HTTP 生成）。
capture（`as_type="generation"`）：
- `model`、`provider`（`model.provider`/`model.model`）
- input：`messages`（脱敏后，见 §5）
- output：`response.content`（脱敏后）、`finish_reason`
- usage：`response.usage` → `prompt_tokens`/`completion_tokens`/`total_tokens`；DeepSeek 的 `cached_tokens` 命中（对应 `TokenUsage.cache_read_tokens`，与既有 cache 事实记忆一致）
- latency、错误类型（`classify_error`/`FailoverErrorType`）、retry/failover 次数（`_call_prepared_with_retry` 的 attempt、`call_llm_with_failover` 是否触发）

流式场景（`client.stream`）：在 span 内累加 chunk 的 `usage`（`stream_options.include_usage=True` 已开启，final chunk 带 usage），结束时写入；chunk 级正文不回传（正文走既有 `on_chunk`/`on_thinking` 通路）。

### 层 3 — 工具调用 span
位置：`tool_step_service.execute_pending`（:1295）或 `tool_execution.py`。
capture：`tool_name`、args（脱敏）、`result_summary`、`side_effect_classification`、`retry_policy`、latency、错误；并把 span 与 Tool Ledger 的 `call_id` 关联（跨系统可追溯）。

---

## 5. 脱敏与多租户隔离（企业平台必做）

- **脱敏**：`tool_execution.py` 已有 `_SECRET_ASSIGNMENT_RE`/`_URL_RE`/`_DSN_RE`，`channel_delivery.py` 有 `_BEARER_RE`。埋点时对 input/output 与 tool args 复用同一套正则做 mask（或映射到 Langfuse masking / OTEL 自定义 SpanProcessor / LangSmith anonymizer）。
- **多租户**：`tenant_id` 必须映射为**物理隔离边界**（Langfuse 的 project，或 OTEL 后端的租户维度），跨租户 trace 绝不能串；每租户独立上报 key。
- **PII**：`actor_user_id` 等直接暴露为 user_id 前需评估脱敏（哈希化）。

---

## 6. 与现有记账/账本的边界（防重复计费）

`record_token_usage()` 已经把 token 计入 `Agent.tokens_used_*`（含 cache 命中），是**计费/配额**的唯一事实源。可观测工具的 usage 字段只做**诊断展示**，不再回写计费字段；两者可共存，但需在文档里明确「计量以 `token_tracker` 为准，trace 的 usage 仅用于排障/评估」。

同理，Tool Ledger 是「执行结果与副作用」的权威，可观测工具的 tool span 是「过程视图」，以 `call_id` 对齐，不重复落地。

---

## 7. 性能与运维成本 / 风险

- **性能**：埋点必须异步（Langfuse SDK 默认后台 batch flush；OTEL 用 `BatchSpanProcessor`）。禁止在 `durability="sync"` 的图路径上做同步上报或同步脱敏大 payload。
- **运维**（Langfuse 自托管）：新增 ClickHouse + S3/MinIO 是新的故障面与备份面；数据量上来后 ClickHouse 是正确选择，但要有升级/备份策略。Phoenix 则轻得多（仅 Postgres + server）。
- **体积**：trace 会放大存储（尤其多模态/长正文），需设采样（Langfuse sampling）或只存脱敏摘要。
- **待核实**：二手信息称「ClickHouse 已收购 Langfuse、Traceloop 并入 ServiceNow」，未在官方源确认；若属实会影响 Langfuse 长期开源路线，落地前查官方公告。

---

## 8. 分阶段落地路径（建议）

- **Phase 0**：定指标与身份映射；确认数据是否允许出网（决定自托管 vs SaaS）；选 Langfuse 或 OTEL+Phoenix。
- **Phase 1（最高价值、最小侵入）**：只埋 **层 2（LLM 调用 span）**，包 `call_llm`/`client.stream`。立刻能看到每次 model 调用的 provider/model/token/latency/error。
- **Phase 2**：埋 **层 0（根 trace）+ 层 1（节点 span）+ 层 3（工具 span）**，打通 run→node→llm→tool 全链路。
- **Phase 3**：多租户 project 隔离 + 脱敏规则 + 采样 + 告警（慢调用/错误率/成本）+ 接 Langfuse evals（或 Phoenix evals）做回归评估。

---

## 附：核实过的一手来源

- LangGraph 官方 observability 页：https://docs.langchain.com/oss/python/langgraph/observability
- Langfuse self-hosting：https://langfuse.com/self-hosting（架构 = Web+Worker+Postgres+ClickHouse+Redis+S3）
- Langfuse Python SDK instrumentation（`@observe`/context manager/manual，框架无关）：https://langfuse.com/docs/sdk/python/decorators
- OpenInference LangChain instrumentation README：https://github.com/Arize-ai/openinference/tree/main/python/instrumentation/openinference-instrumentation-langchain
- GitHub 星数（2026-08-22）：langfuse 33524 / phoenix 11137 / openinference 1161

## 附：关键代码坐标（本次核实）
- `backend/app/services/llm/client.py` — `LLMClient.complete/stream`（:539/:551）、`OpenAICompatibleClient`（:576）、`LLMResponse`（:456，`finish_reason`+`usage`）
- `backend/app/services/llm/caller.py` — `call_llm`（:478）、`client.stream`（:664）、`call_llm_with_failover`（:874）
- `backend/app/services/agent_runtime/graph.py` — `build_agent_runtime_graph`（:241）、7 节点、`_execute_node`（:129）
- `backend/app/services/agent_runtime/state.py` — `RuntimeContext`（:198，全量身份字段）
- `backend/app/services/agent_runtime/node_executor.py` — `execute`（:1188，节点分发 choke point）
- `backend/app/services/agent_runtime/langgraph_driver.py` — `execute`→`ainvoke`（:410/:434/:468/:490）
- `backend/app/services/agent_runtime/checkpointer.py` — `runtime_command_config`（:59）
- `backend/app/services/agent_runtime/tool_step_service.py` — `execute_pending`（:1295）
- `backend/app/services/token_tracker.py` — `TokenUsage`（:15）、`record_token_usage`（:170）

## 附：Phase 1 实施记录（2026-08-22）

已落地（LLM 调用 span，Langfuse 后端，opt-in）：

- `backend/app/services/observability/tracing.py` — 框架无关 facade：惰性初始化 Langfuse、`observe_generation` 上下文管理器（generation span + output/usage/latency/error）、`mask_text` 脱敏、`set_run_identity`（Phase 2 身份注入就绪）、`flush`。
- 埋点：`single_step.complete_llm_once`（v2 LangGraph runtime）与 `caller.call_llm`（legacy/ACP runtime）两处 LLM 调用。
- 配置：`app/config.py` 新增 `OBSERVABILITY_ENABLED` / `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` / `LANGFUSE_HOST`。
- 依赖：`pyproject.toml` 新增 `langfuse>=4.0.0,<5.0.0`。

启用方式（未设置时是严格 no-op，零行为变化）：
```bash
OBSERVABILITY_ENABLED=true
LANGFUSE_PUBLIC_KEY=pk-lf-...
LANGFUSE_SECRET_KEY=sk-lf-...
LANGFUSE_HOST=https://your-langfuse   # 可选，自托管时填
```

验证：ruff 全绿；`tests/test_observability_tracing.py` 8 项通过；受影响既有测试（`test_llm_single_step`/`test_llm_failover`/`test_token_tracker`）63 项通过；`scripts/arch-guard.sh` P0 检查通过；真实 Langfuse SDK 冒烟测试通过（span 创建 + output/usage 写入 + 脱敏）。
