# Clawith Langfuse 埋点差距分析（代码核对版）

> 日期：2026-09-05
> 上游调研：`20260905-langfuse-instrumentation-best-practices.md`（官方文档 24 页，每条带 primary source）
> 本文：把调研结论**逐条对着 `backend/app/services/observability/` 真实代码核对**，剔除「已实现」的假阳性，补上代码侧新发现，给出最终优先级。

## 结论一句话

Clawith 手写 OTel-backed facade 的**选型正确**（Langfuse Python SDK v4 本就是 OTel 薄封装；LangChain `CallbackHandler` 只能追踪走 LangChain 抽象的调用，Clawith 的 httpx 调用用它会丢 generation 层＝成本追踪核心）。现状的 cache read/write 拆分、usage 互斥分桶、截断+redaction、per-tenant 独立 client、score 幂等键，都已与官方最佳实践对齐。真正要改的是**少数几处**：`as_type="agent"`、补 environment、trace 命名低基数化、以及主 LLM 路径漏记 input 这个隐藏缺口。

---

## 一、逐条核对调研 16 项差距（真伪标注）

| 调研 # | 调研结论 | 代码核对结果 |
| --- | --- | --- |
| 1 | run 根节点用 `as_type="agent"` | ✅ **真缺口**。`tracing.py:718-719` 用 `as_type="span"`、name=`"run"`，未启用 agent graph 视图 |
| 2 | 多租户「每租户独立 client」 | ❌ **已实现**。`_get_client`（`tracing.py:205-233`）按 `_tenant_clients` dict 每租户建 client，`LANGFUSE_TENANT_KEYS` 隔离 project，正是官方推荐做法 |
| 3 | `propagate_attributes()` 统一传播 | ⚠️ **大部分已实现**。`observe_run`（`:701-709`）已用 `propagate_attributes(user_id/session_id/trace_name/metadata)`，嵌套观测走 `_user_session_propagation`。**唯独缺 environment/tags/version** |
| 4 | 补 `environment` | ✅ **真缺口**。全仓无 environment 传播，测试/生产 trace 会混进同一 dashboard |
| 5 | 用 `mask_otel_spans` 而非 legacy `mask` | ⚪ **不适用**。Clawith 在应用层 `mask_text` 先于 SDK 传入（`:445,515,721`），且 LLM 走自定义 httpx、无第三方 OTel instrumentation，故不存在「覆盖不到的第三方 span」。方案本身成立 |
| 6 | usage 分桶互斥核对 | ❌ **已实现**。`_map_usage`（`:287-330`）已做 cache read/write 拆分、`input`＝未命中余量、`total`＝各桶求和，与官方「mutually exclusive buckets」契约一致 |
| 7 | reasoning 模型 ingested usage | ⚠️ **部分缺口**。`TokenUsage`（`token_tracker.py:16-35`）**无 reasoning 字段**，推理 token 折进 `output`。DeepSeek 推理/可见 token 同价故成本无误，但缺 `output_reasoning_tokens` 分桶，成本归因不透明 |
| 8 | 自定义 model definition | ✅ **真缺口（配置侧）**。DeepSeek/Qwen 自托管需在 UI `Models` 配 `match_pattern` 定价，否则 cost 依赖 ingested usage 推断、无模型单价 |
| 9 | score config + 幂等键 | ⚠️ **半实现**。幂等键 ✅（`scores.py:276,284,293` 用确定性 `score_id`）；ScoreConfig ❌（`create_score` 未传 `config_id`） |
| 10 | observation 级 evaluator | 🔜 **未来项**。judge 已配置（记忆），上线前确认目标用 observation 级（trace 级已 deprecated） |
| 11 | trace 命名低基数 | ✅ **真缺口**。`trace_name=f"run:{run_id}"`（`:707`）把 run_id 塞进 name，每条 trace 名字唯一，无法按 trace name 聚合/建 dashboard |
| 12 | generation 名不含模型名 | ❌ **已实现**。name=`"llm"` 稳定，model 是独立属性 |
| 13 | 采样 `LANGFUSE_SAMPLE_RATE` | ⚪ **未做（P2）**。高流量租户可选，当前全量观测诉求下暂不需要 |
| 14 | TTL 数据保留 | ⚪ **部署侧 P2**。自托管默认永久保留；开 TTL 需 Enterprise Edition |
| 15 | input/output OpenAI 消息格式 | ⚠️ **部分**。root output 是纯文本（合适）；generation output 是 `{"content","reasoning_content"}` dict、非 OpenAI 消息格式，UI 渲染为 JSON blob 而非对话卡片 |
| 16 | prompt 链接 | ⚪ **明确不做**（prompt management 在范围外） |

