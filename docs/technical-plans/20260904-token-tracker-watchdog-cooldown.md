# token_tracker watchdog 误报抑制方案（cache 低命中告警冷却限频）

- 日期：2026-09-04
- 状态：已实施（代码 + 测试落地，16 passed，ruff 通过）；待部署
- 分支：`f-shubin-0806`
- 影响面：`backend/app/services/token_tracker.py` 内 cache-health watchdog 告警（`logger.warning("[Token Cache] Low hit rate ...")`），仅 observability 层，不改变任何共享契约 / API / 持久化 / 模型可见输入。
- 关联文档：`docs/prompt-cache-prefix-research-2026-08-18.md`（前缀缓存行为）、`docs/analysis/`（DeepSeek 前缀缓存逐出）。

---

## 1. 问题陈述

`record_token_usage` 里的 watchdog 用「单步 cache-miss ratio ≥ 50%」判断「前缀被破坏」，从而 `logger.warning`。24h 全量 10 条告警**全部来自同一 agent**（`Android 工程师 07`，`950a1943-…`），且全部是 Android 构建长任务的正常现象：

- **压缩点 miss ~95-100%**：`node:compact` span + `input_length` 骤降（122779→9247 等）+ `cache_read` 归零/骤降（35200→0 等）。压缩把历史改写为摘要，前缀必然失效——这是缓存命中率骤降的**预期**结果。
- **DeepSeek 前缀缓存逐出 miss 50-66%**：`cache_read` 单步骤降（21120→9472 等）后恢复。

模式是「锯齿波峰」：压缩冲 ~100%、逐出冲 50-66%、波谷回落 30-48%。watchdog 的 50% 阈值恰好切在波峰上 → 每个波峰告警一次。告警频率高到运维脱敏，真告警（schema 重排 / prompt 编辑导致前缀持续破坏）反而被淹没。

## 2. 根因

watchdog 把**观测指标误用为告警触发**：单步 miss_ratio 是观测层信号（Langfuse `usage_details` + `DailyTokenUsage` 已完整承载），不是「前缀被破坏」的证据——前缀破坏应是**持续**低命中，单步尖峰是压缩/逐出的正常态。

## 3. 方案（方向 A：冷却限频）

给 watchdog 告警加 **per-agent 30min 冷却窗口**：同一 agent 在窗口内只告警一次，窗口过后若仍低命中则再告警（可持续、每窗口一次）。这是根治而非降噪权宜：

- 误报危害是「高频 → 运维脱敏」，冷却直接切断这条脱敏链。
- 告警本就是「一次性人工排查型」：真假告警的处置动作相同（去看 schema 稳定性），低频假告警无害。

实现（`token_tracker.py`）：

- `LOW_HIT_WARNING_COOLDOWN_SECONDS = 30 * 60.0`（决策值，无外部背书）
- `_MAX_LOW_HIT_WARNING_ENTRIES = 1024` 进程内 LRU（`OrderedDict`，对齐 `list_dedup.py` / `agent_tools_cache.py` 既有模式）
- `_low_hit_warning_due()` / `_evict_low_hit_lru_if_needed()` / `clear_low_hit_warning_cooldown()`（测试钩子）
- `_maybe_warn_low_hit()`：阈值 **miss ≥ 1024 且 ratio ≥ 0.5 不动**（1024 排除极小请求 0% 命中的噪声，deepseek-harness UI 佐证）；冷却通过才告警；**日志原文不变**。

## 4. 否决的备选

- **B 连续 N 步判据**：过度设计，且锯齿波峰存在连续两步高 miss（53%→60%），会误报。
- **C 降级观测**：改变产品语义。观测层（Langfuse + DailyTokenUsage）已完备，watchdog 是叠加其上的限频告警层。

## 5. 验证

- `pytest tests/test_token_tracker.py`：16 passed（8 原有 + 8 新增）。
- 新增 8 测试覆盖：首次告警 / 冷却内抑制 / 冷却过期再告警（monkeypatch 常量=0.0）/ 不同 agent 独立 / 低 miss 不告警 / 低 ratio 不告警 / clear 钩子复位 / 冷却 dict 有界。
- `ruff check`（0.15.22）通过。
