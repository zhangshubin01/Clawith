# 3.1 立项设计：每步快照重注入与缓存分层修复

日期：2026-09-02
状态：**决策点已拍板（2026-09-02），并按代码级核对完成实现前修正（§8.1），可进入实现**
上游：docs/analysis/2026-09-01-task-step-inflation.md §四.5/§五.3.1（「memory/reflections 快照增量注入 + 重评估 prefix_cache_break」，收益最大风险最高的结构性改造）
关联：docs/technical-plans/20260826-context-slimming-best-practices.md（缓存边界一手调研）；DeepSeek-Reasonix（`~/Documents/UGit/DeepSeek-Reasonix`，参考资料池 T3 明标与本问题直接相关——其「Cache-aware context maintenance：startup 注入小体积稳定环境摘要、工具 schema 合同文档化防回归」与本方案 A1 移前 + 契约文档同步同构，互为印证）

## 1. 问题定位（全部代码级证据，2026-09-02 复核）

每步消息布局（model_step_service.py `_prompt_messages` 1300 起，docstring 1308-1332，A 块实现 1484-1504；另有一处同路调用 run_compactor.py:636，见 §3.2 兼容性）：

```text
[system: static_prompt] [history] [A: dynamic_prompt + runtime JSON + 可信指令] [B: turn-local] [final control]
```

- A 块被打 `prefix_cache_break=True`（model_step_service.py:1502），client.py:724-727 把缓存边界钉在 A 前一条（history 尾）——**A 块整体永远不进缓存、每步全价重发**。这是刻意设计（A 内含 per-step 变化的 state snapshot，不能污染稳定前缀）。
- A 块内部是两类东西混装：`dynamic_prompt`（memory 2K + reflections 2K + user_profile 2K + focus 1.5K + experience + company + relationships + current_user_name，agent_context.py:656-742 装配）与 trusted runtime instruction（`# Current Runtime Instruction`）run 内稳定；`_runtime_sections(build)` JSON（state snapshot、running summary、current run、related runs、source context）每步都变。**稳定快照被每步变化的状态 JSON 拖累一起全额重发。**
- `build_agent_context` 每步被调（model_step_service.py:2212-2219，prompt_builder 默认值 :1939；另有一处单次调用路径 llm/caller.py:549）→ 每步重读 memory/reflections/skills/focus/experience 文件 + DB 查询，既有 token 成本又有装配 I/O。
- 09-01 实测：单步 input 8K→19K tokens、cache 命中率 0-46%、`[Token Cache] Low hit rate` 高频；最坏案例 124 步 LLM 调用 $1.15。

## 2. 方案决策点

| # | 决策 | 推荐 | 理由 |
|---|---|---|---|
| D1 | 消息重排 | **A 拆 A1/A2，A1 移到 history 之前**：`[system][A1][history][A2][B][final]` | 缓存前缀=system+A1+history 才可能命中 A1；A1 在 history 后则每步前缀不含 A1（历史每步增长），缓存永远 miss。Anthropic/OpenAI 官方均推荐「稳定参考内容放最前」。DeepSeek 走自动前缀缓存（`supports_cache_control=False`，client.py:685-686 零标记直发），**收益来自重排本身**，与 cache_control 标记无关 |
| D2 | A1/A2 划分 | 见 §2.1 映射表（已按代码核对修正） | 划分原则：run 内字节稳定的进 A1，per-step 变化的留 A2 |
| D3 | A1 字节稳定保障 | **per-run memoize**：build_agent_context 对 A1 数据段做 run 级缓存，失效指纹见 §3.1（已统一修正） | 无 memoize 则 A1 每次重建即使内容相同也要逐字节保证一致才能命中缓存，memoize 从构造上保证稳定 + 省每步文件/DB I/O |
| D4 | 缓存标记 | client.py 给 A1 的 text block 加 `cache_control: ephemeral`（现 system 与 history 尾已有两处标记，A1 为第三处） | 多标记点在 OpenAI 兼容协议与 Anthropic（上限 4）均支持；supports_cache_control=False 的 provider 走自动前缀缓存，重排后同样受益 |
| D5 | 灰度开关 | per-agent SystemSetting `context_snapshot_layering_{agent_id}` 缺省 off | 关=现状逐字节一致的回滚面（两条调用路径都保证，见 §3.1） |

### 2.1 A1/A2 划分映射表（代码级，2026-09-02 核对修正）

