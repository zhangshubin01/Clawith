# 多轮对话前缀缓存调研：动态内容如何组织以保住缓存（DeepSeek / Anthropic / OpenAI 官方资料）

> 调研日期：2026-08-18。问题背景：Clawith 每轮重发 `system(稳定) + user(含每轮变化的动态 JSON) + 全量历史(稳定 append)`，
> 实测 DeepSeek 前缀缓存从动态 user 消息处断裂，其后历史每轮全量 miss。

## 0. 资料获取情况（如实说明）

| 来源 | 目标 URL | 结果 |
|---|---|---|
| DeepSeek 官方文档 | https://api-docs.deepseek.com/guides/kv_cache | ✅ 实时抓取成功（mcp__fetch） |
| Anthropic 官方 docs | https://docs.anthropic.com/en/docs/build-with-claude/prompt-caching | ❌ 本机网络对该域名全线阻断（robots.txt 连接失败 / TLS 握手中断），Wayback 存档同样不可达 |
| Anthropic 工程博客 | https://www.anthropic.com/engineering/prompt-caching-with-claude | ❌ 404（文章已迁移），存档不可达 |
| Anthropic 官方 GitHub（兜底） | https://github.com/anthropics/claude-cookbooks/blob/main/misc/prompt_caching.ipynb | ✅ 经 GitHub API 抓取（sha `085a3c299f...`，与 main 一致，22917 字节完整） |
| OpenAI platform docs | https://platform.openai.com/docs/guides/prompt-caching | ❌ Cloudflare 403（robots.txt 拒绝） |
| OpenAI 官方 GitHub（兜底，任务允许） | openai/openai-cookbook `examples/Prompt_Caching101.ipynb`、`examples/Prompt_Caching_201.ipynb` | ✅ 经 GitHub API 抓取（sha `953014a6...` / `b1414bc8...`，与 main 一致） |

以下所有结论均出自上述一手资料原文，无凭记忆编造；Anthropic 结论注明出自官方 GitHub cookbook（官方 docs 内容与该 cookbook 相互印证，cookbook 内也链接了同一 docs 页面）。

---

## 1. 各来源关键结论（带 URL 与 Clawith 适用性）

### 1.1 DeepSeek — Context Caching（KV cache）
来源：https://api-docs.deepseek.com/guides/kv_cache

1. **命中规则 = 前缀单元完整匹配。**「由于 Sliding Window Attention 机制，每个缓存前缀是一个独立的、完整的单元，后续请求只有完整匹配某个缓存前缀单元才能命中。」磁盘缓存「只匹配用户输入的前缀部分」。
   → 对 Clawith：序列中间的动态 user 消息使其后所有 token 都不属于任何可命中前缀，**历史部分每轮全量 miss 是机制使然，不是运气问题**。
2. **三种前缀持久化时机**：
   - 请求边界持久化：每个请求在「用户输入结束位置」和「模型输出结束位置」各产生两个前缀单元；
   - **共同前缀检测持久化**：系统检测多个请求的共同前缀，把它单独持久化为一个前缀单元（文档例 2：`A+B` 与 `A+C` 互不命中，但系统把共同前缀 `A` 持久化，第三条 `A+D` 可命中 `A`）；
   - **长输入固定 token 间隔切块**：超长输入/输出按固定间隔切出前缀单元，避免超长前缀因永远够不到终点而完全不可命中。
   → 对 Clawith：重排后历史部分可通过「共同前缀检测」+「间隔切块」命中，但需要 1–2 轮「预热」（见 §3 方案 1）。
3. **可观测性**：响应 `usage` 含 `prompt_cache_hit_tokens` / `prompt_cache_miss_tokens`。
   → 对 Clawith：重排前后可直接用这两个字段做 A/B 验证。
4. **限制**：best-effort，不保证 100% 命中；「缓存构建需要数秒」；缓存不用后数小时至数天内自动清除；无任何 cache_control 类显式标记（官方文档通篇未提断点机制）。
   → 对 Clawith：命中率天花板低于 Anthropic/OpenAI，但结构对了之后主体命中可期。

### 1.2 Anthropic — prompt_caching.ipynb（官方 cookbook）
来源：https://github.com/anthropics/claude-cookbooks/blob/main/misc/prompt_caching.ipynb
（官方 docs：https://docs.anthropic.com/en/docs/build-with-claude/prompt-caching ，本机不可达）

