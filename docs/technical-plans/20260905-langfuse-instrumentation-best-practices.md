# Langfuse 埋点最佳实践调研报告

> 调研日期：2026-09-05
> 调研对象：Langfuse 官方文档（langfuse.com，全部结论可追溯到 primary source URL）
> 背景基线：Clawith（LangGraph + FastAPI 多租户 agent 平台，自托管 Langfuse v4.30.0，Python SDK 4.15.1，手写 OTel-backed 框架无关 facade）

## 摘要

Langfuse Python SDK v4 本身就是「OpenTelemetry 原生」的一层薄封装，因此 Clawith 手写 OTel-backed facade 的方向是正确的，**不应**为了官方集成而切换到 LangChain `CallbackHandler`（它只能追踪走 langchain 抽象的 LLM/tool 调用，而 Clawith 的 LLM 调用走自定义 httpx，用它会丢失 generation 层——成本追踪的核心）。真正值得落地的差距集中在四点：① 用 `as_type="agent"` 而非 `span` 标记 run 根节点以启用 agent graph 视图；② 用 `propagate_attributes()` 统一传播 user/session/trace_name/metadata/environment；③ 确认多租户用的是「每租户独立 client」而非实验性的单 client 多 project 路由；④ 第一方 scores 补 score config + idempotency key。详见第八节差距清单。

---

## 1. SDK vs OTel 选型（含 LangGraph 官方集成结论）

### 1.1 Langfuse Python SDK 本身就是 OTel 原生

Langfuse Python SDK v4 是官方 OpenTelemetry client 之上的一层薄封装，自动把 OTel span 转成 Langfuse observation（span/generation/event 等），并加上 token/cost、prompt 链接、评分等 LLM 专属 helper。官方明确建议：**用 Python/JS/TS 就用 Langfuse SDK，不要直接基于 OTel API 裸写**。

> 来源：https://langfuse.com/integrations/native/opentelemetry.md（"OTEL-native Langfuse SDK v4" 一节，及 "Using Python or JavaScript/TypeScript? Use the Langfuse SDK instead of building directly on the OTEL API"）

### 1.2 何时用原生 OTel

原生 OTel 只在以下场景才值得考虑：非 Python/JS 语言、已有 OTel Collector 需要转发、或已有一套 OTel 管线要复用。Langfuse 支持 `/api/public/otel`（OTLP over HTTP/JSON + protobuf，**不支持 gRPC**），并兼容 GenAI semantic conventions。

> 来源：https://langfuse.com/integrations/native/opentelemetry.md（"OpenTelemetry endpoint"、"Custom via OpenTelemetry SDKs" 一节）

### 1.3 两者能否混用：可以，SDK 活在共享 OTel context 里

SDK 运行在共享的 OpenTelemetry context 中，因此**其他 OTel 库产生的 span 也能被导出到 Langfuse**。但 v4 引入了「智能默认 span 过滤」——默认只导出 LLM 相关 span（`langfuse-sdk` scope、含 `gen_ai.*` 属性的 span、已知 LLM instrumentor）。若要混用第三方 OTel span，需要用 `should_export_span` 组合白名单。

> 来源：https://langfuse.com/docs/observability/sdk/advanced-features.md（"Filtering by Instrumentation Scope"）
> 来源：https://langfuse.com/docs/observability/sdk/upgrade-path/python-v3-to-v4.md（"Smart default span filtering replaces export-all behavior"）

### 1.4 LangGraph 官方集成结论：官方方案 = LangChain CallbackHandler，无独立 LangGraph 埋点

Langfuse 对 LangGraph 的「官方集成」就是 **LangChain 的 `CallbackHandler`**，通过 `config={"callbacks": [langfuse_handler]}` 传入 `graph.stream()/invoke()` 实现自动追踪。官方 cookbook 明确说这是"minimal setup and may create generic names such as LangGraph"，生产需要额外加 trace name / tags / metadata / user/session。

> 来源：https://langfuse.com/integrations/frameworks/langgraph.md（"Basic trace with Langfuse callback"、"Example 2: Named and filterable chatbot trace"）

