# Clawith Agent 省 Token + 高完成率研究方案

> 调研日期：2026-08-20。目标：在既有基础上，找出「每 token 性价比最高、任务完成率最高」的剩余优化点，并给出可落地、可验证的优先级排序。
>
> 所有「已核实」结论均出自代码/已有文档/真实实测，标注文件行号或实验数据；「待验证」为假设，须用真实数据 A/B 后再落地。
> **落地记录**：本文首版提出 P0-2「剥离 reasoning 回放」与「工具 schema 是 cache-miss」两个判断，经对照 DeepSeek 官方文档 + 真实缓存实验后**均被证伪**（详见 §3 内嵌纠正）。最终落地项 = 下线 GitLab MCP（§5 第 1 步，已完成并提交 `b2a3cf66`）。

---

## 0. 结论摘要（TL;DR）

Clawith 的 agent 运行时在 **任务完成率** 侧已经相当成熟（循环熔断、多 run 隔离、路径契约、failover/retry、双级压缩都已落地）。**剩余的最大杠杆几乎全在「每轮重发的固定 token 成本」**，按「省 token × 完成率」双轴排序：

| 优先级 | 优化点 | 省 token | 完成率 | 状态 |
|---|---|---|---|---|
| **P0-1** | 工具 schema 冗余（179 工具 50-66K，只用 17 个） | ★★ | ★★★ | 实测：**cache-HIT 非 miss**，真成本=吃窗口+分心 |
| ~~P0-2~~ | ~~剥离 reasoning 回放~~ | — | — | **已证伪：DeepSeek 工具调用协议要求回传，否则 400** |
| **P0-3** | reasoning_effort 调低（默认 high→可配 low） | ★（实测降级） | ★ | 实测：仅省 ~79 prompt tok/次，reasoning 长度任务驱动 |
| **P1-1** | 压缩模型分级（direct chat 用贵模型做摘要） | ★★ | ★ | 已核实 |
| **P1-2** | token 估算口径不一致（chars/3 vs /4 vs bytes/4） | ★ | ★ | 已核实 |
| **P2-1** | 工具描述精简（description 更短更准） | ★★ | ★★ | 建议 |
| **P2-2** | 压缩阈值 A/B（80% 触发 / 50% 低水位） | ★ | ★ | 建议 |

**一句话结论**：reasoning（占历史 70~93%）是**结构性必需成本**——回放是 DeepSeek 工具调用协议要求（不可剥离）、生成量任务驱动（`reasoning_effort` 控制力弱）。真正可动、且「省 token × 完成率」双赢的是 **工具 schema 治理**：重型 agent 挂 179 个工具却只用 17 个，其 GitLab MCP 116 个工具已在本轮下线（两个 Android 工程师 agent 工具 175/214 → 59/98，schema ~66/73K → ~13/21K tokens）。

---

## 1. Token 成本模型（每轮模型调用去向）

单次模型调用输入 = 以下五段按序拼接（`model_step_service._prompt_messages` + `complete_llm_once` → `client.stream(messages=..., tools=...)`）：

```
[system 静态]  [history 稳定 append]  [dynamic 块(每轮变)]  [最终控制消息]  [tools 数组(请求尾部)]
```

| 段 | 内容 | 稳定性 | 是否进 DeepSeek 前缀缓存 |
|---|---|---|---|
| system | identity + soul + base prompt + capability policies + skills catalog + output | 字节级稳定 | ✅ 命中 |
| history | session recent 20 条 + thread 消息（token 预算约束） | append-only | ✅ 命中（到 dynamic 块为止） |
| dynamic 块 | runtime JSON + runtime instruction + 当前时间 | 每轮变 | ❌ 断点 |
| 最终控制消息 | 用户当前输入 / 续接文案 | 每轮变 | ❌ |
| **tools** | **per-agent 59–214 工具完整 JSON schema（10–73K tok）** | **字节级稳定** | **✅ 命中（独立缓存，动态块不切断）** |

### 关键量级（已实测）