---

## 二、代码侧新发现（调研未覆盖）

### A. 主 LLM 路径漏记 input（最实质的缺口，P1）

- `backend/app/services/llm/caller.py:677-682`：`observe_generation(...)` **只传 name/model/provider/agent_id，不传 `input=api_messages`**。
- 对照 `single_step.py:190-196`：同一 facade 的 `input=api_messages` 是传的。
- `caller.py` 是主 agentic 工具循环路径（`call_llm`/`call_llm_with_failover`，被 `node_executor.py:39` 和 ACP handler 使用），`single_step.py` 只是单次补全的窄路径。
- **后果**：占绝对多数的 LLM generation span 的 input 为空——看不到「哪条 prompt 催生了坏输出」，也违背 `tracing.py:10-11` 文档契约「every llm generation records the full prompt as input」。
- **建议**：补 `input=api_messages`（走既有 `mask_text` 红action+截断，成本可控）。若担心工具循环多轮 prompt 过大，可只传最后一轮或裁剪，但至少要传。

### B. `flush()` 是死代码（P2）

- `tracing.py:753-764` 导出 `flush()` 但**全仓零调用**，也无 FastAPI shutdown 钩子。
- SDK 有 atexit 注册的 shutdown，长驻 uvicorn 正常退出基本安全；但优雅停机时最后一批 span 有丢失窗口。
- **建议**：在 app lifespan shutdown 调一次 `flush()`。

### C. 错误 `status_message` 未走 redaction（P2）

- `mark_error`/`mark_retry` 把 `str(exc)[:400]` 直接写入 `status_message`（`tracing.py:383,389,629,635`），**未过 `mask_text`**。provider/tool 错误文本可能回显敏感值（如 key）。
- **建议**：写 status_message 前套 `mask_text`。

### D. tool span 无 input/output（P2，需权衡）

- `observe_tool`（`:553-575`）只记 latency/error + 对齐键，参数与输出都不采集（docstring 明示参数有意不采）。
- 好处是防泄密、ledger（`agent_tool_executions`）是权威；代价是 Langfuse 里看不到工具行为，排障得跳去 DB。
- **建议**：输出可考虑**经 `mask_text` 后**采集（参数维持不采），让 Langfuse 成为一站式排障面。

### E. environment 若只加在 observe_run，嵌套传播会断（实现细节）

- `_user_session_propagation`（`:591-605`）只 set `user_id`/`session_id`。若 environment 只加进 `observe_run` 的 propagate_attributes，嵌套观测会因新建了更窄的 propagation CM 而丢 environment。**要加就两处一起加**（或收口成一个统一 propagation 帮助函数）。

---

## 三、最终优先级行动清单

**P0（真改，影响面大）**
1. `observe_run` 根节点 `as_type="span"` → `"agent"`（启用 agent graph 视图）。注意：会改变 root 类型，需确认既有 dashboard/过滤器不受影响。
2. 补 `input` 到 `caller.py` 的 `observe_generation`（即 §二.A）。