**关键判断（对 Clawith）**：`CallbackHandler` 追踪的是走 **LangChain 抽象**（如 `langchain_openai.ChatOpenAI`）的 LLM 调用与 graph 节点事件。Clawith 的 LLM 调用走**自定义 httpx AsyncClient**，不经过 LangChain 的 LLM 抽象，因此 `CallbackHandler` **无法捕获 generation 层**（prompt/output/usage/cost），只能捕获图结构。而 generation 层恰恰是成本追踪与 LLM-as-a-judge 的锚点。**结论：Clawith 手写 OTel-backed facade 是正确的，不值得替换为 CallbackHandler。** 但可以借鉴官方 cookbook 的「多 agent 嵌套到一条 trace」与「scored trace」模式。

> 来源（多 agent 嵌套 / 评分模式）：https://langfuse.com/integrations/frameworks/langgraph.md（"nested agents in one trace"、"scored trace"）

### 1.5 观测类型有 `agent` 类型，专为 agent 编排设计

Langfuse 有 `agent` observation type（"decides on the application flow and can for example use tools with the guidance of a LLM"），并有独立的 agent graph 视图。框架集成会自动设置；手动埋点用 `as_type="agent"`。

> 来源：https://langfuse.com/docs/observability/features/observation-types.md（"Available Types" 及 `as_type="agent"` 示例）

---

## 2. span / generation / tool / agent 埋点规范

### 2.1 三种创建观测的方式（可互通）

| 方式 | API | 适用场景 |
| --- | --- | --- |
| Context manager（**首选**） | `start_as_current_observation()` | 需要 OTel context 正确传播、自动 end、自动 parent |
| Observe 装饰器 | `@observe()` | 包装函数，自动捕获入参/出参/耗时/错误 |
| 手动观测 | `start_observation()` | 并行/后台任务、生命周期非连续、需提前拿引用 |

三种方式可任意嵌套互用；嵌套由 OTel context 自动处理。

> 来源：https://langfuse.com/docs/observability/sdk/instrumentation.md（"Context manager"、"Observe wrapper"、"Manual observations"）

要点：`start_as_current_observation` 是"确保 active OpenTelemetry context 更新的 primary way"，子观测在 `with` 块内自动继承 parent。`start_observation`（无 as_current）**不改变 active context**，且必须手动 `.end()`，否则观测丢失。

> 来源：https://langfuse.com/docs/observability/sdk/instrumentation.md（"Manual observations" 的 "Key Characteristics"）

### 2.2 观测类型规范

`as_type` 参数：`span` / `generation` / `agent` / `tool` / `chain` / `retriever` / `embedding` / `guardrail` / `evaluator` / `event`。

- LLM 调用 → `generation`（承载 model/token/cost，是关键）
- 工具调用 → `tool`（可被 LLM-as-a-judge 按 tool 过滤）
- agent 编排根 → `agent`（启用 agent graph）

> 来源：https://langfuse.com/docs/observability/best-practices.md（"Are the right steps showing up?"）
> 来源：https://langfuse.com/docs/observability/features/observation-types.md

### 2.3 命名规范（低基数、动词开头、不含模型名/动态值）

- 用**动词开头**的主动语态：`classify-intent`、`retrieve-context`、`generate-response`。
- **不要把动态值放进 name**：用 `process-order` 而非 `process-order-8945`（run 特定值放 metadata）。
- **不要用模型名命名**：`gpt-4o`/`claude-sonnet` 会随换模型破坏所有 evaluator/dashboard 引用；模型是 generation 的独立属性。
- name 是 API：改名会让 evaluator/dashboard/过滤静默失效，要稳定。

> 来源：https://langfuse.com/docs/observability/best-practices.md（"Choose good names"）

### 2.4 generation 不要聚合循环

agent 循环里每次模型调用都要一条独立 `generation`，与它请求的 `tool` 交错排布；**不要**把整个循环包进一条只记最终输出的父 generation——否则看不到每步决策、也定位不了哪次 tool 调用撑爆了 context。

> 来源：https://langfuse.com/docs/observability/best-practices.md（"Beware of aggregating LLM calls"）

### 2.5 输入输出格式

- generation 的 input/output 建议用标准 OpenAI 消息格式（每个 `role`+`content`），tool 调用放在 assistant message 的 `tool_calls` 数组、`arguments` 为 JSON 字符串，UI 才会渲染成 tool call 卡片。
- **root observation 的 input/output 最重要**：trace 级 input/output 默认从 root 派生，被 evaluator 读、被 dataset 实验比对。应设为 reviewer 一眼能懂的内容，而非原始 JSON blob（raw 放 metadata）。

