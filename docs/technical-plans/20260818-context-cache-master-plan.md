# 上下文缓存成本修复总文档（唯一入口）

日期：2026-08-18/19 ｜ 分支：f-shubin-0806 ｜ 生产：OrbStack `clawith-agent`

> 本文件是「上下文成本优化」系列工作的唯一入口。所有状态、待办、结论以本文件为准；
> 其余文档为过程资料（见文末附录索引）。

---

## 1. 问题一句话

Durable runtime 每轮重发 `system + 动态上下文 + 全量历史`，动态内容插在历史**之前**，导致 DeepSeek 前缀缓存从第 2 条消息处断裂——**历史消息每轮全量 miss（miss 价 = 命中价 ×30）**。长任务实测：input 45k 时命中恒 11.6k、33k 历史全 miss、告警 ratio 71-100%。

## 2. 根因（已三方官方资料证实）

- DeepSeek 缓存只匹配「从开头起算的前缀单元」；前缀中任何一处每轮变化 → 其后全部脱离缓存（无断点标记可救）
- 唯一杠杆 = **消息顺序**：稳定内容在前、每轮变化的内容放最后

## 3. 已完成（均已部署生产）

| 项 | 提交 | 内容 | 效果 |
|---|---|---|---|
| A. 缓存 miss 记账 + 告警 | `d7e7e48b`（L1） | 解析 `prompt_cache_hit/miss_tokens` 落库（agents + daily_token_usage，迁移 f064）；miss≥50% 告警；工具列表确定性排序 | 缓存健康从盲区变可见，**本文件所有结论的数据来源** |
| B. 消息布局重排（设计 v2） | `ff594acd` | 新序列：`[system 静态] [历史 append] [动态块] [尾控制消息]`；runtime_instruction 移入动态块尾部；尾控制消息（当前输入/repair 指令）保持最后 | A/B：miss 占比 24.2%→12.4%（短会话接近消除）；长任务后期仍有残留问题 → 由 R1 根治 |
| C. R1 滑动窗口根治 | `4bfb34bf` | compact-first gate：裁剪前查 80% 水位，超限路由 Thread Compact（`compact_guard` 防死循环） | 长任务 input 96k→6.6k 实测生效，滑窗不再短路摘要机制 |
| D. R2 确认态出 system | `e85184ba` | 确认文案从 static_prompt 移入动态块（`_prompt_messages` 新参数 `extra_instruction`） | system 前缀绝对静态，确认轮不再全量 miss |
| E. R4-a qwen 缓存边界 | `0e6afedc` | `cache_control` 标记从尾控制消息改打历史尾部（`prefix_cache_break` 标记动态块起点） | DashScope 缓存区不再包含每轮变化的动态块（生产未用 qwen，防御性修复） |

**实测（08-19）**：修复链全部上线后平台日 miss 占比 **8.0%**（部署前 24.2%，重排后 12.4%）；长任务 96k→6.6k input 样本验证 R1 生效；`Low hit rate` 告警 0 条。

## 4. 遗留项（按优先级）

### R1 [高] 滑动窗口硬上限 —— 长任务收益的杀手

- **现象**：长任务后期 input 67k、miss 59k、命中仅 8k（08-19 02:11 实测）
- **机制**（已定位）：`context_builder.build` 每次模型调用前用 `select_recent_blocks(token_budget=effective_runtime_budget)` **从头裁剪**历史。历史超预算后，裁剪边界每轮随新增消息前移 → 前缀每轮变 → 命中归零。且裁剪发生在 model step，**先于** `_schedule_compact` 的 80% 水位检查（compact 在 tool 执行后），裁剪后 input 回落到预算内 → 水位永不触达 → **compact 摘要机制被滑窗永久短路**
- **修复设计（已定稿）**：compact 前置 + 裁剪兜底防循环
  1. `ModelIntent` 加 `"compact"`（node_executor.py:35）
  2. model step 在二次 build（裁剪）**之前**用第一次 build（无裁剪）估算历史 token；`history_tokens >= budget.compact_threshold` 且 guard 未置位 → 返回 `intent="compact"`（不发模型调用）
  3. node_executor：`intent == "compact"` → `next_route="compact"` + `lifecycle.compact_guard=True`；compact 节点若实际摘要（compacted=True）清 guard，否则保留 guard
  4. guard 置位时 model step 跳过 compact 请求、走原裁剪兜底（防「摘要后仍超预算」死循环，如单条巨型工具结果）
  5. `RuntimeLifecycle` 加 `compact_guard: NotRequired[bool]`；复用 `reaches_compact_high_watermark`（run_compactor.py:114）
  6. compact 请求不消耗 model_step_count（路由绕过 model step 计数）