**P1（应该做）**
3. 统一传播补 `environment`（`observe_run` + `_user_session_propagation` 两处，见 §二.E），值取 `ENV`/settings。
4. trace 命名低基数化：`trace_name=f"run:{run_id}"` → 稳定名（如 `agent-run` 或按 `command_type`），run_id 已在 metadata（`:708`）不必入 name。
5. 第一方 scores 补 `config_id`（ScoreConfig 强制 name/type/范围），幂等键已有。
6. reasoning 模型：`TokenUsage` 增 reasoning 字段 → `_map_usage` 出 `output_reasoning_tokens` 分桶（DeepSeek 成本不误但归因更透明）。
7. 自定义 model definition（UI `Project Settings > Models` 配 DeepSeek/Qwen `match_pattern` 定价）。

**P2（可选/部署侧）**
8. lifespan shutdown 调 `flush()`（§二.B）。
9. 错误 `status_message` 过 `mask_text`（§二.C）。
10. tool 输出经 `mask_text` 后采集（§二.D，需权衡）。
11. generation output 改 OpenAI 消息格式渲染（可选，体验项）。
12. 采样 `LANGFUSE_SAMPLE_RATE` / TTL（Enterprise 门槛），高流量时再评估。

**明确不做 / 维持现状**
- 换 LangChain `CallbackHandler`、单 client 多 project 路由（已用更稳的独立 client）、`mask_otel_spans` 改造（应用层 mask 已够）、prompt 链接、cost_usd 第一方 score（Langfuse 原生算，见 scores.py 注释）。

---

## 实施记录（2026-09-05）

已落地（本次提交）：

| 项 | 改动 |
| --- | --- |
| P0 #1 | `tracing.py` `observe_run` 根节点 `as_type="span"` → `"agent"`（启用 agent graph 视图） |
| P0 §二.A | `caller.py` `observe_generation` 补 `input=api_messages`（主 agentic 循环 generation 补齐 prompt 输入） |
| P1 #4 | 新增 `LANGFUSE_ENVIRONMENT` 配置 → `_build_client` 传 `environment=`（SDK client 级 environment，覆盖嵌套观测） |
| P1 #11 | `trace_name=f"run:{run_id}"` → 低基数常量 `"agent-run"`（run_id 仍在 metadata） |
| P2 §二.C | `mark_error`/`mark_retry` 的 `status_message` 经 `mask_text` 脱敏 |

验证：`test_observability_tracing.py` / `test_observability_scores.py` 68 passed；`test_llm_failover.py` / `test_llm_single_step.py` 69 passed；ruff 通过；arch-guard P0 全绿（新增 4 条测试覆盖 agent 类型/trace 名/environment/status 脱敏）。

**第二批提交（bfe7edc7）**：

| 项 | 改动 |
| --- | --- |
| P1 #6 | `token_tracker.py` `TokenUsage` 新增 `reasoning_tokens` 字段 + `extract_token_usage` 从顶层 `reasoning_tokens` / `completion_tokens_details.reasoning_tokens` 提取；`output_tokens` 语义保持 inclusive（配额/计费账不变） |
| P1 #6 | `tracing.py` `_map_usage` 仅 `provider=="deepseek"` 时拆 `output_reasoning_tokens` 桶（`output = max(output - reasoning, 0)`，total 仍=各桶求和）；非 deepseek 不拆、不发 reasoning 桶（避免双计） |
| 部署注入 | `scripts/deploy.sh` export `LANGFUSE_ENVIRONMENT`（默认 `production`，调用前可 `LANGFUSE_ENVIRONMENT=staging` 覆盖）；`docker-compose.yml` 注入 `LANGFUSE_ENVIRONMENT: ${LANGFUSE_ENVIRONMENT:-}` |

验证：`test_token_tracker.py` / `test_observability_tracing.py` / `test_observability_scores.py` 87 passed；ruff 通过；arch-guard P0 全绿；`bash -n scripts/deploy.sh` 与 `docker compose config` 通过（新增 3 条测试：reasoning 提取、deepseek 拆分、非 deepseek 不拆）。

**留待后续（需外部配置或独立决策）**：
- score `config_id`（需先在 Langfuse UI 建 ScoreConfig）
- 自定义 model definition（UI `Project Settings > Models` 配 DeepSeek/Qwen 定价）
- tool 输出采集（泄密 vs 排障的权衡）、采样/TTL（部署侧，高流量再评估）