1. **两种启用方式**：自动缓存（请求顶层 `cache_control={"type":"ephemeral"}`，系统自动把断点放在「最后一个可缓存 block」上并随对话增长**自动前移**）；显式断点（放在具体 content block 上，最多 4 个，可分别设 TTL，官方推荐场景之一就是「把 system prompt 与消息内容分开独立缓存」）。
2. **多轮对话缓存行为表（官方原文）**：
   | 请求 | 缓存行为 |
   |---|---|
   | R1 | System + User:A 写入缓存 |
   | R2 | System + User:A 读缓存；Asst:B + User:C 写入 |
   | R3 | System 至 User:C 读缓存；Asst:D + User:E 写入 |
   并明确结论：「首轮之后，**接近 100% 的输入 token 每轮都从缓存读取**——对话代码只是普通消息列表，无需在单个 block 上放任何标记。」
   → 对 Clawith：这是「稳定前缀 append 增长」的理想模型；前提是前缀内没有任何每轮变化的内容。
3. **显式断点示例（Example 3）**：`cache_control` 放在 system prompt 的文本 block 上（独立缓存），并把断点**手动放在最后一个 user 消息上、每轮前移**。断点语义 = 「从请求开头到该 block 为止」的整段前缀。
   → 对 Clawith：Anthropic 路径 Clawith 已实现断点放置（`client.py:2013`、`2027-2039`），但当前把断点放在「最后一个 user 消息」上时，动态 user JSON 落在断点之前 → 前缀每轮变化 → 除 system 静态 block 外全部 miss（与 DeepSeek 同病）。
4. **关键参数**：最小可缓存长度 Sonnet 1024 / Opus 与 Haiku 4.5 4096 tokens；TTL 默认 5 分钟（每次命中刷新），1 小时 TTL 需 2x 输入价；写 1.25x / 读 0.1x 基准价；显式断点上限 4 个（自动缓存占 1 个）。
   → 对 Clawith：轮次间隔 >5min 会掉缓存；动态块大小 <1024 tokens 时本就不参与缓存，放在末尾不损失。

### 1.3 OpenAI — Prompt_Caching101 / Prompt_Caching_201（官方 cookbook）
来源：
- https://github.com/openai/openai-cookbook/blob/main/examples/Prompt_Caching101.ipynb
- https://github.com/openai/openai-cookbook/blob/main/examples/Prompt_Caching_201.ipynb

1. **官方总体建议（101 "Overall tips" 原文）**：「把静态或高频复用内容放在 prompt **开头**……把动态数据放在 prompt **末尾**（Place static or frequently reused content at the beginning of prompts… keeping dynamic data towards the end of the prompt）。」
   → 对 Clawith：直接回答设计问题 b；Clawith 现状正好反着放。
2. **多轮/工具调用（101 Example 1）**：「工具定义及其**顺序必须完全一致**才能进入前缀；要多轮历史命中，就把新元素 **append 到 messages 数组末尾**。」（run2 的 `cached_tokens>0` 验证命中；tool result 作为 append 的一部分进入前缀。）
3. **201 §4.2 Stabilize the Prefix（最直接的一手依据）**：
   - 「把持久内容放开头：Instructions、Tool definitions、Schemas；把易变内容（用户输入、动态值、会话特定数据）**移到末尾**。早期 token 的微小变化都会使精确前缀匹配失效（Even small changes in early tokens will invalidate exact prefix matching）。」
   - **Codex agent loop 官方经验**：「Codex CLI 在请求间保持 system 指令、工具定义、沙箱配置、环境上下文完全一致且顺序固定，以保住长而稳定的前缀；运行时配置中途变化（如换工作目录、审批模式）时，agent loop **追加新消息而不是修改旧消息**。」——这正是「多轮 agent 对话 + 动态状态注入」问题的官方回答。
   - 「不要把 timestamp 放在请求开头，**移到 `metadata` 里**，那里不影响缓存。」
4. **201 §5 排障清单（原文）**：缓存下降常见原因包括「Adding in a space, timestamp or other dynamic content」；201 §4.6：「压缩/摘要/丢弃早期轮次会破坏缓存——context engineering 与 prompt caching **天然对立**：一个要动态，一个要稳定」；结论：「stabilize the prefix, monitor `cached_tokens`」。
   → 对 Clawith：Clawith 每轮变化的动态 JSON + system 尾部的 runtime instruction，命中官方排障清单里的所有反模式。

---

## 2. 三个设计问题的明确答案

### a. 前缀中某消息变化 → 其后所有内容是否全部缓存失效？DeepSeek 切块机制能否让「变化点之后的历史」独立命中？

**答：失效。变化点之后的内容在任何一家都无法作为「当前请求的前缀」独立命中；DeepSeek 的固定间隔切块也不能救变化点之后的历史。**

