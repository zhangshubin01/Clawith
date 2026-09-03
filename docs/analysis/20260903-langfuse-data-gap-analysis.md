# Langfuse 采集数据缺口分析（数据实证）

- 日期：2026-09-03
- 范围：Clawith 生产 Langfuse（project `clawith`）2026-09-01T04:14Z ~ 2026-09-03T01:27Z，共 14782 条 observation
- 方法：直接对 `langfuse-clickhouse-1` 的 `events_full` / `events_core` 两表做只读聚合（API 层 null 过滤不支持，放弃抽样方案）；与部署 commit `4d3fe431` 及容器内实际代码逐文件 diff 核对（`diff -q` 全同）
- 结论速览：**无数据丢失**；3 个缺口为「已修复待部署」；其余为设计取舍、provider 限制或 Langfuse v4 平台展示行为。真正的额外发现是 3 个此前未意识到的现象（§7）

## 1. 关键澄清：events_core 的「200 字符截断」不是数据丢失

Langfuse v4 采用双表分层：

- `events_full`：全量原始数据（input 最长实测 192k 字符，无截断）
- `events_core`：UI 列表/聚合查询用轻量表，由物化视图 `events_core_mv` 写入，**input/output 各截 200 字符、metadata 每个值截 200 字符**（`leftUTF8(x, 200)`，官方建表语句实锤）

影响面：

- Langfuse **UI 列表视图与 list API**（默认走 events_core）看到的 input/output/metadata 是 200 字符预览——「数据被截断」是错觉
- 按 ID 取数（`getObservationById` 等）默认 `renderingProps.truncated=false` → 走 events_full，**完整数据可取**；server 侧另有 `LANGFUSE_SERVER_SIDE_IO_CHAR_LIMIT`（默认 1000）兜底
- **judge 输入窗口不受影响**：实测 judge generation 的 input 5288 字符、含完整 root output、无 `[truncated]` 标记
- 后续做数据体检务必查 `events_full`，查 `events_core` 会系统性误判字段填充率（本次分析前半段就踩了这个坑）

## 2. 全景（events_full 口径）

| 维度 | 数值 |
|---|---|
| observation 总数 | 14782（SPAN 11772 / TOOL 1881 / GENERATION 1140*） |
| run 根 span | 108 条（≈36 runs/天），其中 heartbeat 74 条 |
| evaluator span（app root） | 3762 条 = tool-failure 1881 + tool-retry-exhausted 1881（两规则对每条 TOOL 全量执行） |
| judge 执行 | 6 次（run-goal-judge，10% 采样预期 ~11，实际 5.6%，含规则创建前的 run） |
| 心跳占比 | heartbeat 4881/14782 = 33%（其中 node span 3407、TOOL 951、GENERATION 523） |

*events_full 1140 条 GENERATION = events_core 1129 条，差 11 条为 ReplacingMergeTree 未合并的重复 span（无影响）。实际唯一 generation：'llm' 1134 + judge 6。

## 3. 分类型字段填充率（events_full 实测）

| 类型 | input | output | usage | cost | model | metadata | completionStartTime |
|---|---|---|---|---|---|---|---|
| GENERATION | 1134/1134 100% | 866/1134 76.4% | 1134/1134 100% | 1134/1134 100% | 1134/1134 100% | 1134/1134 100% | **0/1134** |
| TOOL | 0（设计） | 1881/1881 100% | 0（无 token） | 0（无定价） | 0 | 1881/1881 100% | — |
| node span | 0（设计） | 0（设计） | 0 | 0 | 0 | 7896/7896 100% | — |
| run 根 | **0/108** | 106/108 98% | 0 | 0 | 0 | 108/108 100% | — |

要点：

