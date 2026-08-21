# Token 估算口径统一 —— 深度分析

> 2026-08-20。Backlog T1-② 的前置深度分析。目标：把散落的 token 估算统一成单一可信口径，作为后续所有 A/B（压缩阈值、工具路由、reasoning 分级）的**测量基础**。
>
> 参考资料：DeepSeek 官方《Token & Token Usage》（`api-docs.deepseek.com/quick_start/token_usage`）、LangChain `count_tokens_approximately`（本地 `/Users/shubinzhang/Documents/UGit/langchain/libs/core/langchain_core/messages/utils.py:2244`）、OpenAI tokenizer 经验值。

---

## 0. ⚠️ 实测修正（2026-08-21，官方比例证伪，结论方向反转）

用 DeepSeek 官方 `deepseek_v3_tokenizer.zip`（`tokenizers` 库直接加载 `tokenizer.json`，无 transformers 依赖）实测，**推翻了本文 §2–§4 基于官方文档「英文 0.3 / 中文 0.6」的全部推导**：

**① 官方比例本身不准**。真实 tokenizer 实测（真实文本，非连续重复字符）：

| 文本 | 实测 tok/char | 官方说法 |
|---|---|---|
| 英文 reasoning（Clawith 历史大头） | **0.18–0.22** | 0.3（高估 ~50%） |
| 中文指令/回复 | **0.47–0.54** | 0.6（略高估） |
| JSON 结构符号 `{}"":,` `\n` 空格 | **1 token/个** | 未给 |

**② 真实偏差（json.dumps 序列化后的真实消息，DeepSeek tokenizer 真值）**：

| 场景 | `chars/3` | `bytes/4` |
|---|---|---|
| 英文 reasoning 为主（历史 70–93%） | **+69% 高估** | +26% 高估 |
| 中文为主 | −20% 低估 | **+4%** |
| 工具 schema ×10 | +42% 高估 | **+7%** |
| 多轮混合历史 | +56% 高估 | +20% 高估 |

**③ 结论方向反转**：原 §3「中文低估 → 溢出窗口」是**错的**。真实危害是 **`chars/3` 对英文 reasoning 高估 69% → 预算闸门高估历史 → effective_budget 虚小 → 过早触发压缩 → 多烧摘要调用**。且 `bytes/4` 在中文/工具 schema 场景几乎精确（+4%~+7%），明显优于 `chars/3`。

**④ 离线 tokenizer（方案 D）性能实测可行**：加载 89ms（进程级一次）+ 编码 ~0.3ms/千字符（50K 输入约 13ms），依赖仅 `tokenizers`（Rust wheel，Python 3.14 可装）。

**→ 方案修正（替代原 §5 的 B+C）**：**A（统一 `bytes/4`，零依赖一行改动，偏差 +4%~+26%）立即做；D（离线 tokenizer 精确对齐）作为后续独立 PR**。原方案 B（ASCII 0.3/非 ASCII 0.6 加权）因官方比例本身不准而作废。

**实施记录（2026-08-21）**：方案 A 已落地——`model_step_service._estimate_tokens` 与 `caller._usage_from_response_or_estimate`（input 估算）已对齐 `bytes/4`，与 run/session compactor 三处一致（预算闸门 5/6 处统一）；`token_tracker.estimate_tokens_from_chars`（output 记账兜底，usage 缺失时用、不影响预算闸门）保留 `chars//3`。146 相关测试通过、arch-guard 通过。方案 D（离线 tokenizer）待独立 PR。

---

## 1. 现状：不是 3 处，是 6 处（修正研究文档）

研究文档只列了 3 处，实际 grep 全库有 **6 处**独立的 token 估算实现，口径分裂成两派：

| # | 位置 | 口径 | 基础 | 用途 |
|---|---|---|---|---|
| 1 | `model_step_service._estimate_tokens:630` | `chars/3` | 字符数 | **预算闸门核心**：fixed/system/tool_schema/history 全部走它 |
| 2 | `run_compactor._estimate_tokens:201` | `bytes/4` | UTF-8 字节 | run 级压缩保留预算 |
| 3 | `session_context_compactor._estimate_tokens:155` | `bytes/4` | UTF-8 字节 | 会话级压缩 |
| 4 | `session_context_background._estimate_tokens:71` | `bytes/4` | UTF-8 字节 | 共享上下文触发预算（**研究文档漏掉这处**） |
| 5 | `token_tracker.estimate_tokens_from_chars:36` | `chars//3` | 字符数 | 真实 usage 缺失时的粗估 |
| 6 | `caller._usage_from_response_or_estimate:200` | `chars/3` | 字符数 | `response.usage` 缺失时 fallback |

底层都汇聚到 `estimate_multimodal_tokens`（`multimodal_content.py:281`），但 `chars_per_token`（3 vs 4）与 `utf8_bytes`（False vs True）参数不一致——即「字符数÷3」与「字节数÷4」两派。

预算闸门链路（`model_capabilities.runtime_budget:192`）：
`effective_budget = input_limit − (static_prompt_tokens + tool_schema_tokens + reserved + safety)`，其中 `static_prompt_tokens`、`tool_schema_tokens` 都是 `chars/3` 的产物。

---

## 2. 权威换算（DeepSeek 官方，Clawith 即用 DeepSeek）

DeepSeek 官方文档明文（`api-docs.deepseek.com/quick_start/token_usage`）：

> - **1 English character ≈ 0.3 token**（即 1 token ≈ 3.33 英文字符）
> - **1 Chinese character ≈ 0.6 token**（即 1 token ≈ 1.67 中文字符）