> 来源：https://langfuse.com/docs/observability/best-practices.md（"Choose meaningful input and output"）

---

## 3. session / user / trace 分组与 parent 挂载

### 3.1 v4 用 `propagate_attributes()` 统一传播（取代 update_current_trace）

v4 引入 observations-first 数据模型，`user_id`/`session_id`/`metadata`/`tags`/`trace_name`/`environment` 必须传播到**每条观测**（而非只记在 trace 上）。用 `propagate_attributes()` context manager 自动作用于当前及子观测。

```python
with propagate_attributes(
    trace_name="...", user_id="...", session_id="...",
    metadata={...}, tags=[...], version="...", environment="staging",
):
    ...
```

> 来源：https://langfuse.com/docs/observability/sdk/instrumentation.md（"Add attributes to observations"）
> 来源：https://langfuse.com/docs/observability/sdk/upgrade-path/python-v3-to-v4.md（"update_current_trace() decomposed into 3 methods"）

### 3.2 trace 命名、trace IO 行为

- trace 名用 `propagate_attributes(trace_name=...)`。
- trace input/output 默认 mirror root observation；`set_trace_io()` / `set_current_trace_io()` 已 **deprecated**，仅为兼容 trace 级 LLM-as-a-judge 保留，新代码应直接在 root observation 上设 input/output。

> 来源：https://langfuse.com/docs/observability/sdk/instrumentation.md（"Update trace"）

### 3.3 session 与 user 分组

- `session_id`：把多 trace 归组（多轮对话、多 agent 协作产出一份结果、带 human-in-the-loop 的多请求）。单请求单响应、无连续性的应用不需要 session。
- `user_id`：解锁 per-user 成本/质量/用量视图。

> 来源：https://langfuse.com/docs/observability/best-practices.md（"Track users"、"Group related traces with session IDs"）
> 来源：https://langfuse.com/docs/observability/features/sessions.md
> 来源：https://langfuse.com/docs/observability/features/users.md

**一个 trace 的范围**：一次聊天 turn / 一次 agent run / 一次 pipeline 执行 = 一条 trace；多轮对话 = 每 turn 一条 trace + 一个 session。

> 来源：https://langfuse.com/docs/observability/best-practices.md（"What's the scope of one trace?"）

### 3.4 parent 挂载与跨服务传播

- 嵌套靠 OTel context 自动处理；`start_observation()` 不 shift context，若需在其下建子观测要调用该对象上的 `_as_current_` 变体。
- 跨服务/分布式：用 `propagate_attributes(..., as_baggage=True)` 通过 HTTP header 传播（**安全警告**：会写进所有出站 header，勿放敏感值）；或用确定性 trace_id `create_trace_id(seed=...)`、`trace_context={...}` 关联外部系统。

> 来源：https://langfuse.com/docs/observability/sdk/instrumentation.md（"Cross-service propagation"、"Trace and observation IDs"）
> 来源：https://langfuse.com/docs/observability/features/trace-ids-and-distributed-tracing.md

---

## 4. 性能与成本

### 4.1 异步 / 批处理 / flush

- SDK 后台缓冲 span，批量上报，由 `flush_at`（事件数）+ `flush_interval`（秒）决定。
- **短生命周期进程（script/serverless/worker）必须 `flush()` 或 `shutdown()`**，否则丢事件；`flush()` 阻塞等待队列处理完、网络错误会重试不抛异常。
- 长驻 daemon 收到 shutdown 信号时调用 `shutdown()`（atexit 已自动注册，但 serverless 等场景要手动）。

> 来源：https://langfuse.com/docs/observability/sdk/instrumentation.md（"Client lifecycle & flushing"）
> 来源：https://langfuse.com/docs/observability/features/queuing-batching.md

### 4.2 采样 sampling

- `sample_rate`（构造参数）或 `LANGFUSE_SAMPLE_RATE` env，0~1，默认 1。
- **trace 级采样**：trace 被采样则其全部 observation 与 score 都被采；未采样的 trace 不发任何数据。适合高流量降本降噪。

> 来源：https://langfuse.com/docs/observability/features/sampling.md
> 来源：https://langfuse.com/docs/observability/sdk/advanced-features.md（"Sampling"）