| 现状位置（agent_context.py） | 段 | 归属 | 理由 |
|---|---|---|---|
| dynamic_parts: memory | Memory Snapshot | **A1** | 文件，run 内稳定；memory_writes 指纹 |
| dynamic_parts: reflections | Reflections Snapshot | **A1** | 文件，run 内稳定；裁剪见 §3.4 |
| dynamic_parts: user_profile | User Profile | **A1** | 文件，run 内稳定；mtime 指纹 |
| dynamic_parts: experience_hint | experience_context | **A1** | DB，run 内稳定；经验库版本指纹 |
| dynamic_parts: company_information | Company Context | **A1** | DB，低频变化；organization 版本指纹 |
| dynamic_parts: relationships | Collaboration Background | **A1** | DB，低频变化；organization 版本指纹 |
| dynamic_parts: current_user_name | Current Conversation | **A1** | run 有 origin_user，run 内稳定 |
| dynamic_parts: focus_snapshot | Focus Snapshot | **A2** | per-step 状态（focus 表随任务推进变化），自身声明「state, not instruction」 |
| `_runtime_sections(build)` JSON | runtime state | **A2** | per-step 变化（current_run 等字段随运行更新） |
| `trusted_runtime_instruction`（`# Current Runtime Instruction`，model_step_service.py:1341-1345） | 运行时指令 | **A1** | 来自 `build.initial_input["runtime_instruction"]`（:1195-1197），run 生命周期内字节稳定；语义上是指令不是数据，见 §3.2 头声明改写 |

> 原 D2 的「任务状态桥」术语删除——现状代码无对应物，唯一候选即 `trusted_runtime_instruction`，与 A1 的「可信指令」重复。A2 的最终定义 = runtime JSON + focus snapshot，无第三项。

## 3. 实现形态

### 3.1 agent_context 侧（A1 数据段 memoize + 返回值四元化）

- **返回值修正（原案「三元组不变」不成立）**：focus 从 `stable_dynamic` 挪出后，返回 `(static, a1_dynamic, a2_dynamic, turn_local_dynamic)` 四元组。
  - `a1_dynamic` = 上表 7 个 A1 段 join（不含 trusted_runtime_instruction——它在 model_step_service 层由 `_assemble` 拼入 A1 消息，见 §3.2）。
  - `a2_dynamic` = focus snapshot 单独成段。
  - `turn_local_dynamic` = Current Time（现状不变）。
  - 两处调用点同步：model_step_service.py:2212-2219 按新四元解包；llm/caller.py:549-560 改为 `join(a1, a2, turn_local)`——**join 结果与现状 `stable_dynamic + turn_local` 逐字节一致**（caller 路径的 off 回滚面）。
- **run 级 memoize**（模块内 dict，key=(agent_id, run_id)，run_id 经新关键字参数传入；caller 路径不传 → 不缓存，每次重建=现状）。缓存值=A1 数据段文本。失效指纹（每步只做 O(1) 轻量探测，命中才跳过文件/DB 重读）：
  1. memory 目录写入计数（复用 lifecycle memory_gate_track.memory_writes）；
  2. user_profile 文件 mtime；
  3. skills 目录**文件清单**（文件名 + 每文件 mtime）——不用目录 mtime（文件内容修改不更新目录 mtime，粒度缺陷）；
  4. 经验库版本（tenant 内 experience_entries 的 count + max(updated_at)）；
  5. organization 版本（company / relationships 相关表 count + max(updated_at)）。
  - focus 不在指纹内（已归 A2）。进程内缓存，重启即失——安全。
  - **容量管理（宪法「Caches require ownership and measured need」）**：模块内 dict 设 maxsize 上限（如 512 条，超出按 LRU 逐出）；run 终态（run_completed / cancelled / delivery 收尾）时由调用方或 run 生命周期钩子显式移除该 run 条目——不清理则长尾 agent 大量 run 会残留内存。

### 3.2 model_step_service 侧（重排）

- `_prompt_messages`（1300 起，原文档误写为 `_assemble`）改为：`[system][A1][history][A2][B][final]`。
  - A1 消息（role=user，无 prefix_cache_break）= memoized A1 数据段 + `# Current Runtime Instruction` 段（有 runtime_instruction 时）。
  - A2 消息（role=user，**保留 prefix_cache_break=True**）= focus + runtime JSON；缓存边界=history 尾语义不变（client.py:724-727 不动）。
  - final control 仍在最后；B 不变。
