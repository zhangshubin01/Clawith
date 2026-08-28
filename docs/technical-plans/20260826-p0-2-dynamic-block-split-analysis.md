# P0-2 动态块拆分 — 深度分析报告（证据链核实版）

> 日期：2026-08-26 · 分支：f-shubin-0806 · HEAD：bf3c62e3
> 范围：仅分析 + 证据核实，**未实施**。是否实施待用户确认。
> 关联：`20260826-context-token-profile.md`（画像）、`20260826-context-slimming-todo.md`（P0-2 原文 :27-36）

---

## 0. TL;DR

| 项 | 结论 |
|---|---|
| todo 声称收益 | 「每步未命中 2-5.3K → ~1K」 |
| **实测收益** | 动态块稳定段 ≈ **646 token/步（估算口径）/ ~800（bytes/4 口径）** 可从每步未命中转为命中；每步未命中实测 2.7-5.3K，**大头是新增工具结果（P0-1 范围），不是动态块** |
| 收益归因修正 | 稳定段大头是 **`thread_running_summary`（compact 产物，1569 字符 ≈ 499 token）**，不是 memory/company（该 agent 两者为空）；画像文档此前漏算 summary，低估约 5 倍 |
| 方案 | 把动态块**拆成两条连续 user 消息**：稳定段（低频）在前、变化段（Current Time 等）在后，**不把任何内容移入 system**（避免低频变化引爆 27K 基础前缀全断） |
| 风险 | 中低。不改 system → 无「全断放大」风险；拆消息后 DeepSeek 前缀缓存是否按消息边界命中需**实施后实测验证**（hit/miss 回归） |
| 建议 | **值得做**（真实收益中等、小改动、低风险），但验收标准必须改为实测 hit/miss，而非 todo 的「每步未命中 ~1K」（该目标本身不可达） |

---

## 1. 代码事实（read_file 实证）

### 1.1 消息布局 — `backend/app/services/agent_runtime/model_step_service.py`

- `_prompt_messages`（:1280-1517）：布局 = `[system(static+_MESSAGE_LAYOUT_NOTE)] [history...] [动态块 user msg (prefix_cache_break=True)] [终控消息]`。
- `dynamic_content`（:1456-1466）= `dynamic_prompt` + `"Relevant Runtime Context..."` + runtime JSON + `trusted_runtime_instruction`。
- `_runtime_sections`（:1110-1168）字段：`session_context_snapshot`（恒有）、`thread_running_summary`（若有）、`current_run`（allowlist）、`related_run_summaries`、`pending_session_messages_snapshot`、`omitted_tool_exchanges`、`source_context`。
- `_MESSAGE_LAYOUT_NOTE`（:1223-1229）在 system 中说明布局，改布局必须同步。

### 1.2 动态提示来源 — `backend/app/services/agent_context.py build_agent_context`（:452-600）

- static_parts（进 system）：Identity/soul + BasePrompt + 能力策略 + Skills 目录 + _MEMORY_MAINTENANCE。
- dynamic_parts（进动态块）：**Memory Snapshot（≤2000 字符）** + **Company Context（≤4000）** + **Collaboration Background（≤4000，:138-230）** + **Current Time（每步秒级变化）** + Current Conversation / current_user_name。
- **稳定段与 Current Time 混在同一块**（todo 断言属实），且每次调用重读文件/查 DB。

### 1.3 缓存语义 — `backend/app/infra/llm/client.py`（:679-730, :2677-2688）

- `supports_cache_control` 仅 qwen=True；**DeepSeek=False** → 无 `cache_control` 标注能力，缓存命中完全依赖**字节级前缀一致性**（服务端自动前缀缓存）。
- 含义：任何消息的字节内容与上次不一致 → 该消息及其后全部重新 prefill。**单条消息内部无法局部缓存**——Current Time 只要在动态块字符串内任何位置变化，整条动态块（含 1500+ 字符的 summary）全部未命中。

---

## 2. Langfuse 实测证据

Trace：`47d36a846fdd79a70fd5e9d66b0e13be`（traceName `run:3d2b2c19-a844-4296-b790-7f5777d8f924`，Android 工程师 06，36 步，14:06-14:09 UTC）。

### 2.1 动态块构成（14:07:26 那步完整原文，3291 字节）