### 4.3 输入输出裁剪与 masking

- 装饰器采集大输入输出有开销：`capture_input=False`/`capture_output=False`，或 `LANGFUSE_OBSERVE_DECORATOR_IO_CAPTURE_ENABLED` env 全局禁用。
- 敏感信息 masking：Python SDK **首选 `mask_otel_spans`**（在 export 阶段对原始 OTel span attribute 生效，含第三方 instrumentation 产生的 span）；legacy `mask` 仅作用于 SDK API 创建的数据。mask 函数要**快且确定性**（否则阻塞 export 队列）。

> 来源：https://langfuse.com/docs/observability/sdk/instrumentation.md（"Observe wrapper"）
> 来源：https://langfuse.com/docs/observability/features/masking.md
> 来源：https://langfuse.com/docs/observability/sdk/advanced-features.md（"Mask sensitive data"）

### 4.4 TTL 数据保留

- 项目级配置，最小 3 天；**未配置时自托管默认永久保留**。
- 自托管要开 Data Retention **需要 Enterprise Edition**（Hobby/Core 不可用）。删除不可恢复，超出保留期可配合 Blob Storage Export 导出。
- 注意：数据集引用被删 trace 后 run item 指向不存在的 trace。

> 来源：https://langfuse.com/docs/administration/data-retention.md

### 4.5 Token & Cost Tracking

- cost 记录在每个 `generation`/`embedding` 上，分为 usage（用量）+ cost（美元），各自按 usage type 分桶。
- **ingested 优先于 inferred**；ingested 缺 usage 时可用 tokenizer 推断、缺 cost 时按 model 定义价格×usage 推断。
- **usage_details 契约：各 key 是互斥分桶**，`input` 不含 `input_*`（如 `input_cached_tokens`）、`output` 不含 `output_*`（如 `output_reasoning_tokens`），`total` 是求和非独立桶。**分桶重叠会双计**，cost 被高估。OpenAI 的 inclusive 计数必须转成 exclusive 桶再存。
- **reasoning 模型（o1 等）无法推断 cost**，必须 ingested usage。推理 token 按 output token 计费，Langfuse 看不到 reasoning token 就无法算成本。

> 来源：https://langfuse.com/docs/observability/features/token-and-cost-tracking.md（"Usage types are mutually exclusive buckets"、"cost inference for reasoning models"）

- 自定义模型定义：UI `Project Settings > Models` 或 Models API，用 `match_pattern` 正则匹配 `model` 值；支持 pricing tiers（按 usage/model parameter/metadata 条件匹配不同单价）。

> 来源：https://langfuse.com/docs/observability/features/token-and-cost-tracking.md（"Adding custom model definitions"、"Pricing Tiers"）

---

## 5. 评分体系

### 5.1 四种 score 类型与选择

| 类型 | 值 | 用场 |
| --- | --- | --- |
| `NUMERIC` | float（如 0.9） | 连续性判断（accuracy/relevance/similarity） |
| `CATEGORICAL` | 预定义类别字符串 | 离散分类（correct / partially_correct） |
| `BOOLEAN` | `0` 或 `1` | 通过/失败检查（幻觉检测、格式校验） |
| `TEXT` | 1–500 字符自由文本 | 开放式标注/reviewer notes |

TEXT score 无法聚合，不参与 experiments/LLM-as-a-judge/score analytics。

> 来源：https://langfuse.com/docs/evaluation/scores/overview.md（"Score Types"）

### 5.2 三种评价方法的分工

| 方法 | 是什么 | 用场 |
| --- | --- | --- |
| 第一方 scores（API/SDK） | 应用/CI 代码自己算并 `create_score()` | 用户反馈、guardrail、自定义评估管线 |
| Code evaluators | Langfuse 托管跑确定性 Python/TS 逻辑 | 精确匹配、JSON 校验、业务规则 |
| LLM-as-a-Judge | LLM 按 rubric 打分 | 主观评估规模化（tone/accuracy/helpfulness） |

> 来源：https://langfuse.com/docs/evaluation/core-concepts.md（"Evaluation Methods"）

### 5.3 LLM-as-a-Judge 目标：observation 级（trace 级已 deprecated）

