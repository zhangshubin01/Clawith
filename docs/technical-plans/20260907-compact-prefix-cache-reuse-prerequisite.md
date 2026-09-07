# 「压缩请求复用主请求前缀 → 命中 DeepSeek 前缀缓存」前提立项

- 日期：2026-09-07
- 状态：前提验证完成，**成立**（有双参考项目实证 + 官方文档背书），进入 F2 实施
- 触发：F3（中文 token 估算）被负向探针证伪「方向反了」后，用户要求 F2 的「前缀缓存复用」前提**单独立项核实**，不重蹈「照文档实施才发现前提错」的覆辙。
- 关联：`docs/technical-plans/20260829-compaction-production-fix.md` §4（F2 设计）；记忆 [[deepseek-cache-tool-schema-facts]]、[[deepseek-token-estimation-facts]]、[[reference-projects]]。

---

## 1. 前提陈述（要验证的假设链）

F2 声称的收益是「压缩调用 cache_read_tokens 从 256（全 miss）→ >0」。这依赖一条三层假设链，任一层不成立则 F2 落空：

- **P1（机制存在）**：DeepSeek 对「连续请求的共享前缀」提供可命中的磁盘缓存，且命中语义是「前缀 token 序列完全匹配已持久化的缓存单元」。
- **P2（前缀构成）**：缓存前缀由 `system + tools + messages` 共同构成——**tools 参与缓存形状**，故压缩请求必须复用主请求的 system 与 tools（不能 `tools=[]`），否则前缀不匹配。
- **P3（时序与边界）**：压缩请求在主请求「秒级间隔」内发出（< 缓存 TTL），且 covered 前缀能落到某个已持久化的缓存单元上（不要求 100% 命中，只要求「显著优于现状 0%」）。

## 2. 验证证据

### P1 机制存在 —— DeepSeek 官方文档（`api-docs.deepseek.com/guides/kv_cache`）

- Context Caching on Disk「enabled by default for all users」。
- 命中规则：「A subsequent request can only hit the cache if it **fully matches** a cache prefix unit」（完全匹配一个已持久化单元）。
- 三种持久化时机：①请求边界（用户输入末尾 + 模型输出末尾各一单元）②公共前缀检测（多请求共享前缀→持久化为独立单元）③固定 token 间隔（长输入按间隔切分）。
- 附加说明：「best-effort，不保证 100%」「缓存不再使用后自动清除，通常 hours to days」。

### P2 前缀构成（tools 参与缓存）—— 双参考项目实证

**deepseek-harness** `packages/compaction/compaction-basic/src/summarizer.ts`：
- `SummarizationInput` 显式含 `system` / `tools` / `messages` 三字段，注释原文：「Reproducing the last routed request's **system prompt, tools, and leading messages** verbatim lets the auxiliary call reuse the provider's warm prefix cache」。
- `summarizeWithLlm` 调用时 `{ system: input.system, tools: [...input.tools], messages: [...input.messages, 指令] }`——**指令作为最后一条 user 消息追加，system/tools/消息前缀逐字节重放**（与 Clawith F1 已落地的「指令移出 system」完全同构）。
- 这正是 Clawith 现状的反面：Clawith 压缩请求 `_prompt_messages`（run_compactor.py:993）= `[user(JSON payload), user(指令)]`、`tools=[]`（`_compact_batch:1148`）、`supports_vision=False` → 与主请求零共享前缀 → cache_read 全 256。

**DeepSeek-Reasonix** `internal/agent/cache_shape.go` + `internal/config/cache_policy.go`：
- `PrefixShape` 结构 = `SystemHash + ToolsHash + PrefixHash(sha256{"system","tools"})`，注释「hashes the portions of the request prefix that influence provider-side prompt-cache reuse」→ **system 与 tools 都是缓存形状的组成部分**。
- `DefaultCacheTTL`：DeepSeek「Context Caching on Disk retains prefixes for several hours to days」→ 保守取 **24h**（`cache_policy.go:34`），并注明「too small burns a live cache (measured ~4x miss cost)」。

### P3 时序与边界 —— 官方文档 + 文档 §4.4 既有决议

- **时序**：压缩在「主请求发出后、检测到 80% 水位」的同 run 内触发，间隔秒级 ≪ TTL（hours-days）→ 缓存必然仍在。✓
- **边界对齐**：covered（compactable prefix 的 history 前半）**不保证**恰好落在某个已持久化单元的边界上——这是命中率非 100% 的根因。官方「固定 token 间隔」「公共前缀检测」会提高命中概率，但非保证。
- **截断场景**：文档 §4.4 已实锤——主请求 history 来自「带预算截断的第二次 build」，covered 来自「无截断投影」，截断时 covered 旧消息不在最近主请求里 → miss 不可避免（罕见边缘场景，已接受）。

## 3. 逐层裁决

| 层 | 裁决 | 依据 |
|---|---|---|
| P1 机制存在 | ✅ 成立 | 官方文档默认开启 + 命中规则 |
| P2 前缀构成（tools 参与） | ✅ 成立 | dsh 逐字节重放 system+tools+消息 + Reasonix PrefixShape 双实证 |
| P3 时序 | ✅ 成立 | 秒级 ≪ 24h TTL |
| P3 边界对齐 | ⚠️ 部分成立 | covered 非 100% 命中；但「复用主请求前缀」把命中率从 0% 提到「大概率/部分命中」，净收益确定 |

## 4. 结论与对 F2 实施的指导

1. **前提成立，F2 方向正确，可实施。** 核心机制（复用 system+tools+history 前缀）被 deepseek-harness 与 DeepSeek-Reasonix 两个独立项目实证，且 dsh 的「指令作最后 user 消息」与 Clawith F1 已落地形态同构。
2. **tools 对齐是 F2 的硬前提**（非可选优化）：Reasonix 证实 tools 参与缓存形状，故 F2 的 `_completion` 必须 `tools=list(shape.provider_tools)` 与主请求一致，`supports_vision=model.supports_vision`（supports_vision 经 tools 构造间接参与，见文档 F-A）。文档 §4.2-B 的 F-A 缺陷（`compact_inputs:2375` 缺 `onboarding_run` 条件）必须补齐，否则 onboarding 场景 tools 多 `user_wait` 工具 → 必 miss。
3. **验收口径维持文档 §4.4 决议不变**：`cache_read_tokens > 0` 降级为**集成监控指标**（同 run 秒级间隔内观测），不作 invariant 硬断言——covered 边界对齐 + 截断场景会导致正常 miss。invariant 测试只守「管线一致性」（`_build_history_messages` 两次调用逐字节一致），这是「缓存能命中」的必要条件门禁。
4. **`thinking_disabled=True` 保留**（0c43ce61）：官方「缓存只匹配前缀部分，输出仍计算」，输出侧参数（thinking/max_tokens）不影响输入前缀缓存，无需为缓存对齐而改动。

## 5. 负向探针记录（本立项的自我反证）

- **对立假设**：「tools 不参与缓存，F2 无需对齐 tools 就能命中」——被 Reasonix `PrefixShape.ToolsHash` 与 dsh `tools: [...input.tools]` 双实证推翻：两者都把 tools 列为缓存前缀对齐的必要组成。
- **删除测试**：「若不做 F2，根因（cache_read 全 256）会否复发」——会：Clawith 压缩请求 JSON payload + tools=[] 与主请求零共享前缀，结构上必 miss，run a4b1a018 已实锤。
- **边界诚实标注**：F2 是「提升命中率」不是「保证 100% 命中」，covered 边界对齐与截断场景 miss 已显式列为已知限制，不作不实承诺。
