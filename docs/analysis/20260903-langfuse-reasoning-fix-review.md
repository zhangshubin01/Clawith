# 20260903 评审：Langfuse reasoning/input 完整性修复（1224cf77）

评审对象：提交 `1224cf77` + 方案文档 `docs/technical-plans/20260903-langfuse-reasoning-input-completeness.md`
方法：代码重读 + 线上真实 trace（`a00e9d86006b0886e96ad9c295c60737`）+ Langfuse 项目配置 + 参考资料清单对照（reference-check 流程）。

## 逐问结论

### 1. 根因是否正确 ✅ 正确

- 埋点处只 `set_output(response.content)`，`reasoning_content` 被丢弃；且
  `extract_embedded_reasoning` 在埋点**之后**才执行，结构上埋点永远拿不到合并结果。
- 关键补充证据：DeepSeek 流式路径在 client 层已把 `<think>` 拆解合并进
  `chunk.reasoning_content`（`client.py:829-844`），即**埋点时刻 `response.reasoning_content`
  已有值**——缺口纯粹在 observability 层，无更深根因。
- 线上实证：5 条 generation output 纯文本无思考。

### 2. 根治方案是否正确 ✅ 正确（一处取舍已诚实记录）

- 结构化 `{"content", "reasoning_content"}` output 是 Langfuse 合法形态（output 任意 JSON）。
- 整树 64k 使 content 键也随之放宽——偏离原「content 仍 4k」表述，已回写方案文档并测试锁定；
  脱敏仍先行，无安全面扩大。
- 下游消费面核查（见第 5 问）无冲突。

### 3. 参考的资料是否正确 ✅ 已对照清单，定位准确

| 参考条目 | 发现 |
|---|---|
| Langfuse 官方 DeepSeek cookbook（T0） | 官方集成仅包装 OpenAI SDK，**不记录 reasoning 文本**——生态无此惯例，本修复是 Clawith 自有需求扩展，不与官方集成冲突 |
| gemini-cli（T1，本地源码） | `telemetry/loggers.ts:334` 只记 `type:'thought'` 的 **token 计数**——主流做法是思考只进 usage 维度（Clawith 已有） |
| litellm（T2，本地源码） | `reasoning_content` 仅用于 moonshot 协议回传，非可观测记录 |
| deepseek-harness（T1，本地源码） | thinking 只在 assembler/types 层（协议回传），无 trace 记录 |
| OTel GenAI 语义约定（gemini-cli 的 GEN_AI_* 属性用法） | 无 reasoning 标准属性槽 |

- 未覆盖项：OpenHands 本地镜像已只剩 UI 层（后端拆至 software-agent-sdk），其 LLM 埋点
  无法从本地源码完成对照——注明为覆盖缺口。
- 结论：方案无生态标准可抄（正常），也无生态冲突；usage 记 token + output 记文本双轨
  与业界（token 维度）+ 用户需求（文本）恰好互补。

### 4. 会引起其他问题吗 ⚠️ 两个已知项，均非阻塞

1. **体积**：真实 trace 实测 generation usage `output=6902 token`（DeepSeek 含思考），
   按实测 tokenizer 口径（中文 0.47-0.54 token/字符）最坏 ≈ 38k 字符 **< 64k 上限** ✅；
   input 全量记录后单条 input 约 10-50KB（mask 默认 4000/字符串兜底），Langfuse 自托管
   存储上升——方案已注明、可控。
2. **judge 边界（未来风险，已按用户确认配置防护）**：`run-goal-judge` 规则 filter 为
   `isRootObservation=true`（未限 type）。若未来出现无 run 包裹的独立 generation root，
   judge 的 `{{output}}` 会收到 JSON 而非纯文本。近 3 天数据中 GENERATION root = **0 条**，
   无实际触发。**已于 2026-09-02 配置**：filter 追加 `type=SPAN`（多条件 AND 语义已从
   langfuse worker 源码 `scheduleObservationEvals.ts:318` 确认「matches all filter conditions」），
   judge 现在只命中 run 根 span。

### 5. 会把其他逻辑搞坏吗 ✅ 不会（逐面核查）

- 协议链路：埋点处 `extract_embedded_reasoning` 是纯函数只读调用，不改 response；
  后续协议提取（`caller.py:714+`、`single_step.py:218+`）行为不变，重复计算幂等无害。
- 安全链路：`mask_text` 参数化默认值向后兼容；RunHandle/_observe_span 未动（4k 合同保留，
  既有截断测试不变）。脱敏→截断顺序不变。
- 评分链路：全部 3 个 evaluator 已逐一核查——
  `run-goal-judge`（吃根 span output，未改）；`tool-failure`/`tool-retry-exhausted`
  （filter `type=TOOL`，读 tool span output，未改）。
- 缓存链路：input 记录不影响实际请求 payload，DeepSeek 前缀缓存无涉。
- 多租户隔离：每租户 Langfuse client 未动。
- 验证实据：observability 45 passed、llm 相关 69 passed、ruff 全过、pyright 改动区零新
  错误、arch-guard P0 干净。

### 6. 这是根治的最佳方案吗 ✅ 是（备选均已排除）

- metadata 放截断版 reasoning：不满足「完整」；单独 thinking span：拆散 trace 语义，
  Langfuse UI 上思考与回复分离反而不便对读；output dict 保持 input/output 对称且可搜索。
- 一个可辩护的未选项：per-key 上限（reasoning 64k/content 4k）更精准，但要给 facade
  引入字段知识——当前整树 64k 是 ponytail 式取舍，天花板已注释，超限时单常量可调。

### 7. 修复方案是否多余 ❌ 不多余

缺口真实（线上 trace 实证 + 用户明确需求）。删 `capture_input` 死参数符合宪法
「Delete verified dead code」（实施前 grep 确认无他处使用）。

### 8. 是否已有可复用逻辑 ✅ 有，且已复用

`extract_embedded_reasoning`（client.py 既有）、`mask_text` 脱敏+截断（参数化复用）。
零新依赖、零新造轮子。

### 9. 会破坏 Clawith 特性吗 ✅ 不破坏

核心特性盘点均无涉：DeepSeek 前缀缓存、多租户 Langfuse 隔离、judge 链路（根 span 未动）、
前端思考流式显示（on_thinking 未动）、协议回传 reasoning（未动）、token 账本（未动）。
唯一行为变化 = trace 数据形态，即本次目标本身。

## 总评与后续

**结论：根因正确、方案根治、无破坏面、参考对照到位。可以部署。**

部署后验证（原方案待办 + 本评审新增）：
1. 抽查生产 trace：generation output 含完整 `reasoning_content`、两路径 input 非空。
2. （可选，推荐）Langfuse `run-goal-judge-sampled` 规则 filter 追加 `type=SPAN`，
   防未来独立 generation root 污染 judge 输入——一行配置，是否执行请拍板。
