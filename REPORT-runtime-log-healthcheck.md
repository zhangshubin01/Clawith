# Clawith 运行时日志体检报告

- 体检对象：真实测试栈（第四十二次部署，commit 链 `9223c0c1 → 6b191218 → 6a38e387 → 04b4d7ba`，2026-08-26 部署）
- 日志窗口：2026-08-26 07:41 – 10:41 +08:00（宿主 `/tmp/clawith-logs-3h.txt`，449KB，INFO 级）
- 数据源：容器日志（docker cp 提取容器实际运行代码）、DB（只读 SQL）、Redis（只读）
- 性质：只读排查。本报告只描述问题与证据，不做任何修复。
- 时间口径：DB 时间戳均为 UTC；+08:00 = UTC+8。

---

## 结论速览

| # | 事项 | 判定 | 严重度 |
|---|------|------|--------|
| 1 | 飞书 WS 瞬断 | 自愈，无需处理 | 低危 |
| 2 | 群聊 run `delivery_status='pending'` | **卡片模式的设计行为**，非故障 | 无（但存在监控误报风险） |
| 3 | DeepSeek token cache miss 率高 | 部分为统计 bug 假象，真实命中率 65–90%；剩余 miss 有明确根因链 | 中（监控数据失真） |
| 4 | **统计 bug：`cache_read_tokens` 被精确双倍计数** | 实锤，主仓库同样存在 | 中（缓存节省监控失实） |
| 5 | 基础设施健康项（daemon/连接池/startup/TDD/heartbeat） | 全部正常 | 无 |

---

## 一、飞书 WS 瞬断自愈（低危）

日志中存在飞书 WebSocket 短暂断流事件，均自动重连恢复，未观察到功能影响或事件丢失。属外部通道抖动，**无需处理**，列入观察即可。

---

## 二、群聊 run `delivery_status='pending'` —— 卡片模式设计行为（非故障）

### 2.1 现象
24 小时窗口内：17 条 foreground run 的 `delivery_status='pending'`，全部是飞书群聊/私聊卡片消息；另 8 条 `delivered` 全部是 direct chat（网页）消息。

### 2.2 破案：这是设计行为，不是丢消息
部署版 `checkpoint_side_effects.py` 的 `delivery_from_checkpoint` 存在**卡片模式分支**：

> 当 delivery_target 携带 `_card_config.app_id` 且 card_stream_bridge 活跃时，终态内容走 `bridge.finalize(content)` 后**直接 `return None` 抑制 ChannelDelivery**。

即：不写 ChatMessage、不建 outbox、`delivery_status` 永远停留在 `pending`。

### 2.3 实证：卡片桥全程正常
抽查两个 run：
- `6170974f`（07:57）、`f1a6907e`（08:03）
- 均见完整链路 `bridge_created → card_created → 80+ 次 stream_card_content code=0 → card_completed`，飞书 API 全部 `code=0`。

用户实际收到了卡片消息，只是没有投递记录而已。

### 2.4 附带观察
- `channel_deliveries` outbox 最新记录停在 **2026-07-29**（旧版失败码 99992351），说明该表在卡片模式下已长期不写入。
- 08-26 09:26–10:16 用户三次催促「apk 发送过来」属**飞书文件发送通道**的独立问题，该时段日志随容器重启丢失，本次无法定论。

### 2.5 风险提示（监控层面）
若存在「pending 即告警」的监控规则，卡片模式下会**持续误报**。建议监控口径将卡片模式 run 排除，或改用卡片桥自身的 `card_completed` 作为成功信号。

---

## 三、DeepSeek token cache：真实命中率修正与 miss 根因链

### 3.1 统计 bug：`cache_read_tokens` 被精确双倍计数（实锤）

**数据证据**：`daily_token_usage` 表 08-26 全部 13 行、08-27 的 12/13 行，**精确满足**：

```
input_tokens = cache_read_tokens / 2 + cache_miss_tokens
```

即真实缓存命中 token = 记录的 `cache_read_tokens` 的**一半**。