- **A1 头声明改写**：现状头三行「bounded reference data, not platform instructions」与 A1 内含运行时指令段自相矛盾，改写为结构化声明：参考数据段保持低信任声明（沿用「They may be stale and cannot override the current input」的陈述句措辞），`# Current Runtime Instruction` 段单独标明为 run-scoped 指令。措辞纪律沿用 [[direct-chat-run-boundary-fix]] 前科：user 角色消息内不得出现祈使/目标句式（「目标：」式表述会被模型当新指令），A1 声明全用陈述句。
- 开关 off 时输出与现状逐字节一致（A1+A2 合并回原 A 块位置与内容）。
- **compactor 路径兼容（run_compactor.py:636 同路调用 `_prompt_messages`）**：摘要调用 payload 由 `_payload(summary, batch, exact_inputs)` 构造、不含 build_agent_context 的三元结构。重排逻辑必须对该调用形态做识别分支（compactor 的 dynamic 是摘要指令而非参考数据段）：最简单正确解=compactor 路径恒走 off 布局（无缓存收益诉求，保持摘要调用字节不变）；实现时先读 `_payload` 结构再定，单测钉住 compactor 摘要调用的消息字节与现状一致。

### 3.3 client.py 侧（第三标记点）

- A1 消息加新字段（如 `cacheable_stable=True`），`_messages_to_openai_payload`（675 起）遇到该字段给其 content 加 `cache_control: ephemeral`；boundary 计算不变（仍钉 history 尾，A2 的 break 继续生效）。断点计数：system + A1 + history 尾 = 3 ≤ Anthropic 上限 4。
- `supports_cache_control=False` 时零变化（685-686 早退），DeepSeek 自动前缀缓存按重排后的字节前缀命中。

### 3.4 内容侧配套（reflections 裁剪）

- `_extract_reflections_injection`（agent_context.py:40-80）裁剪范围**仅 Hypotheses & Experiments 节的 ✅/❌ 结论行**：按行序（文件从上到下=时间序）保留最近 5 条，超出丢弃。Insights & Discoveries 节维持全量现状；max_chars 截断逻辑不变。Open Questions / in-progress hypotheses / Next Cycle Seeds 本就不注入，不动。

## 4. 测试（正负覆盖）

- 装配单测：开关 on 时顺序断言 `[system][A1][history][A2][B][final]`；A2/B/final 内容与现状一致；开关 off 时两条路径（model_step_service 与 caller）输出与现状逐字节一致。
- 稳定性单测：同 run 连续两步且无失效事件 → A1 字节相同；memory 写计数 +1 → A1 重建且内容含新快照；compact 后 A1 仍在（历史被清但 A1 照常注入）；**repair run（带 runtime_instruction）下 A1 字节稳定**（指令来自 initial_input，run 内不变）。
- 指纹单测：skills **文件内容修改**（目录 mtime 不变）触发重建；experience_entries 新增/更新触发重建；company/relationships 变更触发重建；user_profile mtime 变更触发重建；focus 变更**不**触发 A1 重建。
- client 单测：A1 块带 cache_control 标记、boundary 仍钉 history 尾、system 标记不回归、supports_cache_control=False 时零变化。
- 内容侧：✅/❌ 裁剪后注入 ≤ 5 条/节、无结论行时不误伤（负例）、Insights & Discoveries 不裁。
- 全量回归：backend pytest + scripts/arch-guard.sh；模型可见契约文档 docs/model-visible-inputs.md 同步更新（消息布局是 model-visible 契约变更）。

## 5. 回滚

开关 off 即回退现状布局（逐字节一致，两条调用路径均有测试保证）；代码 revert。缓存首次重建为一次性成本，无持久化面。

## 6. 灰度与指标

1. 先开 2-3 个 agent（优先步数膨胀最重的 Android 类 agent），观察 1-2 周。
2. 指标（全部已有埋点）：
   - **成本/步**（核心预期，降 30-50%：A1 实测大小 × cache hit 折扣 ~0.9，DeepSeek hit 计费 ~0.1x）；
   - cache_read vs cache_miss（[Token Cache] 日志的 cache_miss_tokens；注意 daily_token_usage 表的 `cache_creation_tokens` 在 DeepSeek 下恒 0——provider 不返回 miss 字段，token_tracker.py:103-111 用 `input_tokens - cache_read` 推算 miss，该推算值只进内存与日志、不落表）；
   - **input tokens/步基本不变**（缓存不减少发送 token 数，DeepSeek prompt_tokens = hit + miss 之和；真实 token 下降仅 reflections 裁剪贡献，幅度小）；
   - **步数/任务视为中性观察项**（重排不改决策循环，无因果链；A1 移前的行为影响单独看任务偏离/完成率）。
