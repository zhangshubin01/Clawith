# Android 重构任务慢（9 分钟修 4 个编译错误）—— 深度分析与参考资料对照

> 2026-08-21。run `8c36242c`（Android 工程师05 重构 credito-mx，17:49:55→17:59:11，
> delivered）。用户反馈「任务太慢，修复也慢得很」。本文做数据级分解 + 参考资料对照
> （[[reference-projects]] 本地源码 + DeepSeek/Anthropic 官方文档），给出分级优化方案。

## 1. 数据事实（9 分 12 秒的构成）

| 维度 | 数据 |
|---|---|
| 模型调用 | **40 轮**（17:49:59→17:59:00），每轮输入 ~65k tokens |
| 编译 | 5 次共 ~52s（失败 8.0/8.9s、成功 14.5/7.0/13.8s；配置缓存复用良好） |
| 工具调用（落盘 31 个） | read_file ×11、execute_code ×4、edit_file ×4、list_files ×3、find_files ×2、write_file ×2、android_compile ×3（+2 次失败） |
| 探索:修改 | **20:6 ≈ 3.3:1**（read/list/find/execute vs edit/write） |
| 长间隔 | 17:52:58→17:54:47（109s）、17:55:22→17:56:40（78s） |
| 当日该 agent token | input 11,236,302（全天）；cache_read 18,605,056 |

## 2. 慢的四层根因（按贡献排序）

### L0 模型行为层（最大头，~5-6 分钟）

- **修复不彻底**：第一次失败给出 4 个真实错误（AppNavHost ×2、DoubleType ×2），模型
  只修 AppNavHost 两处就重新编译 → 第二次失败还剩 DoubleType ×2 → 再修一轮。一次可
  改完的分成两轮，多烧 1 次失败编译 + ~10 轮模型调用。
- **路径猜测浪费**：前 5 个文件工具用**错误路径** `workspace/credito-mx/app/...`（缺
  `android/` 段），read_file 失败后未立即纠正，继续同前缀试 find_files/list_files。
  ——与 20260819「无前缀路径空转事故」同型：模型把「workspace 中的相对路径」理解成
  「相对 workspace/ 目录」。L1/L2 路径契约已落地但本次仍有 4-5 个工具调用浪费。
- **API 查证绕远路**：DoubleType 是 Navigation Compose 的 NavType 子类型，模型不确定
  API → **4 次 execute_code 下载/解压 navigation sources jar 查源码**（两个 78-109s
  长间隔即此段）。行为合理但耗时巨大；若 skill/上下文里有一句「import
  androidx.navigation.NavType.DoubleType」就省 2 分钟。
- thinking 默认 **high** effort：每轮先推理再动，40 轮叠加。

### L1 上下文层（~1-2 分钟）

- 65k 输入 ×40 轮。read_file 回填**全文件**（无窗口/分页），历史膨胀快。
- DeepSeek KV 缓存（官方 guides/kv_cache）：**前缀完整匹配才命中**，缓存重建「takes
  seconds」；消息中段任何变动（repair 注入、动态块、控制消息）使前缀断裂 → 全价
  重算。本 run 有 2 次长间隔与缓存失效/重建吻合。perf(runtime) 缓存友好布局
  （ff594acd）方向正确，但「历史尾部每轮追加 tool result」使其尾部前缀单元每轮变化。

### L2 配置层

- 34 个工具 schema（对 Android 重构任务核心 ~15 个够）→ 每轮输入多几千 token。
- `read_file` 无窗口参数 → 大文件全量回填。
- `jina_read` 等与该 agent 无关的工具也在 schema 里（本 run 还失败 1 次）。

### L3 架构层（战略项）

- Clawith 用 chat completions **每轮全量重发历史**；对照 codex 用 Responses API
  （服务端保持会话状态、增量发送，codex 快的关键架构差异）。DeepSeek 官方同样支持
  Responses API。但 Clawith 的消息布局（repair 注入/动态块/控制消息）与增量语义冲突，
  迁移代价大——列为战略项，不做短期动作。

## 3. 参考资料对照（2026-08-21 核实）

| 来源 | 结论 | 对 Clawith 的启示 |
|---|---|---|
| Anthropic《Building Effective Agents》 | 「Agents trade latency and cost for better task performance」；能分解成固定子任务就用 workflow | 编译-修复循环高度可结构化：错误列表→批量修复→再编译，应在**平台提示词层**引导，而非纯靠模型自律 |
| DeepSeek《Context Caching》 | 前缀完整匹配才命中；请求边界/公共前缀/固定间隔持久化；缓存构建秒级 | 保持前缀稳定收益直接（本 run 40 轮 × 65k，命中率高则省大量重算） |
| SWE-agent（本地源码） | ACI 核心=**search 与 edit 分离 + 窗口式查看**，专治「模型反复 open 文件」 | Clawith read_file 全量回填无窗口——可加 max_chars/窗口参数，或引导先 search 定位再读 |
| codex（本地源码 models.json/compact.rs） | `model_auto_compact_token_limit` + 上下文耗尽时「写 notes 检查点 + new_context」 | Clawith 已有 compactor+compact-first gate（R1），机制对齐；可考虑**更早触发**压缩而非等 65k |
| OpenAI《A practical guide to building agents》 | 上下文工程是核心：工具裁剪、上下文清理 | 34 工具裁剪 + 每轮输入瘦身 |

## 4. 分级优化方案

### 快赢（小改动，本周可做）

1. **编译失败文案引导「一次性修复全部错误」**（`_format_android_build_failure` 错误
   列表后加一句）——直接消灭本 run 的第二轮失败编译 + ~10 轮模型调用。风险≈0。
2. **路径契约强化**：L2 `describe_path_failure` 的 workspace 前缀建议更靠前/更醒目
   （本 run 证明模型仍会连试 4-5 次错误路径）。改动小。
3. **read_file 结果回填加窗口上限**（如默认 max_chars 或「前 N 行+后 N 行」标注），
   历史膨胀减速。需评估对已有行为的影响。

### 中期（需小实验验证）

4. **工具裁剪**：该 agent 34 → ~15 核心工具（读/写/编辑/搜索/编译/execute_code/
   list 等），schema 减 ~40%。需先列 34 清单与使用频率（台账可查）。
5. **reasoning_effort 降档 A/B**：默认 high；DeepSeek 官方映射 medium→high，只有 low
   真降。用 2-3 个同类任务对照（耗时/一次修复率/总轮数）再定。
6. **agent 级知识 skill**：给 Android 工程师补「Navigation Compose 常用 API」类
   skill，减少「下载 jar 查源码」型查证（本 run 直接省 2 分钟）。

### 战略（记录在案，不动）

7. chat completions → Responses API 增量上下文（codex 同款架构）。与修复协议/消息
   布局冲突，需 ADR。
8. 前缀稳定专项：把「每轮变动的尾部」与「稳定前缀」物理隔离到 Responses API 或
   缓存单元边界（依赖 DeepSeek 缓存持久化规则）。

## 5. 建议顺序

快赢 1+2 立即做（合计 <1 小时改动+测试+部署，本类任务预计省 30-40% 轮次）；
4+5+6 各需数据/实验，逐项出结论再实施。