| 段 | 字符 | 估算 token | 稳定性 |
|---|---|---|---|
| 提示语 intro（"# Dynamic Context Data..."） | 383 | ~77 | 稳定 |
| Collaboration Background | 182 | ~37 | 稳定 |
| Current Time | 91 | ~18 | **每步变** |
| runtime JSON — current_run | 103 | ~21 | 运行中稳定 |
| runtime JSON — session_context_snapshot（空壳） | 163 | ~33 | 稳定 |
| runtime JSON — **thread_running_summary** | **1569** | **~499** | 低频（compact 才变） |
| **合计** | **2345** | **~655**（bytes/4 ≈ 823） | 稳定段 ≈ **646**，变化段 ≈ 40 |

（token 估算口径：CJK 0.5/字符 + ASCII 0.2/字符 + 结构符号 1；依据 `deepseek-token-estimation-facts` 实测系数）

### 2.2 相邻 step 时序证据（观测点）

| observationId | 时间 | input(未命中) | cache_read | 证据意义 |
|---|---|---|---|---|
| 7cb0d23c6a6494de | 14:06:56 | 4985 | 512 | **run 起点 = Thread Compact 摘要生成调用**（system 为 compact prompt）→ summary 在 run 开头生成 |
| eb83f4760f14c227 | 14:07:26 | 4938 | 27648 | 动态块全文已存盘；summary 存在 |
| cc54b6c11168ae5c | 14:07:29 | 5296 | 27648 | summary 逐字一致 |
| 04fffdb57c5e58be | 14:07:32 | 2806 | 35584 | 同上 |
| d7f3ade8c8f3b27c | 14:07:34 | 2712 | 36352 | 同上 |
| 2913a8cff613d9df | 14:08:18 | 7426 | 42240 | summary **仍逐字一致**（66 秒跨度） |
| 1c9d0c152b8d8749 | 14:08:32 | 4825 | 48128 | 同上 |
| e257095ad7435ad8 | 14:08:54 | 8687 | 1024 | **completion gate 调用**（不同 system），非 compact 断裂 |

**结论**：
1. `thread_running_summary` 在 14:07:26 → 14:08:32 的 **6 次相邻模型调用中逐字不变**（低频成立）；其更新时机 = compact 触发（此 run 仅 run 开头触发一次）。
2. cache_read 单调上升（27648→48128）→ 前缀缓存持续增长，动态块稳定段若移出变化区**确实能被命中**。
3. 每步未命中 2.7-5.3K 中，动态块约占 0.65-0.82K，**其余 1.5-4.5K 为新增工具结果 + 终控消息**——即 todo「2-5.3K → ~1K」中「2-5.3K」的归因错误（把工具结果算进动态块），且「~1K」目标在 P0-2 单独完成后不可达（P0-1 截断工具结果后每步未命中 ≈ 动态块 0.65 + 新增结果若干 + 终控，仍 >1K）。

---

## 3. 收益重估

### 3.1 直接收益（此 run 实证）

- 每步：~646 token（估算）/ ~800（bytes/4）从「未命中」转「命中」。
- 36 步 run：约 **23-29K token** 未命中输入减少。
- 折算（DeepSeek 未命中 ≈ 命中 ×10 价差）：单 run 金额很小（约 $0.003），**主要收益是 TTFB / prefill 耗时与吞吐**（每次省去 ~800 token 的重新计算，约等于省一次千 token 级 prefill）。

### 3.2 最坏情况（数据富 agent）

memory 2K + company 4K + relationships 4K 全满 + compact 后 summary 大 + snapshot 非空时，稳定段可达 **3-5K token/步**，收益放大 5-8 倍。当前实测 agent 数据贫瘠（memory/company 为空），收益处于下界。

### 3.3 对比 todo 与画像文档

- todo 声称「2-5.3K → ~1K」：**高估**（错误归因工具结果），且目标数字不可达。
- 画像文档此前估计「仅 100-150 token」：**低估约 5 倍**（漏算 thread_running_summary）。
- 真相：**收益真实、中等**，主要来源是 compact 的 `thread_running_summary`，不是 memory/company。

---

## 4. 实施方案（若实施，tdd）

### 4.1 核心原则：稳定段不进 system