- **observation 级 evaluator 是生产推荐目标**；trace 级已 deprecated。
- observation 级 evaluator 只加载匹配那条观测的 input/output/metadata，**不加载兄弟/子观测**；需要整体语义时用「logical root observation」。
- 生产模式：开发期用 Experiments，生产用 observation 级 evaluator 做规模化精确监控。

> 来源：https://langfuse.com/docs/evaluation/evaluation-methods/llm-as-a-judge.md（"Understanding Each Evaluation Target"）

### 5.4 评分最佳实践要点

- **score config 强制**：`configId` 引用 `ScoreConfig`，校验 name/type/数值范围/类别枚举。
- **幂等键**：score 由 `id`+`name`+`timestamp(toDate)` 三字段定位，三者都相同才覆盖；用 `score_id`（Python）/`id`（JS）作幂等键。
- **不要发部分更新**：partial merge 已 deprecated，总是发完整 score。
- **scores vs tags**：trace 时就知道的维度（feature/API endpoint）用 tag；事后要评估/分类的用 score。

> 来源：https://langfuse.com/docs/evaluation/evaluation-methods/scores-via-sdk.md（"Preventing Duplicate Scores"、"Enforcing a Score Config"）
> 来源：https://langfuse.com/docs/evaluation/scores/overview.md（"Should I Use Scores or Tags?"）

---

## 6. 多租户隔离

### 6.1 标准模型：每租户 = 独立 project + 独立 API key

RBAC 层级：User → Organization → Project → API Key。**API key 绑定 project**（与 user 解耦），用于程序化访问该 project 数据。这是多租户隔离的标准做法。

> 来源：https://langfuse.com/docs/administration/rbac.md

### 6.2 Python SDK 单 client 多 project 路由是「实验性」且有坑

官方 Python SDK 支持单应用内多 public key 路由到不同 project，但**标记为 experimental**，关键限制：

- 第三方库（HTTP client/DB）自动产生的 OTel span **没有** Langfuse public key 属性，无法路由，会被**所有 span processor 处理、可能发到所有 project**（默认过滤下主要影响 GenAI/LLM span）。
- 要求所有 SDK 执行都传 `public_key`，漏传会导致 trace 落到默认 project 或丢失。

> 来源：https://langfuse.com/docs/observability/sdk/advanced-features.md（"Multi-project setups"）

### 6.3 推荐做法

- 每租户实例化**独立 `Langfuse` client**（各自 public/secret key），而非依赖实验性单 client 多 project 路由——尤其是 Clawith 这种 LLM 调用走自定义 httpx（非 langchain/OpenAI wrapper）的场景，第三方 span 泄漏风险更需规避。
- 跨服务传播 user/session 等属性时勿用 baggage 放敏感值。

> 来源：https://langfuse.com/docs/observability/sdk/advanced-features.md（"Initialization" 多 client 示例）
> 来源：https://langfuse.com/docs/observability/sdk/instrumentation.md（"Cross-service propagation" 安全警告）

---

## 7. 生产环境坑清单

1. **async contextvar 传播丢失**：跨 `await` 边界丢 context → 观测挂错 parent 或丢失。优先用 context manager（`start_as_current_observation`），依赖 Langfuse helper；多线程用 `ThreadingInstrumentor().instrument()`。
   > https://langfuse.com/docs/observability/sdk/troubleshooting-and-faq.md（"Incorrect nesting or missing spans"）；https://langfuse.com/docs/observability/sdk/advanced-features.md（"Thread pools and multiprocessing"）

2. **flush 时机**：短生命周期进程不 `flush()`/`shutdown()` 丢事件。
   > https://langfuse.com/docs/observability/features/queuing-batching.md

3. **v4 默认 span 过滤导致「孤儿观测」**：过滤掉父 span 但保留子 span → 子观测挂到 trace root。排查：开 `LANGFUSE_DEBUG`，用 `should_export_span` 白名单恢复。
   > https://langfuse.com/docs/observability/sdk/advanced-features.md（"Filtering spans may break parent-child relationships"）

4. **引用未到达的 parent**：观测引用了 Langfuse 从未收到的 parent，会显示在 trace root 而非预期 parent 下。
   > https://langfuse.com/docs/observability/sdk/troubleshooting-and-faq.md

5. **metadata/user_id/session_id 长度限制**：v4 起 `metadata` 是 `dict[str,str]`、值 ≤200 字符（非字符串强转、超限丢弃）；`user_id`/`session_id` ≤200 字符。
   > https://langfuse.com/docs/observability/sdk/upgrade-path/python-v3-to-v4.md（"Validation changes"）

