# 20260903 Langfuse 埋点完整性修复：reasoning 记录 + input 行为统一

状态：已实施（2026-09-03，未部署）。同日追加「监控盲区三件套」：TTFT、run 根 input、跨 run parent 关联（见 §8）。

## 1. 结论

Langfuse 的 `llm` generation 观测只记录 `response.content`，思考内容（`reasoning_content`）
完全不落 trace；且两条 LLM 路径的 input 记录行为不一致（`call_llm` 关闭、`complete_llm_once` 开启）。
本方案补齐 reasoning 记录并统一 input 行为。

## 2. 已核实的缺口（代码 + 线上证据）

| # | 缺口 | 证据 |
|---|---|---|
| G1 | generation output 无 reasoning | `caller.py:688` 与 `single_step.py:209` 均 `gen.set_output(response.content)`；`extract_embedded_reasoning` 在埋点之后才执行（`caller.py:714-721`、`single_step.py:218-220`），埋点时 reasoning 未合并 |
| G2 | 两路径 input 行为不一致 | `caller.py:676` 显式 `capture_input=False`（986fdb5d Phase 1 引入，当时不存 prompt）；`single_step.py:179` 走默认 `capture_input=True` 记录了完整 `api_messages` |
| 线上 | trace 输出无思考 | 2026-09-02 trace `a00e9d86006b0886e96ad9c295c60737`：5 条 `llm` generation 的 output 均为纯文本结论，无任何 reasoning |

非缺口（勿重复建设）：usage_details.output 已含 DeepSeek 思考 token 数（成本可算）；
前端 `on_thinking` 流式显示、DB `tool_call_log.reasoning_content`、回传 provider 的
`reasoning_content`（DeepSeek 协议要求）均已有。

## 3. 目标

1. `llm` generation 的 output 完整记录 content + reasoning_content（两路径一致）。
2. 两路径 input 均完整记录（`api_messages`）。
3. 安全防线不弱化：secrets 脱敏（正则）保持先行；run 根 span（judge 输入窗口）与
   tool/node span 的 4000 字符截断合同不变。

## 4. 设计决策

### D1 output 结构化为 `{"content", "reasoning_content"}`

- 埋点处直接用 `extract_embedded_reasoning(response.content, response.reasoning_content)`
  得到合并后的 `(visible_content, merged_reasoning)`，set_output 结构化 dict。
  该调用只读不改 response，后续原有 extract 逻辑（`caller.py:714+`）保持不变、幂等。
- 影响面：generation output 从纯字符串变为 JSON 对象。**实施前 grep 确认**没有
  evaluator/查询逻辑依赖 generation output 为纯文本（tool span 的 CODE evaluator
  消费的是 tool span，与此无关，需复核一遍）。

### D2 截断上限按观测类型分化，防线保留

- `reasoning_content` 可能远超 4000 字符（DeepSeek 思考动辄数千 token）。若沿用
  `mask_text` 的 `_MAX_STRING_CHARS=4000`，「完整记录」落空。
- 做法：`mask_text(value, *, max_string_chars: int = _MAX_STRING_CHARS)` 参数化；
  `GenerationHandle` 构造时接收 `max_string_chars`；仅 `observe_generation` 路径传
  新常量 `_GENERATION_MAX_STRING_CHARS = 65536`（约 2 万+ token 思考文本，足够；
  ponytail: 若未来思考超限再提）。
- 上限是**整树**参数：generation output 的 `content` 键随之放宽到 64k（最简实现的
  取舍，content 无独立 4k 保护；脱敏仍先行，无安全面扩大，仅体积上界抬高）。
- 不变：RunHandle（`tracing.py`）与 `_observe_span` 的 tool/node 观测仍用 4000，
  对应测试 `test_observe_run_output_truncates_to_max_string_chars` 不动。
- 脱敏不变：先正则脱敏、后截断的顺序保持。

### D3 input 统一为记录

- 删除 `caller.py:676` 的 `capture_input=False`（恢复默认 True）。
- `observe_generation` 的 `capture_input` 参数随之失去唯一使用方：实施时
  `grep -rn capture_input backend/` 确认无他处使用后删除该参数（宪法：Delete
  verified dead code）；`_observe_span` 的同名参数因 tool/node 观测共用先保留。
- 体积影响：call_llm 的 `api_messages` 含 system prompt + 动态上下文 + 工具结果，
  每条字符串受 4000 字符上限约束（input 走 `mask_text` 默认上限，**不**用 64k，
  防止 input 无限膨胀）。

## 5. 改动清单