- **GENERATION input 100% 有值且完整**（`single_step.py` 传 `input=api_messages`）。此前担心「caller 路径 `capture_input=False` → 大量 null」被数据证伪：`llm` generation 的 metadata 无 `tool_round` 键（caller 独有），且 TOOL span 的 parent 全部指向带 input 的 generation——**本窗口内 generation 全部出自 `single_step.py`**，caller 的 `capture_input=False` 分支实际未产出数据
- input 长度分布：<10k 140 / 10k-30k 274 / 30k-80k 424 / ≥80k 300 条（最长 192k，中文 0.47-0.54 token/字符换算后与 usage.input 量级吻合）
- **GENERATION output 空 268 条**（23.7%）：全部 253 条 DEFAULT 级均挂有 TOOL 子 span（avg 1 个）且 usage.output 均值 1131 tokens——**纯工具轮**（DeepSeek 工具轮 `content` 为空，usage.output 计入工具调用 JSON）。15 条 ERROR 全部有 status_message（402 余额不足×6、DNS 解析失败×4、RemoteProtocolError×3、ReadTimeout×1、Server disconnected×1），usage 已记录——**不是缺口，是正常业务形态**
- 有 output 的 generation（866 条）也 100% 挂 TOOL 子 span——生产流量以工具轮为主，文本轮输出长度：<500 629 / <5k 238 / <20k 3
- run 根 output 106/108 有值（摘要，均值 562 字符）——judge 输入窗口有真实内容
- TOOL 100% 有 output（均值 <1k 字符），metadata 富集：`tool_execution_id` / `tool_call_id` / `side_effect_classification` / `retry_policy` 全量
- SPAN/TOOL 无 cost：Langfuse 只对 GENERATION 按模型定价计费（3 天 3.94+4.05+0.02 USD），工具/节点层成本不可见

## 4. 已修复待部署（3 项，代码在分支 f-shubin-0806，部署后消失）

| 缺口 | 现状证据 | 修复 |
|---|---|---|
| TTFT 全空 | completionStartTime 0/1134 | e3204afb（首 token 标记 + `completion_start_time`） |
| reasoning_content 缺失 | output 含 `reasoning_content`/`<think>` 的 0 条 | 1224cf77（结构化 output + `<think>` 块合并） |
| run 根 input 空 | 108/108 空（goal 目前在 `metadata.goal` 里，完整） | e3204afb（`observe_run(input=run.goal)`） |
| 跨 run parent 无关联 | `is_app_root AND parent_span_id!=''` = 0 条 | e3204afb（trace_context 跨 trace 挂载） |

## 5. 设计取舍（不改，记录在案）

- **工具入参不入 trace**：TOOL input 全空、node span input/output 全空——`observe_tool` docstring 明确「Tool arguments are intentionally not captured」，权威账本是 `agent_tool_executions` 表。GENERATION 层也**不写 `tool_calls` 数组**（1140/1140 空）：模型请求的工具 JSON 只以 TOOL 子 span 存在，generation 自身看不到「这轮要了哪个工具」，只能靠父子关系反查
- **文本轮 output 是模型文字而非最终回复**：最终回复在 run 根 output（摘要）与业务存储里
- **heartbeat 占 1/3 数据量**（含 523 条 generation ≈ 3 天 LLM 调用的 46%）——心跳是产耗大户，若做成本分析必须按 `run_kind=background` 单独拆
- TOOL 层无 ERROR 标记：工具失败由 CODE evaluator（tool-failure/tool-retry-exhausted，对每条 TOOL 全量执行）捕获，不在 span level 上体现

## 6. Provider 限制（DeepSeek）

- usage 不拆「思考 token vs 回答 token」：output 桶混计，无法单看思考成本（Langfuse 官方定价模型也认 output 一桶）
- 无 cache_creation 桶：`input_cache_read` 1065/1134 有值（缓存命中普遍，单次 512~32768 tokens），DeepSeek 不回传 cache 写入量
- 缓存命中 token 已按 f451a90c/e72fb8e7 对齐价格键，无双重计费（此前已修）

## 7. 新发现（此前未意识到，非阻塞）

1. **Langfuse UI 列表视图永远只显示 200 字符预览**（§1）——看完整 input/output 必须进详情页或走 events_full 的 API。这与 Clawith 侧 64k/4000 的上限无关
2. **evaluator span 不是噪声，是第一方评分的载体**：tool-failure 与 tool-retry-exhausted 两条 CODE 规则对每条 TOOL 全量执行，每个执行产 1 条 span + **1 条 score**（3 天各 1903 条 `tool_failure`/`tool_retry_exhausted` score，即工具失败率指标的数据源）。这 3762 条 span（占 SPAN 32%）是逐工具评分的固有成本——**不能采样**（采样=丢失败率指标），若 UI 列表噪声困扰，用过滤视图而非改采样率
3. **run 根 span 只占 SPAN 的 0.9%**（108/11772）：根级聚合（按 run 统计）需按 `name='run'` 过滤而非 `is_app_root`（后者把 3762 条 evaluator span 也算进去，`is_app_root` 语义是「外部父级的逻辑根」）