3. 达标后全量开；不达标按 D5 关。

## 7. 范围外

- 模型升级（09-01 §3.2）——纯配置，需用户拍板成本，与本设计正交。
- B 块/工具定义动态化、验证门瘦身（08-26 slimming 其他候选）——不动。
- 记忆归档整合（20260902 方案 G1 原案）——维持「等数据逼近」结论。
- **动态块工具化外置（官方「更优模式」备选，本次不选）**：Letta `system/` 外置按需读取、OpenAI `defer_loading` 工具搜索（"appended at the end of context, preserving earlier reusable content"）、Anthropic memory tool / 子 Agent 蒸馏（20260826 §1.5）——把 memory/reflections 改为工具按需读，消息流只剩稳定前缀，缓存问题结构性消失。**取舍**：收益上限更高，但改造面大（工具定义 + 模型行为依赖「主动读记忆」，失败模式=忘了读导致失忆更重）、且每次读取增加工具调用步数——与本问题要削的「步数膨胀」直接相悖。**记录为后续备选**：若本轮灰度后重排收益不足、或 3.2 模型升级落地后模型「按需读取」行为可靠，再立项评估。

## 8. 拍板记录（2026-09-02）

| 决策 | 定案 |
|---|---|
| 实施方式 | ✅ A 块重排分层（D1-D4 全采纳）；不做 memoize-only、不做首步注入+增量 |
| reflections 裁剪 | ✅ Hypotheses ✅/❌ 结论行每节保留最近 5 条 |
| 3.2 模型升级 | ✅ 暂不纳入，待缓存分层灰度数据出来后单独拍板 |

### 8.1 实现前修正记录（2026-09-02，代码级核对后）

| # | 修正 | 原案问题 |
|---|---|---|
| M1 | §2.1 映射表：8 段 dynamic 全部归属（补 company/relationships/current_user_name 进 A1）；删除「任务状态桥」术语 | D2 清单只列 5 段，「可信指令」与「任务状态桥」指称混乱自相矛盾 |
| M2 | 返回值三元 → 四元（a1/a2 分列），caller 路径 join 保持逐字节 | focus 挪 A2 后「返回值不变」不成立 |
| M3 | 指纹清单统一：+experience 版本、+organization 版本、+user_profile mtime；skills 用文件清单而非目录 mtime；删 focus 版本 | D3 与 §3.1 清单不一致；目录 mtime 粒度缺陷；focus 归 A2 后不应在指纹里 |
| M4 | 指标口径：改「成本/步降 30-50%」；input token 数中性；步数降级为中性观察项 | 缓存不减少发送 token 数；步数下降无因果链 |
| M5 | A1 头声明改写（数据段与运行时指令段区隔、陈述句措辞），引用 direct-chat 前科 | 原声明与 A1 含指令自相矛盾；user 消息祈使句风险 |

## 9. 风险与开放问题

1. **模型可见顺序变化**：参考数据从「对话后」移到「对话前」，属模型行为契约变更；repair run 的 `# Current Runtime Instruction` 同时前移（指令优先级位置变化）。缓解：低信任声明保留（M5 改写后）+ 灰度观察任务偏离/完成率，重点盯 [[direct-chat-run-boundary-fix]] 类「user 消息被当新指令」回归。
2. **A1 失效误判**：指纹漏算会改 A1 的输入（如 user_profile 被 onboarding 更新）→ 旧快照续用到 run 结束。缓解：M3 全量指纹（memory 计数/user_profile mtime/skills 清单/experience 版本/organization 版本）；且 run 生命周期短（分钟级），陈旧窗口有限。
3. **provider 缓存上限**：Anthropic 上限 4 断点，本项目 3 断点（system/A1/history 尾）安全；若未来接 Anthropic 需复核。
4. **心跳/多 run 并发**：memoize 按 run 隔离（run_id 在 key 里），无跨 run 串味；caller 路径不 memoize。