- **状态**：✅ 已部署（`4bfb34bf`）。实测长任务 input 96k→6.6k；`compact_guard` 置位时回退裁剪兜底，无死循环报告

### R2 [中] 确认态污染 system 前缀

- **机制**：`_prepare_messages`（model_step_service.py ~1343）`requires_confirmation` 时把确认文案拼进 `static_prompt` → 该轮 system 前缀变化，全量 miss 一次（含 system+tools）
- **影响**：低频（仅工具结果不确定时），单轮成本 ~$0.02
- **修复方向**：确认文案移入动态块或尾控制消息（与重排同思路），system 保持绝对静态
- **状态**：✅ 已部署（`e85184ba`）。确认文案经 `_prompt_messages(extra_instruction=...)` 进入动态块，system 绝对静态

### R3 [低] 动态工具注入与缓存互斥（原 L3-B 方案作废依据）

- **结论**：每轮按需注入工具子集（L3-B）与 DeepSeek 前缀缓存**根本互斥**——工具定义在 token 流最前部，任何变动摧毁全部前缀单元
- **修正**：L3 只做「会话边界/r​​un 边界切换工具集」或纯静态挂载；每轮动态注入**放弃**
- **状态**：仅结论，未实施（L3 尚未启动）

### R4 [低] 杂项 follow-up

- ✅ qwen 路径 `cache_control` 标记错位 → 已修复（`0e6afedc`）：边界改打历史尾部
- ~~chat 路径（caller.py + context_compressor）消息布局与 durable 路径分叉~~ → **已核实不存在**：chat/durable 路径均经 `_prompt_messages`；`context_compressor` 的 `_multi_role_compress`/`_ctx_compress` 无生产调用方（仅 tool_trim 注释提及），此项关闭
- `model_step_service.py` 1984 行（C6 超限）→ 可选抽 `model_prompt.py`（低优先）
- `agent_tools.py` 24k+ 行（C6 最重违规）→ 拆分为 P2-c，待并行会话收尾窗口

## 5. 收益基线（重排 B + R1/R2 全链上线后的量化）

| 指标 | 部署前 | 重排后 | 全链上线后（08-19 实测） |
|---|---|---|---|
| 平台日 miss 占比 | 24.2% | 12.4% | **8.0%** |
| 长 run 每轮输入成本 | ~$0.0224 | ~$0.002-0.0026（R1 未触发时） | R1 触发时 input 96k→6.6k，成本同步骤降 |
| 缓存相关告警 | miss≥50% 频发 | — | `Low hit rate` 0 条 |

## 6. 下一步建议

1. **P2-c**：`agent_tools.py` 拆分（C6 最重违规，24k+ 行）——唯一高优先级遗留项；需并行会话收尾窗口 + 全量回归
2. **R4-c / P2-d**（低）：`model_step_service.py` 1984 行拆分；前端 57 个 >600 行文件（arch-guard 存量告警清单）
3. **观察项**（无需动手）：慢查询（08-19 复核健康）；compact 摘要 repair（等新 compact 样本）
4. 每项修复上线后按 L1 检查表核对 `prompt_cache_hit/miss_tokens`（本文件所有结论的数据来源）

---

## 附录：过程文档索引（不改、以本文件为准）

| 文档 | 内容 |
|---|---|
| `docs/technical-plans/20260818-context-cost-optimization-plan.md` | 初版方案（L1-L5 分级、量化；第 9 节含设计 v2 演进过程） |
| `docs/prompt-cache-prefix-research-2026-08-18.md` | 三方官方资料调研（DeepSeek/Anthropic/OpenAI 原文结论） |
| `docs/technical-plans/20260818-context-cache-hit-model-audit.md` | 缓存命中模型审核（原 V1-V5 编号出处，本文件 R1-R4 对应） |
| `docs/technical-plans/20260818-message-reorder-test-deploy-review.md` | 测试影响面与部署 A/B 审核 |