- `builtin_model_definitions()` 共 **131** 个工具，schema JSON 总量 **91,120 chars ≈ 23–30K tokens**。
- per-agent 经 `get_runtime_agent_tools_for_llm` 实测：轻型 agent **59 工具 ≈ 10–13K tok**，重型 agent（含 MCP）**179–214 工具 ≈ 50–73K tok**（详见 §3 P0-1）。
- 早先「tools 位于请求尾部→永远 miss」的判断被**实测证伪**：2 轮缓存对照实验 T2 `hit=7040, miss=111`，工具全命中缓存（DeepSeek 固定间隔切块/共同前缀检测能独立缓存工具，中间 dynamic 块不切断）。工具的真实成本是「吃 context window 预算 + 模型分心 + cache-hit 折扣价」，见 §3 P0-1。

---

## 2. 已落地的地基（不重复推荐，避免重复造轮子）

- **缓存友好消息布局**（`ff594acd`）：history 前置 + 动态块后置 + 末尾控制消息；`prefix_cache_break=True` 标记缓存边界。`model_step_service.py:958-1146`。
- **compact-first gate**（`4bfb34bf` / `R1`）：未压缩历史先触发 Thread Compact，避免每轮截断改写缓存前缀。`model_step_service.py:1855-1871`。
- **前缀缓存调研**（`docs/prompt-cache-prefix-research-2026-08-18.md`）：DeepSeek/Anthropic/OpenAI 三家一手资料，结论=动态内容放末尾。
- **工具结果 Layer0 压缩**（`tool_trim.py`）：按 token 预算、类型感知压缩工具返回，`never_worse` 回退。
- **循环熔断三件套**：config 失败熔断（`2af11755`）、相同成功循环熔断（`d3674da1`/`b31f51ec`/`d9eb094e`）、3 连软提醒（`5b85ca7f`）。
- **多 run 共 thread 隔离**（`b4a18ba6`/`0010726c`）：`bound_current_run_window` 折叠上一 run。
- **双级压缩**：run 级 `run_compactor`（80% 水位触发、50% 低水位、摘要上限 min(4096, quarter)）；会话级 `session_context_compactor`。
- **工具 schema 解析缓存**（`agent_tools_cache.py`）：30s TTL 只省「解析」不省「发送」，与本文 P0-1 不冲突（它解决的是 CPU/DB，不是 token）。

---

## 3. 剩余高杠杆机会（排序）

### P0-1 工具 schema 冗余（省 token ★★ / 完成率 ★★★，实测后重新定性）

**2026-08-20 实测结论（容器内直调，真实 agent + 缓存对照实验）**：

1. **真实规模远超预估**：`get_runtime_agent_tools_for_llm` 实测——轻型 agent **59 工具 ≈ 10–13K tok**，重型 agent（Android 工程师 4 `27d55a64`）**179 工具 ≈ 50–66K tok**（62bc9c81 达 214 工具）。早先「~63 工具 / 11–15K」是错的。
2. **90% 浪费**：`27d55a64` 08-19 共 672 次工具执行、**只用 17 个 distinct 工具**；08-20 只用 16 个。179 工具里 ~162 个从没用过。
3. **膨胀主因 = GitLab MCP 工具**：该 agent 的 `mcp_*` 工具 ~130 个，单个 schema 600–2100 tok（`mcp_create_draft_note`/`mcp_update_draft_note` 各 6.4K chars），合计 ~50K tok。
4. **【关键反转】工具 schema 是 cache-HIT 不是 miss**：2 轮缓存对照实验（30 工具 + 动态块在中间）——T2 `hit=7040, miss=111`，工具全命中缓存，miss 仅动态块。DeepSeek 的缓存（固定间隔切块/共同前缀检测）能**独立缓存工具**，动态块不切断。故早先「工具在尾部→cache-miss 全价」的假设是错的。

**重新定性后的真实成本（不再是「每轮全价重发」）**：

- **吃 context window 预算**：`runtime_budget` 已把 `tool_schema_tokens` 从有效预算里扣除（`model_step_service.py:1690/1850`）。50–66K 工具 ≈ 131K 窗口的 38–50%，**挤掉历史预算 → 压缩水位提前触发 → 摘要调用变多**。
- **模型分心（完成率 ↓）**：179 个工具里 162 个无关，模型选错工具的概率上升。
- **cache-hit 折扣价**：0.1x 非零，50–66K × 0.1x × 轮次仍是持续成本，但远小于全价。

