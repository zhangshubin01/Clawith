# Clawith Token 优化 Backlog（按收益 × 风险排序）

> 2026-08-20。承接 `20260820-token-efficiency-completion-rate-research.md`（研究全文，含实测数据与证伪记录），本文件只列**剩余待办**的优先级排期。
> 排序原则：收益/风险性价比优先，其次按依赖关系（测量基础先于 A/B、守质量项先于结构改造）。

## 已完成（不再列）

- **GitLab MCP 下线**（提交 `b2a3cf66`）：重型 agent 175/214 → 59/98 工具，schema ~66/73K → ~13/21K tokens，释放 ~50K 窗口预算。
- **OpenViking 死重复 16 工具删除**（2026-08-20，纯 DB）：62bc9c81 129 → 113 工具，无容器/compose 改动。

## 已证伪（不做）

- **剥离 reasoning 回放**：DeepSeek 工具调用协议要求 `reasoning_content` 完整回传，否则 400；`make_message`→`to_openai_format` 的回放是必需行为。

---

## T1 立即做（低风险快赢，收益 ★★）

| # | 项 | 收益 | 风险 | 工作量 | 依赖 |
|---|---|---|---|---|---|
| 1 | 工具 description 精简 | ★★ | 低 | 小 | 无 |
| 2 | token 估算口径统一 | ★ | 低 | 小 | 无 |
| 3 | per-agent 工具治理（产品侧） | ★★ | 低 | 无代码 | 无 |

### 1. 工具 description 精简

- **量化**：description（25–27%）+ 参数描述（19–24%）= 44–51% 的 schema 体积。
- **三种冗余**：① 跨工具引导重复（「写文件前先 list_files/用子目录」在 write_file/list_files/move_file 重复 → 应搬 system prompt）；② 冗长散文（write_file 830 字符）；③ MCP「Args:」散文（是参数文档唯一载体，难安全大砍，放 Phase 2 谨慎处理）。
- **预期**：整体 schema 缩 15–25%（保守口径）。
- **风险边界（绝不删）**：6000 字符上限、offset/limit 分块、mode=append、read_file 不解析二进制用 read_document 等「防错坑位」。
- **分阶段**：Phase 1 内置工具（改 `agent_tools.py`）→ Phase 2 MCP 工具 → A/B 验证工具选择准确率。

### 2. token 估算口径统一

- **问题**：三处口径不一致——`_estimate_tokens`（`chars/3`）、`run_compactor._estimate_tokens`（`chars/4, utf8_bytes`）、`session_context_compactor._estimate_tokens`（`bytes/4`）。CJK 下 chars/3 显著低估、ASCII 下高估。
- **价值**：让预算闸门（80% 触发 / 50% 低水位）可信，是后续所有 A/B（P4）的**测量基础**。
- **方向**：用 `token_tracker.extract_token_usage` 的真实 usage 反推校准系数，或至少统一 chars/4+utf8。

### 3. per-agent 工具治理（产品侧，非代码）

- 重型 agent 179 工具里 ~162 个从未用（90% 浪费）。最直接的动作是让 agent 所有者在工具面板裁剪启用列表。
- 无代码改动，收益即时；需用户/租户操作。

---

## T2 短期（中收益，需 benchmark 守质量）

| # | 项 | 收益 | 风险 | 依赖 |
|---|---|---|---|---|
| 4 | 压缩模型分级 | ★★ | 中 | 质量 benchmark |
| 5 | 压缩阈值 A/B | ★ | 低 | #2 |

### 4. 压缩模型分级（direct chat）

- direct chat 摘要用 agent 主模型，group 已用独立便宜模型（`resolve_multi_agent_compact_model`）。对齐 group 模式，让摘要走便宜/快模型。
- 风险：摘要质量下降污染后续上下文 → 完成率 ↓，必须 benchmark 守住。

### 5. 压缩阈值 A/B

- 当前 80% 触发 / 50% 低水位。找「总 token（正文 + 摘要调用）」最低点。
- 依赖 #2 统一估算后测量才可信。

---

## T3 中期（最大收益，最高风险，需 ADR + 多轮 benchmark）

| # | 项 | 收益 | 风险 |
|---|---|---|---|
| 6 | 按需工具路由 | ★★★ | 高 |

### 6. 按需工具路由

- 主攻「分心 + 吃窗口」：按当前指令 + run 状态只发相关工具子集（179 → ~20），释放 ~50K 窗口预算、大幅减少模型分心。
- 结构性改动：需 ADR + benchmark 验证路由准确率；漏发工具需 fallback。
- 放最后——先靠 T1 的 schema 精简 + 工具治理吃到大部分收益。

---

## T4 待验证（收益存疑 / 未实测，先立项后落地）

| # | 项 | 收益 | 风险 | 前置验证 |
|---|---|---|---|---|
| 7 | reasoning_effort 分级 | ★（实测降级） | 中 | 真实 benchmark Case |
| 8 | thinking:disabled | 未实测 | 中 | 工具调用协议是否正常 |

### 7. reasoning_effort 按任务分级

- 实测：reasoning 长度任务驱动，effort 控制力弱；唯一稳定可省 = high 档固定 +79 prompt tokens/次（量级极小）。
- 若要定论，需用真实 agent benchmark Case（skill `agent-evaluation`）跑 low vs high 对比完成率，而非玩具任务。
- 结论：低优先级，除非 #8 先证伪。

### 8. thinking:disabled（心跳/健康检查/简单回显）

- 比 effort 更彻底：完全关闭思考。需先验证关闭思考后工具调用协议是否仍正常（reasoning 不再产出，也不存在回传问题）。
- 未实测，先立项。

---

## 验证工具速查

- 缓存命中：`usage.prompt_cache_hit_tokens / prompt_cache_miss_tokens`（DeepSeek），`token_tracker.extract_token_usage`。
- 真实线程状态：skill `clawith-graph-state-triage`（`AsyncPostgresSaver.aget_tuple` + `runtime_messages_as_json`）。
- 工具/摘要质量回归：skill `agent-evaluation` / benchmark Case 固化。