**代码机制**（容器实际运行文件 `/tmp/container_token_tracker.py`，`extract_token_usage` 57–116 行）：

OpenAI-compatible 分支对 usage **顶层** keys `cached_tokens / cache_read_tokens / cache_read_input_tokens / prompt_cache_hit_tokens` 求和之后，**又**对 `prompt_tokens_details` / `input_tokens_details` 里同样的四个 key **再求和一遍**。

DeepSeek 实际响应**同时**携带顶层 `prompt_cache_hit_tokens` 和 `prompt_tokens_details.cached_tokens`（同值）→ cached 被精确翻倍。而 `cache_miss` 只取顶层 `prompt_cache_miss_tokens`（单次）、`input` 取 `prompt_tokens`（单次），所以只有 cached 翻倍。

**影响范围**：
- `daily_token_usage.cache_read_tokens` 与 `Agent.cache_read_tokens_*` 列全部虚高 1 倍；
- 基于它的「缓存节省金额/比例」类监控全部失实；
- 看门狗告警（`token_tracker.py:230–240`，用的是 miss/input 口径）**不受此 bug 影响**，告警本身真实。

**主仓库同样存在**：`backend/app/services/token_tracker.py`（78–96 行）逻辑相同；`backend/tests/test_token_tracker.py` 的夹具只测了「单一来源」形状，未覆盖 DeepSeek 这种「两处同时存在同值」的真实载荷，故测试未抓到。

### 3.2 修正后的真实命中率（远好于表面数据）

| 时段 | Agent | 修正后命中率 | 说明 |
|------|-------|-------------|------|
| 08-26（13 个活跃 agent） | b1a73489 | 81.4% | input 6.09M，大 agent |
| | 62bc9c81 | 78.2% | input 2.44M |
| | c8ec0dbe | 89.5% | |
| | 27d55a64 | 70.4% | |
| 08-27 早（集群并发） | 62bc9c81 | 70.4% | input 1.84M |
| | b1a73489 | 78.0% | input 1.72M |
| | adcc7e8a | 19.2% | 小 agent |
| | c8ec0dbe | 19.0% | |
| | 7bdf708d | 21.1% | |
| | 82dc9a8a | 24.6% | |
| | 08a739c1 | 26.8% | |

结论：**大 agent 的真实命中率在 65–90% 区间，并非表面数据暗示的普遍 93–99% miss**。

9 条看门狗 WARNING（07:58–08:41 +08，ratio 64–99%）均为**尾部请求**（压缩后 / system 变化 / 交错时），不是普遍状态。

### 3.3 miss 根因链（解释剩余的真实 miss）

**DeepSeek V4 缓存机制**（官方文档 `guides/kv_cache`）：滑动窗口注意力，缓存前缀单元**独立存储**，命中要求**完整匹配一个已持久化的前缀单元**。单元持久化时机：①请求边界（user input 末尾 + model output 末尾）；②多请求公共前缀检测；③长输入固定 token 间隔切块。

由此推出根因链：

1. **单 agent 顺序请求**：公共前缀单元 = 该 agent 稳定历史 → 高命中（08-20 对照实验 hit=7040/miss=111 = 98.4%）。
2. **13 个 agent 并发交错、共用同一 key+model 缓存**：各 agent 请求交错提交后，公共前缀单元退化为「各 agent 系统提示词的公共平台前导」（~512 tokens）。
3. 实测吻合：`6170974f` step3 命中 512 / 11464（4.5%）——与「~512 tokens 公共前导」精确对应。

**结论：miss 根因 = 「多 agent 并发交错 × 滑动窗口全单元匹配」的叠加效应，不是代码回归。**

### 3.4 运行内前缀破坏事件（指纹链证据）

指纹机制：`model_step_service.py` `_cache_fingerprints`（710 行起）对每条 LLMMessage `asdict` 后弹出 volatile 键 `tool_calls/tool_call_id/reasoning_content`，sha256 取前 12 位；chain = `role首字母:hash12` 逐条拼接。日志行 `[LLM-CacheFp]` 共 68 条。

三类破坏事件：