6. **`release` 属性变化**：v4 从 `update_current_trace(release=...)` 移除，改用 `LANGFUSE_RELEASE` env（Clawith 已支持）。
   > https://langfuse.com/docs/observability/sdk/upgrade-path/python-v3-to-v4.md（表格 release 行）

7. **usage 分桶重叠双计**：见 4.5，inclusive 计数必须转 exclusive。
   > https://langfuse.com/docs/observability/features/token-and-cost-tracking.md

8. **mask 函数要快**：慢的 mask 阻塞 OTel export 队列；`mask_otel_spans` 抛异常或返回非法结果会丢弃整批。
   > https://langfuse.com/docs/observability/features/masking.md

9. **isolated TracerProvider 的 parent 关系错乱**：隔离 TracerProvider 会与其它 provider 共享 OTel context，产生跨 provider 的父子/孤儿 span。
   > https://langfuse.com/docs/observability/sdk/advanced-features.md（"Isolated TracerProvider"）

10. **自托管版本门槛**：OTel-based SDK 要求 Langfuse platform ≥ 3.63.0；OTel endpoint 于 v3.22.0 引入，遇 4xx 先升级。
    > https://langfuse.com/docs/observability/sdk/troubleshooting-and-faq.md；https://langfuse.com/integrations/native/opentelemetry.md（"Troubleshooting"）

11. **预留属性 key 段被静默丢弃**：属性 key 含 `__proto__`/`constructor`/`prototype` 段会被 ingestion 丢弃（防 prototype pollution）。
    > https://langfuse.com/integrations/native/opentelemetry.md（"Reserved attribute key segments"）

---

## 8. 对 Clawith 现状的差距清单

> 对照「背景」里已确认的现状逐条评估，优先级 P0=必须 / P1=应该 / P2=可选。

| # | 差距/可优化点 | 优先级 | 一句话理由 | 来源 |
| --- | --- | --- | --- | --- |
| 1 | run 根节点用 `as_type="agent"` 而非 `span` | **P0** | `agent` 类型启用 agent graph 视图与按 agent 过滤，是 Langfuse 对 agent 编排的一等公民类型。 | observation-types.md |
| 2 | 多租户隔离确认用「每租户独立 client」，勿用实验性单 client 多 project 路由 | **P0** | 官方单 client 多 project 路由标记 experimental，第三方 span 会泄漏到所有 project；Clawith 的 httpx LLM 调用正好规避不了这个坑。 | advanced-features.md "Multi-project setups" |
| 3 | 用 `propagate_attributes()` 统一传播 user/session/trace_name/metadata/environment | **P0** | v4 属性必须传播到每条观测（非只记 trace），否则过滤/聚合失效；这是 v4 的核心 API 变化。 | python-v3-to-v4.md |
| 4 | 补 `environment`（production/staging/development）属性 | P1 | 防止测试 trace 污染生产 dashboard/evaluator，官方明确列为最佳实践。 | best-practices.md "Set the environment" |
| 5 | 确认 masking 用的是 `mask_otel_spans`（v4 推荐）而非 legacy `mask` | P1 | legacy `mask` 只覆盖 SDK API 数据，覆盖不到第三方 instrumentation 的 span。 | masking.md |
| 6 | 确认 usage 分桶互斥（cache read/write 拆分）符合官方契约 | P1 | 现状已做 cache read/write 拆分（正确方向），需核对是否严格互斥、`total` 是否求和而非独立桶，避免双计。 | token-and-cost-tracking.md "Usage types are mutually exclusive buckets" |
| 7 | DeepSeek/推理模型的 cost：为 reasoning 模型显式 ingested usage | P1 | 官方明确 reasoning 模型（o1 等）无法推断 cost；DeepSeek 含 reasoning_content，需保证 ingested usage 才能有成本。 | token-and-cost-tracking.md "reasoning models" |
| 8 | 为 DeepSeek/Qwen 等自定义模型补 model definition（含 pricing tiers） | P1 | cost 推断要求 `model` 值匹配 `match_pattern`，自托管模型无内置价格需手动加。 | token-and-cost-tracking.md "Adding custom model definitions" |
| 9 | 第一方 scores 补 `score_config` + 幂等键（`score_id`） | P1 | 标准化的 score config 保证 name/type/范围一致；幂等键避免重复/覆盖失控（run settle 多次写需防重复）。 | scores-via-sdk.md "Enforcing a Score Config"、"Preventing Duplicate Scores" |
| 10 | 若走 LLM-as-a-judge，用 observation 级而非 trace 级 evaluator | P1 | trace 级已 deprecated；observation 级更快、更省、更精确。 | llm-as-a-judge.md |
| 11 | trace 命名遵守低基数规则（无 run_id/动态值入 name） | P1 | name 是 API，含动态值会导致无法分组过滤；run 特定值应放 metadata（现状已记 metadata，需核对 name 是否干净）。 | best-practices.md "Choose good names" |
| 12 | 确认 generation 名不含模型名 | P1 | 模型名入 name 会随换模型破坏 evaluator/dashboard。 | best-practices.md "Choose good names" |
| 13 | 采样 `LANGFUSE_SAMPLE_RATE`（高流量租户降本） | P2 | trace 级采样可显著降量，但对 Clawith 全量观测诉求可能暂不需要。 | sampling.md |
| 14 | 数据保留 TTL 策略 | P2 | 自托管默认永久保留；开 TTL 需 Enterprise Edition，且需配合 Blob Storage Export。 | data-retention.md |
| 15 | generation input/output 用 OpenAI 消息格式渲染 | P2 | 非结构化 prompt 会显示为 raw JSON blob，用 role/content 格式 UI 更友好。 | best-practices.md "Choose meaningful input and output" |
| 16 | 链接 prompt 到 generation（若引入 prompt management） | P2 | 可追踪哪个 prompt 版本、跨版本对比指标；Clawith 目前未在 Langfuse 管理 prompt。 | best-practices.md "Link prompts to traces" |