**可选方向（按 ROI 排序）**：

1. **工具路由 / 按需注入（最大杠杆，主攻「分心 + 吃窗口」）**：按当前指令+run 状态只发相关工具子集（179→~20）。收益不再是「省全价 miss」，而是**释放 ~50K 窗口预算 + 大幅减少模型分心**；需 ADR + benchmark 验证路由准确率（漏发工具需 fallback）。
2. **工具 description 精简（零结构改动，先做）**：`mcp_*` 工具 description 冗余（300–1400 chars），压到「一句话+关键参数」可全局砍 20–40%。直接减小 schema 体积（吃窗口↓、分心↓），且更短描述让模型更少误读。
3. **per-agent 工具治理**：`27d55a64` 179 工具里 162 个从未用——最直接的是让 agent 所有者裁剪启用工具列表（产品侧，非代码侧）。

**验证方法**：同一 run 轨迹，对比「路由前 179 工具 vs 路由后 ~20 工具」的 `prompt_cache_hit_tokens` 下降 + 工具选择准确率 + 压缩触发次数。

---

### ~~P0-2 剥离 reasoning 回放~~（已证伪）+ P0-3 reasoning_effort 调低

> **⚠️ 重要纠正（2026-08-20，对照 DeepSeek 官方 Thinking Mode 文档 `api-docs.deepseek.com/guides/thinking_mode`）**：
> 原 P0-2「剥离 reasoning 回放」是**错误建议**。DeepSeek 官方明文：
> - **工具调用场景**（Clawith 每轮都在工具调用）：「for requests carrying the tools parameter, the reasoning_content **must be fully passed back to the API in all subsequent requests**. If your code does not correctly pass back reasoning_content, the API will return a **400 error**。」
> - **非工具调用场景**：「does not need to participate in the context concatenation. If passed, it will be ignored.」
> - 结论：Clawith 是工具调用型 agent，**回放 reasoning 是协议要求，剥离 = 400 错误 = 直接破坏平台**。当前 `make_message` → `to_openai_format` 的回放行为是**正确且必需**的，不是浪费。
> - 新杠杆转移为 **P0-3**：reasoning 的 token 成本主要在**生成侧**（output，全价）而非回放侧（cache-hit，折扣价），可通过 `reasoning_effort` 调低生成量。

**2026-08-20 实测结论（真实 checkpoint 导出，`runtime_messages_as_json`）**：

| thread | 消息数 | reasoning 字符 | **reasoning 占历史比例** |
|---|---|---|---|
| c28509bb | 27 | 35,034 | **69.6%** |
| f23045c7 | 48 | 94,846 | **77.9%** |
| b77fb2ef | 56 | 94,282 | **92.5%** |

- 模型队列为 `deepseek-v4-flash` / `deepseek-v4-pro`（无 deepseek-reasoner），但**两个 v4 模型都产出思维链**，每个 assistant 消息带 1.6K~6.9K 字符 reasoning（如 "Let me review the context for this heartbeat..."）。
- 早先 `checkpoint::text LIKE '%reasoning_content%'` 返回 0 是**假阴性**：checkpoint 用 LangGraph message 序列化，不含该字面字符串；真实导出后字段非空。
- reasoning 是 **cache-hit**（位于稳定历史前缀内），故直接 token 省钱是「折扣价」，但它的**间接成本更大**：历史膨胀 4~13 倍 → 压缩水位（80%）提前 4~13 倍触发 → 摘要调用（完整模型调用）频率暴涨；同时是 checkpoint 膨胀（631MB）的主要推手。

**问题（已核实代码路径）**：

1. `_assistant_message`（`model_step_service.py:1174-1175`）把 `step.reasoning_content` 持久化进 checkpoint；
2. `make_message`（`model_step_service.py:1020-1022`）在组装历史时把它原样回放；
3. `LLMMessage.to_openai_format`（`client.py:264-265`）把它序列化回请求。

**纠正后的结论**：