| 文件 | 改动 |
|---|---|
| `backend/app/services/observability/tracing.py` | ① `_mask_string`/`mask_text` 增加 `max_string_chars` 参数（默认 4000）② 新增 `_GENERATION_MAX_STRING_CHARS = 65536` ③ `GenerationHandle.__init__`/`finalize` 透传该参数 ④ `observe_generation` 构造 handle 时传 64k ⑤ （若 D3 确认）删除 `observe_generation` 的 `capture_input` 参数 |
| `backend/app/services/llm/caller.py` | ① 删除 `capture_input=False` ② 埋点块内：`visible, merged = extract_embedded_reasoning(response.content, response.reasoning_content)` 后 `gen.set_output({"content": visible, "reasoning_content": merged})` |
| `backend/app/services/llm/single_step.py` | 埋点块内同样改为结构化 `set_output`（extract 只读调用） |
| `backend/tests/test_observability_tracing.py` | 新增：`observe_generation` 输出 dict 时 reasoning 键完整（超 4000 不截断）、其余键仍 4000 截断、脱敏仍生效；`capture_input` 参数删除后的相关断言清理 |
| Agent Note | 本变更属非机械行为变更，同步更新 owning note（可观测性/埋点契约），与 commit 三方对齐 |

## 6. 验证

实施验证记录（2026-09-03）：

1. `scripts/arch-guard.sh` ✅ P0 全干净（仅既有前端行数警告）。
2. `uv run --extra dev pytest tests/test_observability_tracing.py` ✅ 45 passed（新增 4 个：
   input 默认捕获、dict output 双键超 4k 存活、超 64k 截断、reasoning 内 secrets 脱敏）。
3. `uv run --extra dev pytest tests/test_llm_single_step.py tests/test_llm_failover.py` ✅ 69 passed。
4. `uv run --extra dev ruff check` 4 个改动文件 ✅ All checks passed。
5. `uv run --extra dev pyright` 改动区域零新错误（31 个报错均为既有：`LLMClient.close`
   类型缺口、`asdict` 等，位于未改动行）。

待办（部署后）：

1. 本地跑一条带工具的真实 run，在 Langfuse 中确认：
   - `llm` generation 的 output 为 `{"content": ..., "reasoning_content": ...}` 且思考完整；
   - 同一 trace 中 `call_llm` 与 `complete_llm_once` 路径的 generation input 均非空。
2. 部署后抽查生产 trace 一条，核对上述两点。

## 7. 风险与取舍

- 存储膨胀：全量 input + 全量 reasoning 入 trace。DeepSeek 思考文本是主要增量，
  64k 上限为天花板。Langfuse 是自托管，可控；如后续成本超预期，将
  `_GENERATION_MAX_STRING_CHARS` 调低即可（单一常量，无需改调用点）。
- JSON output 对历史数据无影响（只作用于新写入）；对 UI 的「output」列显示为
  JSON 文本，可读性略降，换来完整思考内容。
- 脱敏有效性不变（正则先行）；64k 只是字符上限而非跳过脱敏。

## 8. 追加：监控盲区三件套（2026-09-03）

评审报告 `docs/analysis/20260903-langfuse-reasoning-fix-review.md` 盘点盲区后，按用户确认补齐前三项：

1. **TTFT**：`GenerationHandle.set_completion_start()` + finalize 写
   `completion_start_time`（Langfuse 原生字段）。采集点=流式首个可见 content
   token（`caller.py::_buffer_chunk`、`single_step.py::_first_token_marker`；
   `_delta_with_marker` 透传 gate 的 bool 发布语义）。非流式 complete 不记录。
2. **run 根 input**：`observe_run(input=...)` 记录 run goal（`langgraph_driver.execute`
   传 `run.goal`，facade 内 mask+4000 上限）。
3. **跨 run parent 关联**：`observe_run` 新增 `_run_observation_id` contextvar +
   `current_observation_id()`；A2A 创建子 run 时（父 run observe_run 上下文内）把
   `parent_trace_id/parent_observation_id` 写入 command payload，子 run execute 时
   解析为 Langfuse `trace_context`（`{trace_id, parent_span_id}`）传入
   `start_as_current_observation`——子 trace 挂到父 trace 树。无父上下文（独立 run）
   时自然无 parent，行为不变。

SDK 能力依据（本地 venv langfuse 源码核查）：`span.update(completion_start_time=datetime)`
（span.py:645）、`span.id`/`span.trace_id`（span.py:131-132）、
`start_as_current_observation(trace_context=...)` 跨 trace 挂载（client.py 实现 +
observe.py 同机制用法）。

验证（2026-09-03）：arch-guard P0 干净；affected 测试集 165 passed（observability 54、
langgraph_driver、a2a×2、single_step、failover）；ruff 全过；pyright 改动区零新错误
（caller 10 / tracing 17 / single_step 4 均与改动前基线一致，为既有问题）。
一处回归被测试抓住并修复：`_delta_with_marker` 曾吞掉 `_VisibleDeltaGate.push` 的
bool 返回（client 层发布语义），已透传。

部署后验证：生产 trace 抽查 TTFT 非空、run 根有 input、A2A 子 trace 挂在父树。