- **DeepSeek（有出处）**：官方文档明确「磁盘缓存只匹配用户输入的**前缀**部分」；缓存单元是「独立的、完整的**前缀**单元」，命中要求「完整匹配一个缓存前缀单元」。固定间隔切块切的仍是**从开头起算的前缀单元**（"carve out cache **prefix** units at fixed token intervals"），其作用是让超长输入在「最后一个切块边界」处也能命中——即只能保住**变化点之前、最近一个切块边界为止**的内容；变化点之后的历史位于任何前缀单元的边界之外，永远不属于任何可命中前缀。唯一例外路径是「共同前缀检测」：多条请求的共同前缀（必然从开头起算）会被持久化，第三条起可命中该**较短**前缀——但这依然不覆盖变化点之后的 token。
- OpenAI（有出处）：201 §1.1「Cache hits require an exact, repeated **prefix** match」；101 Example 2 实测「第一个图片 URL 不同 → 整个请求 miss」。无任何「跳过中间段」机制。
- Anthropic：断点定义的是「从开头到断点」的完整区间，断点之后不缓存；不存在「中间跳过」语义（详见 c）。
- **对 Clawith 的推论**：当前顺序 `system → user(动态) → 历史` 下，历史每轮全量 miss 是三家 provider 共同的结构性必然。DeepSeek 即使触发了「共同前缀检测」，能持久化的共同前缀也只有 `system`（因为动态 user 在第二位就分叉了），与实测现象完全吻合。

### b. 官方推荐把动态内容放消息序列末尾，还是拆分稳定/动态段？

**答：放消息序列末尾。** 「在 user 消息内部拆分稳定/动态两段」对前缀缓存没有帮助——前缀匹配看的是整个请求序列化后的 token 流，user 消息内部分段不改变「动态 token 位于中间」的事实。

- **OpenAI（明确推荐，有出处）**：101 tips「static 开头、dynamic 末尾」；201 §4.2「Move volatile content … to the end. Even small changes in early tokens will invalidate exact prefix matching」；Codex 实践「append 而不是修改早期消息」。
- **Anthropic（结构上隐含推荐）**：自动缓存把断点放在「最后一个可缓存 block」上——即官方模型就是「前缀全稳定 + 新内容不断 append」；显式断点示例把断点放在最后一个 user 消息上（每轮前移），动态内容自然位于断点之后。
- **DeepSeek（机制上隐含）**：文档例 2（长文 QA）的结构就是「system + 文档（稳定、成为共同前缀）+ 每轮不同的问题（末尾）」，第三轮起命中稳定部分。文档没有「分段」相关的任何表述。
- **对 Clawith 的推论**：正确的顺序是「稳定内容全部在前（system + 历史）→ 动态内容压到最后」。这不只是 provider 建议，也是唯一能保住历史前缀的结构。

### c. 缓存断点/标记机制（cache_control）能否让 provider 跳过动态段、命中后面的稳定段？

**答：不能。** 三家的断点/标记机制都只定义「从请求开头到标记处」的前缀区间，没有任何机制能跳过中间的动态段去缓存其后的稳定段。

- **Anthropic（有出处）**：`cache_control` 断点语义是「标记请求中**到此为止**应缓存的位置」（最多 4 个，各自独立 TTL）；多个断点之间是**嵌套前缀**关系（每个断点都包含从开头的完整内容）。若把断点放在动态段之前，动态段及其后的稳定段都不在缓存区间内；若把断点放在动态段之后，动态段本身进入前缀 → 每轮变化 → 该前缀永不命中。官方 cookbook 的场景表也证实：断点只能「缓存 system 独立于消息」或「缓存到最后一个 user 消息为止」这类**前缀**用法。
- **OpenAI（有出处）**：无断点标记；`prompt_cache_key`（201 §4.4）只影响**路由粘性**（把相同前缀的请求尽量路由到同一台推理机，命中率 60%→87% 的客户案例），不改变「精确前缀匹配」的语义，不能跳过中间变化。
- **DeepSeek（有出处）**：官方文档未提供任何显式缓存标记；命中只能靠前缀单元完整匹配。
- **对 Clawith 的推论**：不要在「保留动态段在中间」的前提下寻找标记魔法——不存在。唯一杠杆是**消息顺序**。对 Anthropic 侧可以顺手做的优化：显式断点分别放在 system 静态 block 与最后一个 user 消息上（Clawith 已实现后者，需保证断点前的内容每轮稳定）。

---

## 3. 综合建议：Clawith 消息重排候选方案