### 优先级小结

- **P0（3 项）**：#1 `agent` 类型、#2 独立 client 多租户、#3 `propagate_attributes` 统一传播。
- **P1（8 项）**：#4 environment、#5 mask_otel_spans、#6 usage 互斥核对、#7 reasoning 模型 ingested usage、#8 自定义 model definition、#9 score config + 幂等键、#10 observation 级 evaluator、#11/#12 命名规范。
- **P2（4 项）**：#13 采样、#14 TTL、#15 消息格式、#16 prompt 链接。

---

## 附：核心参考 URL 索引

- 最佳实践：https://langfuse.com/docs/observability/best-practices.md
- 埋点指南：https://langfuse.com/docs/observability/sdk/instrumentation.md
- SDK 高级特性：https://langfuse.com/docs/observability/sdk/advanced-features.md
- Python v3→v4 迁移：https://langfuse.com/docs/observability/sdk/upgrade-path/python-v3-to-v4.md
- OTel 集成：https://langfuse.com/integrations/native/opentelemetry.md
- LangGraph 集成：https://langfuse.com/integrations/frameworks/langgraph.md
- LangChain 集成：https://langfuse.com/integrations/frameworks/langchain.md
- 观测类型：https://langfuse.com/docs/observability/features/observation-types.md
- 采样：https://langfuse.com/docs/observability/features/sampling.md
- 批处理：https://langfuse.com/docs/observability/features/queuing-batching.md
- Masking：https://langfuse.com/docs/observability/features/masking.md
- Token & Cost：https://langfuse.com/docs/observability/features/token-and-cost-tracking.md
- 数据保留：https://langfuse.com/docs/administration/data-retention.md
- Sessions：https://langfuse.com/docs/observability/features/sessions.md
- Users：https://langfuse.com/docs/observability/features/users.md
- Trace IDs：https://langfuse.com/docs/observability/features/trace-ids-and-distributed-tracing.md
- Scores 概览：https://langfuse.com/docs/evaluation/scores/overview.md
- Scores via SDK：https://langfuse.com/docs/evaluation/evaluation-methods/scores-via-sdk.md
- LLM-as-a-Judge：https://langfuse.com/docs/evaluation/evaluation-methods/llm-as-a-judge.md
- 评估概念：https://langfuse.com/docs/evaluation/core-concepts.md
- RBAC：https://langfuse.com/docs/administration/rbac.md
- SDK 排障：https://langfuse.com/docs/observability/sdk/troubleshooting-and-faq.md