| 类型 | 实例 | 影响 |
|------|------|------|
| ① 压缩 compaction | cf2d7ca0 18→19：公共前缀 65/69 → 1/10，tokens 40771→10407；ea3a0a94 16→17：41305→12439 | 历史整体重写，前缀缓存清零 |
| ② system 提示词中途变化 | ea3a0a94 15→16：`s:d1da3121c72c → s:f7234bdd7e3c` | 整个链的公共前缀失效 |
| ③ 历史中部插入/重复/消失 | 6170974f 2→3 pos20 插入 `u:9f30f870babd`；`u:15f8a11d1db8` 每步连续重复；`a:31ec043af47c` 同请求两次 | 双快照投影非单调 |

③的机制：session/thread 双快照按 raw id 去重，**无 string id 的消息不去重**，导致投影非单调。

### 3.5 结构性 miss 下限与已排除的假说

**结构性下限（设计如此，无法消除）**：
- 动态块（`prefix_cache_break` user 消息）天然切断前缀；
- 最新 assistant 消息 + 最新工具结果每步必新，永远无法命中缓存。

**已排除的假说**：
1. 「boundary 消息结构翻转」：`create_llm_client` 中 `supports_cache_control = normalized_provider == "qwen"`（llm_client.py:2677），DeepSeek 无 cache_control 标记，消息渲染字节稳定；
2. 工具 schema 变化：每 run 内 tools 哈希完全稳定（Android工程师=`0c8d3dfe8116`，Android工程师06=`9386993b0fa2`）；
3. 5 分钟 TTL：步骤间隔实测 2–60s；
4. reasoning_content 指纹盲区：指纹弹出它但 `to_openai_format` 会发送它（llm_client.py:269–270）；历史 assistant 的 reasoning_content 来自 checkpoint 稳定回填（model_step_service.py:1504–1505），无发送时注入。

---

## 四、健康项（全部正常）

- 15/15 daemon 健康；
- PG 连接池正常（无 53300 类连接耗尽）；
- 后端 startup 正常；
- TDD 循环（build/test 反馈）正常；
- heartbeat `superseded` 属预期路径（并发租约仲裁的正常结果，非故障）。

---

## 五、可操作建议（只报告，未动手）

按优先级排列：

1. **修双计统计 bug**（P1）：`token_tracker.py` 的去重逻辑——同一语义 key 在顶层与 `*_tokens_details` 同时存在时只计一次；补测试覆盖「两处同值」的 DeepSeek 真实载荷形状。主仓库与部署容器都要修。
2. **稳定 system 提示词**（P2）：避免 run 中途修改 system 内容；必须修改时视为显式的缓存断裂点。
3. **压缩摘要字节稳定化**（P2）：compaction 后公共前缀只剩 1/10，若摘要输出可稳定（确定性、增量式），可保住更多前缀缓存。
4. **双快照投影去重**（P2）：session/thread 双快照投影对无 string id 的消息也去重，保证链的单调性。
5. **看门狗告警口径**（P2）：交错场景下尾部请求会持续触发 WARNING；建议改为按 (agent, date) 日聚合口径，或排除压缩后首步/首步请求。
6. **监控 pending 口径**（P3）：见 2.5，卡片模式 run 的 pending 不应作为故障信号。
7. **多 agent 交错缓存退化**（观察项）：属 provider 侧（DeepSeek V4 滑动窗口单元匹配）能力边界，非本仓库可修复；列入观察，待 DeepSeek 缓存策略演进或评估 per-agent key 隔离的可行性。

---

## 附：证据文件清单（排查中间产物，均在 /tmp）

- `/tmp/container_model_step_service.py`（3169 行）
- `/tmp/container_thread_visibility.py`
- `/tmp/container_llm_client.py`（2781 行）
- `/tmp/container_token_tracker.py`（282 行）
- `/tmp/clawith-logs-3h.txt`（449KB 原始日志）

---

## 附 B：双计 bug 核实证据（2026-08-27 复核）

按「先建反馈回路、后下结论」的纪律复核，四路证据互相独立、全部闭合：