1. **回放是协议要求，不可剥离**：DeepSeek 工具调用场景要求 reasoning_content「完整回传」，剥离会导致 400 错误。`make_message` → `to_openai_format` 的回放是**必需**的。
2. **「回放旧思维链 re-cue 循环」的担忧已由既有机制兜底**：`_TURN_CONTINUATION_MESSAGE`（`R1` `09109cbf`）+ 循环熔断三件套已处理循环问题，reasoning 回放不构成新的循环风险源。
3. **reasoning 的 token 成本结构**：生成侧（output，全价）≫ 回放侧（cache-hit，折扣价）。真正能省的杠杆在生成侧，不在回放侧。

### P0-3 reasoning_effort 调低（省 token ★★★ → 实测降级为 ★ / 完成率 ★）

**问题（已核实）**：

- DeepSeek 官方：思考模式**默认开启、默认 effort = high**；effort 映射 `low→low`、`medium/high→high`、`max→max`。
- Clawith 代码**未设置** `reasoning_effort` 或 `thinking` 参数（`grep` 全库无 `reasoning_effort` 传参）→ 所有模型调用默认吃 **high** 档。

**2026-08-20 实测结论（deepseek-v4-pro，容器内 httpx 直调，11 个任务 × low/high）**：

1. **reasoning 长度主要由任务决定，effort 控制力弱**：均值 low ≈ 353 chars vs high ≈ 423 chars（high 略长 ~20%），但方差极大（scheduling：low 282–1272、high 379–1496；rope 低 1276 > 高 480、palindrome 低 778 < 高 1654——方向不一致）。
2. **完成率无实测差异**：11 个任务 low/high 全部答对（含难任务 zeros/monty/scheduling/coinflip）。早先「low 档答错 coinflip」是**判题 bug 假象**——答案 `\frac{1}{7}`（LaTeX）被 `"1/7" in content` 误判，复测 3 次全对。
3. **唯一干净信号**：`reasoning_effort=high` 每请求固定 +**79 prompt tokens**（34→113、44→123、57→136、85→164、40→119，精确 +79），疑为 high 档注入的思考 scaffolding。这是 high→low 唯一稳定可省的量，但量级极小（相对 reasoning 占历史 70~93% 的量级）。

**降级后的结论**：P0-3 从「★★★」降为「★」。reasoning 生成量是**任务驱动的结构成本**，`reasoning_effort` 旋钮对它的控制力远小于预期，省不了历史膨胀的主体。

**重要 caveat（必须说明）**：本测试是**单轮 Q&A**，非 agent 的多轮工具调用场景。Clawith 的 reasoning（1.6K~6.9K chars，关于「下一步调什么工具/计划」）可能对 effort 更敏感。**若要定论，需用真实 agent benchmark Case（skill `agent-evaluation`）跑 low vs high 对比完成率**，而非玩具任务。

**剩余可能的 reasoning 杠杆（未实测，另立项）**：`thinking: {"type": "disabled"}`（对心跳/健康检查/简单回显等无需推理的 turn 完全关闭思考模式）——比 effort 更彻底，但需验证关闭思考后工具调用协议是否仍正常（reasoning 不再产出，也就不存在回传问题）。

**风险（需验证）**：

- effort 调低会降推理质量，可能影响复杂任务完成率。需按任务类型分级：心跳/健康检查/简单查询走 `low`，复杂编码/规划走 `high`。
- 需实测 `low` 档的 reasoning 长度与任务完成率曲线，找「省 token vs 完成率」拐点。

**验证方法**：同一 benchmark Case，`reasoning_effort` 分别跑 low/high，对比 `output_tokens`（reasoning 占比）与任务完成率；用 `token_tracker` 的 `completion_tokens` 精确对比生成量。

---

### P1-1 压缩模型分级（省 token ★★ / 完成率 ★）

**问题（已核实）**：`session_context_compactor._resolve_models`（`session_context_compactor.py:282-338`）——group 会话用 `resolve_multi_agent_compact_model`（独立便宜模型），但 **direct chat 用 agent 自己的主模型**做摘要。