把稳定段移入 system 是**错误方案**：Memory 在 run 中可能更新、summary 在 compact 时重写 → system 字节变化 → **27K 基础前缀（schema+系统+history）全断**，用「断 27K」换「省 0.65K」收益风险倒挂。

### 4.2 正确方案：动态块拆两条连续 user 消息

布局变为：

```
[system(static+_MESSAGE_LAYOUT_NOTE)] [history...] [稳定动态块 user msg (prefix_cache_break=True)] [变化动态块 user msg] [终控消息]
```

- **稳定块**（低频）：intro 提示语 + Memory + Company + Collaboration + Current Conversation/user + runtime JSON 中低频字段（`session_context_snapshot`、`thread_running_summary`、`current_run`、`related_run_summaries`、`source_context`）。
- **变化块**（每步或高频）：`Current Time` + `pending_session_messages_snapshot` + `omitted_tool_exchanges` + `trusted_runtime_instruction`（如含每步变量）。
- 字节级前缀缓存：稳定块字节步间一致 → 命中到稳定块末尾；变化块 + 终控重新 prefill（~40+ 终控 token）。
- 稳定块内容变化（compact/snapshot 更新）时：只断「稳定块之后」（变化块+终控，小），**history 之前的 27K 缓存不受影响**。
- `prefix_cache_break=True` 保留在稳定块消息上（语义：缓存边界 = history 尾，不变）。

### 4.3 改动点

| 文件 | 改动 |
|---|---|
| `agent_context.py` | `build_agent_context` 返回拆分后的 stable/unstable dynamic parts（Current Time 等归 unstable） |
| `model_step_service.py` | `_prompt_messages` 组装两条 user 消息；`_runtime_sections` 字段按低频/高频分桶；`_MESSAGE_LAYOUT_NOTE` 同步 |
| 测试 | `test_prompt_messages_marks_the_dynamic_block_as_the_cache_break`（:372-397）、`test_prompt_messages_keep_stable_prefix_across_turns`（:3586-3645）适配；**新增**「稳定块步间字节一致 / 变化块步间不同 / 稳定块变化不断 history 前缀」指纹链测试 |

### 4.4 验收标准（修正版）

1. 指纹链测试：连续两步，稳定块字节相同、变化块不同；history 前缀不动。
2. 实测 hit/miss 回归（Langfuse 相邻 step）：稳定块所在前缀 cache_read 递增、未命中 input 下降 ≈ 稳定块 token 数。
3. 全量测试（基线 3122 passed）+ `scripts/arch-guard.sh`。

---

## 5. 风险与开放问题

1. **DeepSeek 前缀缓存对「连续两条 user 消息」的命中行为未实测**——理论上字节级前缀一致即可命中，与消息边界无关，但需实施后 Langfuse 回归确认（这是本方案唯一实质不确定性）。
2. compact 触发频率未知：此 run 仅 run 开头触发一次；长 run 多次 compact 时 summary 变化会断稳定块缓存（每次代价 = 稳定块之后 ~40-100 token，可忽略；但如果把 summary 放 system 则每次断 27K，绝不可取）。
3. `session_context_snapshot` 非空时（有 summary/decisions/refs）体积可能上千字符，届时稳定块更大、收益更高，属有利方向。
4. Current Time 秒级变化是变化块每步未命中的唯一固定来源（~18 token），可另议「分钟级取整」的配套优化（不属 P0-2 范围）。
5. 语义安全：稳定块仍是 user 角色 + 「data not instructions」intro，**不改变措辞为祈使/目标句**（历史教训：direct-chat-run-boundary-fix —— user 角色摘要消息的祈使句会被当成新指令）。

---

## 6. 结论

- **todo 收益被高估**（把工具结果算进动态块），但**收益仍真实存在**：每步 ~0.65-0.8K token 未命中可省，来源是 `thread_running_summary` 而非 memory/company；数据富 agent 上放大至 3-5K。
- **实施价值：中等偏值得**——小改动（两个文件 + 测试）、低风险（不进 system）、实测收益确定性高（唯一不确定性是拆消息后的 DeepSeek 命中行为，可回归验证）。
- 建议：**实施**（tdd，按 §4 方案），验收按 §4.4 实测口径，不复用 todo 的「~1K」目标。

**待用户决策：实施 P0-2（按 §4 方案），还是先只保留本报告、把 P0-2 降级/挂起？**