### 现状（代码核对，2026-08-18）
- 组装点：`backend/app/services/agent_runtime/model_step_service.py:611-725`（`_prompt_messages`）。
  顺序 = `system(static_prompt)` → `user(dynamic_prompt + "Relevant Runtime Context" + runtime_context JSON)` → 追加历史（session/thread/initial_input）。
- `system` 尾部还每轮拼接 `dynamic_content`（runtime instruction）：OpenAI 兼容路径 `LLMMessage.to_openai_format()`（`client.py:246-262`）把 `dynamic_content` 拼进 system content → **system 本身每轮变化**，前缀在第一位就断了。
- DeepSeek 走 `OpenAICompatibleClient`，`supports_cache_control=False`（`client.py:2610`，仅 qwen 为 True）→ 无任何断点标记。
- Anthropic 路径（`client.py:1993-2077`）已放断点：system 静态 block（2013 行）+ 最后一个 user 消息（2027-2039 行）；但动态 user 在序列第二位，落在断点之前 → 前缀每轮变化 → 除 system 静态块外全 miss。

### 方案 1（推荐，改动最小）：动态内容整体移到序列末尾

新顺序：`system(仅静态，字节级一致)` → `全量历史(纯 append)` → `user(动态 prompt + runtime context JSON + runtime instruction)`。

配套改动：
1. `_prompt_messages` 把动态 user 消息从 `messages[1]` 挪到历史之后（当前 `deferred_current` / `initial_input` / directive 的追加逻辑已有「当前输入放最后」的雏形，动态块并入该位置即可）。
2. `system` 的 `dynamic_content`（runtime instruction）**移出 system**，并入末尾动态块（加个 `# Current Runtime Instruction` 标题保语义）。OpenAI 201 §4.2 官方警告：Instructions 变化会 invalidate 前缀；DeepSeek 的 system 是整个前缀的头部，更不能动。
3. 历史必须严格 append（不修改、不重排、不摘要旧消息）——201 §4.6 明示压缩早期轮次会破坏缓存，若要压缩需接受缓存重置。

**预期命中效果（有依据）**：
- DeepSeek：第 1 轮 miss（无可匹配单元）；第 2 轮起「共同前缀检测」把 `system+历史` 持久化为独立单元；第 3 轮起每轮 miss 仅剩「本轮新增的 assistant 回复 + 新 user 输入 + 末尾动态块」，主体命中。DeepSeek 为 best-effort + 持久化需数秒 + 单元大小受固定间隔切块影响，实测预期**命中主体（非 100%）**，可用 `prompt_cache_hit_tokens` 直接验证。
- 结构依据：Anthropic 官方多轮表（同构结构下「接近 100% 输入每轮读缓存」）；OpenAI 101 run2 `cached_tokens>0`；DeepSeek 例 2 第三轮命中稳定前缀。
- 附带收益：OpenAI 直连 / Anthropic 路径同样受益——Anthropic 的「最后一个 user 消息」断点此时恰好落在动态块之前，前缀 = 稳定的 system+历史，命中理想；system 静态块断点继续独立命中。

### 方案 2（备选，仅当动态 JSON 必须靠前时）：稳定段显式断点 + 接受动态段后不可恢复

若产品上必须保持「动态 user 在第二位」（如对某些 provider 的 user 消息语义有强依赖），则只能在 provider 支持断点时做局部止血：
- Anthropic：把 `cache_control` 放 system 静态 block（已有）+ 动态 user 之后的**历史首条**无法标记——断点只能定义前缀，因此历史仍然 miss；唯一可保的是 system 静态前缀。收益有限。
- DeepSeek：无标记机制，此顺序下只能依赖「共同前缀检测」保住 system 部分。**方案 2 不解决历史 miss 问题**，仅在无法重排时避免更糟。

**结论：采用方案 1。** 它与三家官方文档的结构建议完全一致（OpenAI「static 开头、dynamic 末尾」、Anthropic 自动断点前移模型、DeepSeek 前缀单元模型），且能用 DeepSeek 的 `prompt_cache_hit_tokens/miss_tokens` 字段做量化验证。

---

## 附录：验证方法（重排落地后）
1. DeepSeek：同一会话轨迹，重排前后各跑一轮，对比 `usage.prompt_cache_hit_tokens` 占比；轮间间隔 <5 分钟、连续 3+ 轮观察预热曲线。
2. Anthropic：`usage.cache_read_input_tokens / cache_creation_input_tokens`（cookbook 的 `print_usage` 同款字段）。
3. OpenAI：`usage.prompt_tokens_details.cached_tokens`（101 run2 同款字段）。
4. 注意各家的 best-effort 属性：单轮偶发 miss 不构成回归证据，以 3 轮以上均值判定。