**建议**：direct chat 也允许配置一个独立的 compact 模型（对齐 group 的 `MULTI_AGENT_COMPACT_MODEL_ID` 模式），让摘要这种「无需强推理、只做归并」的任务走便宜/快模型。省的是「摘要调用本身」的 token 单价，不省调用次数。

**风险**：摘要质量下降会污染后续上下文（完成率 ↓）。需 benchmark 验证摘要模型与主模型的摘要质量差距。

---

### P1-2 token 估算口径不一致（省 token ★ / 完成率 ★）

**问题（已核实）**：三处 token 估算口径不一致：

- `model_step_service._estimate_tokens`：`chars_per_token=3`（`model_step_service.py:630-631`）；
- `run_compactor._estimate_tokens`：`chars_per_token=4, utf8_bytes=True`（`run_compactor.py:201-209`）；
- `session_context_compactor._estimate_tokens`：`len(bytes)/4`（`session_context_compactor.py:155-157`）。

CJK 文本下 chars/3 会显著低估（中文 1 字符 ≈ 1–2 token），ASCII 下又高估。估算与真实计费（`token_tracker.extract_token_usage` 已能拿到 `prompt_tokens` 精确值）存在系统性偏差，导致预算闸门（80% 触发、50% 低水位）与实际不符。

**建议**：统一估算口径（用真实 usage 反推的校准系数，或至少统一 chars/4+utf8），避免预算失准导致「过早压缩」（多烧摘要调用）或「过晚压缩」（历史溢出）。

---

### P2-1 工具描述精简（见 P0-1 方向 2，独立可做）

### P2-2 压缩阈值 A/B

当前 run 级压缩 80% 水位触发、50% 低水位（`run_compactor.reaches_compact_high_watermark` / `compact_context_budgets`）。压缩本身是一次额外模型调用（有 token + 延迟成本），阈值过高→历史膨胀、过低→摘要调用过频。建议用真实多轮轨迹 A/B 不同阈值，找「总 token（正文 + 摘要调用）」最低点。

---

## 4. 完成率侧小结（已有 vs 剩余）

**已落地（完成率的主要保障）**：

- 循环熔断（config 失败 / 相同成功 / 软提醒）→ 杜绝烧满轮次的空转；
- 多 run 共 thread 隔离 → 杜绝旧指令污染新任务；
- 路径契约 + 结构化路径诊断 → 杜绝「错误路径空转」；
- failover/retry（`6dc6f33c`）→ DNS/瞬时错误不再秒杀整 run；
- 压缩摘要 prompt 禁重复调用（`R7` `83728b27`）→ 摘要不再放大循环。

**剩余完成率机会**（与省 token 重合）：

1. **P0-1 工具精简 → 减少模型选错工具**：179 个工具只用 17 个，162 个无关工具是「选错工具」的诱因（GitLab MCP 已下线）。
2. **摘要质量分级（P1-1）的风险面**：省 token 不能以摘要质量换，需 benchmark 守住。

---

## 5. 建议的实施顺序（快赢优先 → 结构改造）

1. **✅ GitLab MCP 下线**（已完成 2026-08-20，提交 `b2a3cf66`）——两个 Android 工程师 agent 工具 175/214 → 59/98，schema ~66/73K → ~13/21K tokens，释放 ~50K 窗口预算。
2. **P0-1 方向 2 工具 description 精简**（零结构风险，独立 PR，可 A/B 量化）——剩余的 openviking MCP 等工具 description 仍冗余可压。
3. **P1-2 token 估算统一**（改动小，让预算闸门可信，是后面所有 A/B 的测量基础）。
4. **P1-1 压缩模型分级**（对齐 group 模式，需 benchmark 守质量）。
5. **P0-1 方向 1 按需工具路由**（结构性改动，收益最大但需 ADR + 多轮 benchmark，放最后）。

## 6. 验证工具

- 缓存命中：`usage.prompt_cache_hit_tokens / prompt_cache_miss_tokens`（DeepSeek），`token_tracker.extract_token_usage` 已解析。
- 真实线程状态：skill `clawith-graph-state-triage`（容器内 `AsyncPostgresSaver.aget_tuple` + `runtime_messages_as_json`）。
- 工具/摘要质量：skill `agent-evaluation` / benchmark Case 固化回归。