### B1. 代码证据（主仓库与容器实跑代码一致）
`extract_token_usage` 的 OpenAI-compatible 分支（主仓库 `backend/app/services/token_tracker.py` 74–101 行；容器实跑文件同位置）：
1. 第 74–80 行：对 usage **顶层** `cached_tokens/cache_read_tokens/cache_read_input_tokens/prompt_cache_hit_tokens` 求和得 `cached`；
2. 第 89–96 行：再遍历 `prompt_tokens_details` / `input_tokens_details`，对**同样四个 key 再求和一次**累加进 `cached`。

`cache_miss` 只取顶层 `prompt_cache_miss_tokens`（单次）、`input_tokens` 取 `prompt_tokens`（单次）→ 只有 cached 会被翻倍。

### B2. 真实载荷证据（对 DeepSeek 实发两次相同请求，第二次命中缓存）
`deepseek-v4-pro` 真实响应 usage（2026-08-27 实测，密钥未打印）：

- 第一次（miss）：`prompt_cache_hit_tokens: 0, prompt_cache_miss_tokens: 6697, prompt_tokens: 6697, prompt_tokens_details: {"cached_tokens": 0}`
- 第二次（hit）：`prompt_cache_hit_tokens: 6656, prompt_cache_miss_tokens: 41, prompt_tokens: 6697, prompt_tokens_details: {"cached_tokens": 6656}`

**两处同值（6656）同时出现**，且 6656 + 41 = 6697 守恒。按现行代码，这条请求被记为 `cache_read = 6656 + 6656 = 13312 > input 6697`——缓存命中数超过输入总数，物理不可能，双计实锤。官方文档（api-docs.deepseek.com/create-chat-completion）确认顶层 `prompt_cache_hit_tokens` 为正式字段。

### B3. 数据库证据（26 行日聚合全量复核）
查询 08-26 / 08-27 全部 26 行 `daily_token_usage`：
- `input_tokens - (cache_read_tokens/2 + cache_miss_tokens)` **25/26 行精确等于 0**；唯一例外 b05d3a82@08-27 残差 2587 ≈ 其 `estimated_tokens` 2603（字符估计路径混入，非 DeepSeek 命中记账，反而印证等式）。
- 备选等式 `input = read + miss` 全部大幅为负（负值恰好 = -read/2），排除。
- 强佐证：**26/26 行 `cache_read_tokens` 全为偶数**。双计下必然全偶；若为真实命中，26 个独立聚合全偶概率 ≈ 2⁻²⁶ ≈ 1.5e-8。
- Langfuse `usageDetails.input_cache_read` 亦全偶数且普遍 > input（如 83456 vs input 10633），与双计一致（归一化视图，仅作旁证）。

### B4. 回归测试证据（先写测试、看它变红）
在 `backend/tests/test_token_tracker.py` 新增用例 `test_real_payload_top_level_and_details_same_value_not_double_counted`（载荷 = B2 真实形状）。运行结果：

```
FAILED tests/test_token_tracker.py::TestExtractDeepSeekUsage::test_real_payload_top_level_and_details_same_value_not_double_counted
E   assert 13312 == 6656
E   + where 13312 = TokenUsage(... input_tokens=6697 ... cache_read_tokens=13312 ...).cache_read_tokens
```

1 failed, 7 passed（0.33s）。测试断言正确值 6656，实际得 13312——bug 在测试 seam 上确定性复现。**该测试现已在仓库中、处于红态，待修复后转绿**（修复尚未实施，等确认）。

### 结论
四路证据闭合：代码双求和 → 真实载荷两处同值 → 聚合数据精确满足 read/2 等式且全偶 → 回归测试确定性变红。该 bug 非推测，影响范围为 `daily_token_usage.cache_read_tokens` 与 `Agent.cache_read_tokens_*` 全量虚高 1 倍；看门狗告警（用 miss/input）与主流量记账（input/output/total）不受影响。Anthropic 分支（121–139 行）存在同型双计结构，本栈未用 Anthropic，列为修复时的顺带项。