## 9. 优先级待办（按收益/风险）

**P0 部署 f-shubin-0806 的观测修复（1224cf77 + e3204afb）——唯一必须的动作**

- 收益：一次部署同时关闭全部 4 个实锤缺口——TTFT（0/1134 → 全量）、reasoning 记录（0 → 全量）、run 根 input（0/108 → 108，goal 从 metadata 转正为 input）、跨 run parent 关联（0 → 有）。监控能力型收益：推理行为可观测、首 token 延迟可观测、judge 输入窗口语义完整、子 run 血缘可追溯
- 风险：
  - 部署固有风险（杀在途 run）→ 已有 6f43d25b 账本终态复用+审计防护，按 clawith-prod-deploy 清单执行
  - 唯一新代码风险点：e3204afb 改动 a2a_runtime 子 run 创建的 command payload + langgraph_driver 的 trace_context 解析——部署后重点验证 a2a 子 run 正常发起、父子 trace 关联出现
  - 1224cf77 改变 generation output 结构（`{"content","reasoning_content"}`）——下游读 generation output 的只有 Langfuse 内展示/分析（judge 读 run 根 output，前端 reasoning 走 DB tool_call_log，均不受影响）
  - 已过 165 测试 + arch-guard + ruff + pyright 基线
- 部署后验证：§8 填充率查询复跑——TTFT 非空、output 含 reasoning_content、run 根 input=goal、app-root with parent 出现

**P1 数据复检（跟随部署，半小时级）**

- 复跑 §8 四条查询确认缺口关闭；顺带确认 judge 输入窗口（root output）内容未因 input 新增而变化

**P2 分析口径约定（零改动，已部分落地）**

- 成本/用量分析按 `run_kind` 拆心跳（heartbeat 占 LLM 调用 46%）
- 根级聚合按 `name='run'` 过滤，不按 `is_app_root`
- events_core/full 双表陷阱已存记忆 langfuse-events-core-vs-full

**P3 明确不做（评估过，收益不足或风险过高）**

- tool-failure/tool-retry-exhausted 加采样：**否决**——它们各产 1903 条 score/3 天（逐工具评分），采样=丢工具失败率指标；UI 噪声用过滤视图解决
- GENERATION 层记录 tool_calls 数组：工具参数含敏感数据，与「工具入参不入 trace」的设计冲突（权威账本在 agent_tool_executions）——不做
- 改 events_core 的 200 字符截断：平台物化视图固定值，需重建 CH 表，高风险低收益——不做
- TOOL/SPAN 层成本定价：Langfuse 不支持工具层计价——不做
- DeepSeek 思考/回答 token 拆分：provider 不回传分桶——无法做

## 8. 复查用的 CH 查询配方（只读）

```bash
# 分类型字段填充率（务必用 events_full）
docker exec langfuse-clickhouse-1 clickhouse-client --query "
SELECT type, count() n,
  countIf(input!='') in_f, countIf(output!='') out_f,
  countIf(completion_start_time IS NOT NULL) ttft,
  countIf(NOT empty(usage_details)) usage
FROM default.events_full WHERE project_id='clawith' AND is_deleted=0
GROUP BY type ORDER BY n DESC"

# 物化视图截断定义（200 字符出处）
docker exec langfuse-clickhouse-1 clickhouse-client --query \
  "SELECT create_table_query FROM system.tables WHERE database='default' AND name='events_core_mv'"

# 空 output 是否纯工具轮
docker exec langfuse-clickhouse-1 clickhouse-client --query "
SELECT count() n, countIf(tc>0) with_tool FROM (
  SELECT g.span_id, count(t.span_id) tc FROM default.events_full g
  LEFT JOIN default.events_full t ON t.parent_span_id=g.span_id AND t.project_id='clawith' AND t.is_deleted=0 AND t.type='TOOL'
  WHERE g.project_id='clawith' AND g.is_deleted=0 AND g.type='GENERATION' AND g.output='' AND g.level='DEFAULT'
  GROUP BY g.span_id)"
```