这是 DeepSeek 自家 tokenizer 的经验比例，是 Clawith（deepseek-v4-flash/pro）唯一可直接对齐的权威口径。注意官方同时强调「不同 tokenization 方式比例可变，以 API 返回的 usage 为准」——即静态换算永远有误差，真实 usage 才是真值。

---

## 3. 偏差量化（核心结论）

设文本含 E 个英文字符、C 个中文字符（UTF-8：英文 1 字节、中文 3 字节）。DeepSeek 真值 ≈ `0.3E + 0.6C`。

| 口径 | 英文偏差 | 中文偏差 | 公式 |
|---|---|---|---|
| `chars/3`（字符数÷3） | **+11% 高估** | **−44% 低估** | `(E+C)/3 = 0.333E + 0.333C` |
| `bytes/4`（字节数÷4） | **−17% 低估** | **+25% 高估** | `(E+3C)/4 = 0.25E + 0.75C` |

三个典型场景（10000 字符规模）实测：

| 场景 | 英:中 | DeepSeek 真值 | chars/3 | bytes/4 |
|---|---|---|---|---|
| reasoning 为主 | 9:1 | 3300 | 3333（+1%） | 3000（−9%） |
| 混合 | 5:5 | 4500 | 3333（**−26%**） | 5000（+11%） |
| 中文指令为主 | 2:8 | 5400 | 3333（**−38%**） | 6500（+20%） |

**关键矛盾**：`chars/3`（闸门）与 `bytes/4`（压缩器）**方向相反**——中文越重，闸门越低估（倾向溢出窗口）、压缩器越高估（倾向过度压缩）。同一份历史在两个阶段被读出两个差异巨大的数字。

---

## 4. 根因与危害

1. **闸门与压缩器互相矛盾**：`model_step_service` 判定「是否超预算触发压缩」用 `chars/3`，而 `run_compactor`/`session_context_compactor` 判定「压缩后保留多少」用 `bytes/4`。中文场景下前者低估 38–44%、后者高估 20–25%，触发点系统性失准。
2. **中文低估 → 溢出窗口风险**：system prompt / 中文指令是中文为主，`chars/3` 低估 ~44% → `effective_budget` 虚高 → 历史塞过头 → 实际 `prompt_tokens` 超 `input_limit`（请求被拒/截断）。
3. **测量基础不可信**：P4 压缩阈值 A/B、P5 工具路由的「省了多少窗口」都依赖估算，口径不一致则这些 A/B 的对照组本身有 ±20–44% 的噪声，结论失真。

---

## 5. 方案（分档 + 推荐）

### 方案 A —— 最小改动：统一到 `bytes/4`
把 `model_step_service` 等 `chars/3` 调用点全部对齐到 `bytes/4`。
- 收益：消除闸门/压缩器矛盾；`bytes/4` 对中文高估（安全侧：宁可提前压缩、不溢出窗口）。
- 缺陷：英文低估 17%、中文高估 25%，仍未对齐 DeepSeek 真值，且「高估中文」会多烧摘要调用。

### 方案 B —— 对齐 DeepSeek 官方：ASCII/非 ASCII 加权
底层 `estimate_multimodal_tokens` 增加「字符类别加权」：`token ≈ 0.3 × ASCII字符数 + 0.6 × 非ASCII字符数`（对 JSON 结构符号 `{}"":,` 等非英文 ASCII 需单独按 ~1.0 计，避免低估）。
- 收益：静态对齐 DeepSeek 官方，中英文都落在 ±10% 内，6 处统一走同一函数。
- 缺陷：仍是静态近似；JSON 结构符号、数字、标点的 token 化有残余误差。

### 方案 C —— LangChain 自适应校准（`use_usage_metadata_scaling` 模式）
用 `token_tracker.extract_token_usage` 已拿到的真实 `prompt_tokens` 反推 running 校准系数：
`scale_factor = 真实 prompt_tokens / 估算 tokens`，夹在 `[1.0, 1.25]`（LangChain 同款），乘到后续估算上。
- 收益：自适应收敛到真实 tokenizer，不依赖任何静态假设，长期最准。
- 缺陷：需跨请求维护校准状态；首次请求无历史可用。

### 方案 D —— DeepSeek 离线 tokenizer（不推荐）
官方 `deepseek_tokenizer.zip` 精确计算。
- 缺陷：引入依赖 + 每请求串行化后跑 tokenizer 的 CPU 开销，对高频预算闸门过重。

**推荐：B + C 组合**。B 作为统一静态口径（替换 6 处散落实现、对齐官方），C 作为运行时自适应兜底（吸收 B 的残余误差）。两者收敛到单一 `estimate_tokens` 入口。

---

## 6. 落地范围（实现清单）

1. 底层：`estimate_multimodal_tokens` 增加「DeepSeek 加权」模式（ASCII 0.3 / 非 ASCII 0.6 / JSON 结构符号 1.0）。
2. 6 处调用点统一收敛到同一函数，删除 `token_tracker.estimate_tokens_from_chars` 与 `session_context_compactor`/`session_context_background` 的重复 `_estimate_tokens`。
3. 新增运行时校准：真实 usage → running scale_factor（夹 `[1.0, 1.25]`），供预算闸门使用。
4. 回归测试：真实中英混合样本断言估算落在真值 ±15% 内；校准系数收敛性测试。

---

## 7. 验证方法

1. **偏差分布**：取真实多轮轨迹（skill `clawith-graph-state-triage` 导出），对比「估算 tokens」vs「真实 `prompt_tokens`」，按中英文比例分桶看偏差是否收进 ±15%。
2. **校准收敛**：连续 N 轮后 `scale_factor` 是否稳定在 `[1.0, 1.25]`。
3. **闸门正确性**：验证「估算触发压缩」的时刻与「真实 prompt_tokens 达 input_limit」的时刻对齐（不再溢出/不再过早）。
