# jcode 整库源码研究报告

日期：2026-09-05
状态：**完成**（分析基于本地仓库 `/Users/shubinzhang/Documents/UGit/jcode` HEAD `f11adb5996c541592e28519018709eebebc9fce4`，`--depth 1` 浅克隆；Clawith 侧基于 `/Users/shubinzhang/Documents/agent/Clawith/backend` 工作树实读核对）
定位：参考资料研究，非实现方案。对照 Clawith 的上下文压缩（`run_compactor`）、命令风险/审批（`autonomy_service` + `approval_requests`）、checkpoint 连接池与 token 缓存口径。

## 0. 项目概览

- **是什么**：jcode（`1jehuang/jcode`，Rust workspace，~19k★）——自称「The most RAM efficient harness / The most intelligent harness」的 coding agent harness。单仓 80+ crate，核心是一条 `jcode-base`（CompactionManager）→ `jcode-app-core`（Agent 循环）→ `jcode-tui` 的本地 harness 链路，外加 provider 适配（anthropic/openai/bedrock/gemini/copilot 等 20+ crate）、TUI 全家桶、harness-api 稳定协议层。
- **核心结论**：jcode 的「智能」与「省 RAM」是**同一套设计哲学的两面**——上下文压缩不做成「临到墙了再暴力截断」，而是**三级阈值 + 三模式策略 + 确定性护栏 + 异步后台**的分层自愈系统；命令风险不靠 LLM 法官，而是**确定性 blast-radius 分类 + 反射门**。Clawith 的 `run_compactor` 在「结构化模板 + 确定性事实管线 + Tool Exchange 原子边界 + 前缀缓存保真」上**已经明显更深**，jcode 的可迁移增量集中在：**①应急恢复分层、②主动/语义压缩策略、③命令风险的确定性前置层、④缓存口径统一启发式**。
- **对标关系**：jcode 是单进程本地 harness（无多租户、无队列、checkpoint 全在内存 + 磁盘 JSON）；Clawith 是多租户 LangGraph 平台（checkpoint 走 PG、run 走命令队列）。**两者「压缩后 provider 前缀缓存必然失效」是同构痛点**，这是最有价值的对照面。

## 1. 上下文压缩核心（jcode-compaction-core + CompactionManager）

jcode 压缩逻辑分两层：`jcode-compaction-core`（`crates/jcode-compaction-core/src/lib.rs`，1036 行，**纯函数/常量，无状态、无 I/O**）+ `jcode-base` 的 `CompactionManager`（`crates/jcode-base/src/compaction.rs`，1790 行，持有状态与触发逻辑）。

### 1.1 三级阈值 + 常量表（lib.rs）

| 常量 | 值 | 行号 |
|---|---|---|
| `DEFAULT_TOKEN_BUDGET` | 200_000（对齐 Claude 实际上下文） | `lib.rs:6` |
| `COMPACTION_THRESHOLD`（软阈值，触发后台压缩） | 0.80 | `lib.rs:9` |
| `CRITICAL_THRESHOLD`（硬阈值，同步硬压缩） | 0.95 | `lib.rs:13` |
| `MANUAL_COMPACT_MIN_THRESHOLD`（手动 /compact 下限） | 0.10 | `lib.rs:16` |
| `RECENT_TURNS_TO_KEEP`（保留最近轮次逐字） | 10 | `lib.rs:19` |
| `MIN_TURNS_TO_KEEP`（应急压缩绝对下限） | 2 | `lib.rs:22` |
| `EMERGENCY_TOOL_RESULT_MAX_CHARS` | 4000 | `lib.rs:25` |
| `EMERGENCY_IMAGE_MAX_CHARS` | 1024 | `lib.rs:31` |
| `PAYLOAD_IMAGE_CHAR_BUDGET`（413 恢复的 base64 字节预算） | 12 MiB | `lib.rs:40` |
| `CHARS_PER_TOKEN` | 4 | `lib.rs:43` |
| `IMAGE_TOKEN_COST`（图片按固定 token 计费，非 base64 长度） | 1600 | `lib.rs:58` |
| `SYSTEM_OVERHEAD_TOKENS`（system prompt + 50+ 工具定义） | 18000 | `lib.rs:63` |

**关键设计点：图片 token 计费不按 base64 长度**（`lib.rs:45-58` 注释 + `content_char_count` 的 `Image` 分支 `lib.rs:325`）。base64 长度会让估算值虚高 ~100 倍，导致「阈值被虚假触发 → 反复连续压缩却压不下来」。这一条直接对应 Clawith 多模态计费口径（见 §5）。

### 1.2 摘要模板（SUMMARY_PROMPT，lib.rs:77-85）

四段自然语言模板：`Context / What we did / Current state / User preferences`。比 Clawith 的 8 段结构模板（`run_compactor.py:65-88`）简单，但**显式要求「User preferences」独立成段**，且保留「可以之后再搜全文拿精确报错/代码片段」的提示。

### 1.3 工具调用/结果成对保护（safe_compaction_cutoff）

`safe_compaction_cutoff()`（`lib.rs:238-291`）：从初始 cutoff 出发，统计保留后缀里「有 ToolResult 但缺对应 ToolUse」的 id，**向后回溯逐步扩展保留后缀**直到每个保留的工具结果都能在同后缀找到其工具调用；找不到则返回 0（**不压缩**）。这是 jcode 侧对「压缩不得拆散 tool call/result 对」的保证，与 Clawith 的 Tool Exchange 原子边界（`run_compactor.py:350-426` `_compactable_prefix`，`tool_exchange.py` 的 `build_message_blocks`）**同构但弱一档**：jcode 只保证成对，Clawith 还保证「未结算交换是硬屏障」「受保护消息永不被摘要」。

### 1.4 三模式压缩策略（CompactionManager）

`CompactionMode`（`jcode-config-types/src/lib.rs:16-24`）：`Reactive`（默认，固定阈值）/ `Proactive`（EWMA 预测）/ `Semantic`（embedding 主题漂移）。`CompactionConfig` 默认值（`jcode-config-types/src/lib.rs:386-398`）：`lookahead_turns=15`、`ewma_alpha=0.3`、`proactive_floor=0.40`、`min_samples=3`、`stall_window=5`、`min_turns_between_compactions=10`、`topic_shift_threshold=0.45`、`relevance_keep_threshold=0.65`、`goal_window_turns=5`。

- **Reactive**：`should_compact_with`（`compaction.rs:838-857`）——`context_usage >= 0.80` 且 `active.len() > 10` 且无 pending 任务。
- **Proactive**：`should_compact_proactively`（`compaction.rs:514-548`）——对 token 快照序列算 EWMA 增量，向前投影 `lookahead_turns` 轮，投影值 ≥ 80% 阈值即提前压缩。
- **Semantic**：`should_compact_semantic`（`compaction.rs:561-601`）——embedding 历史窗口按新旧两半求均值向量余弦，低于 `topic_shift_threshold` 判主题漂移（上一主题已完结、可安全摘要）；`semantic_cutoff`（`compaction.rs:613-675`）用「最近 goal 嵌入」对每条旧消息做相关性打分，把高相关消息从「待摘要集合」里拉出来逐字保留。

**反信号守卫**（`anti_signals_block`，`compaction.rs:463-505`）：已在压缩 / 低于 `proactive_floor` / 样本不足 / 增长停滞（`stall_window` 内无增长）/ 冷却期未过——五条任一命中则**不**主动压缩。这是 jcode 对抗「压缩风暴」的机制。

### 1.5 异步后台 + 应急硬压缩（ensure_context_fits）

`ensure_context_fits`（`compaction.rs:932-1027`）是触发入口，在**每次 API 请求构造前**调用（`agent.rs:690-711`）：

- `usage >= 0.95`：同步硬压缩（`hard_compact_with`，`compaction.rs:1444-1562`）。若后台压缩在途，先等最多 15s（50ms 轮询，`compaction.rs:43-44`），等不到就**中止在途后台任务**（`task.abort()`，`compaction.rs:1501-1509`）再硬压缩。
- 否则走 `maybe_start_compaction_with`（`compaction.rs:860-925`）：`tokio::spawn` 后台摘要，完成经 `Bus::publish(CompactionFinished)`（`compaction.rs:918`）异步应用，**不打断用户对话**。

`hard_compact_with` 的核心（`compaction.rs:1444-1562`）：`turns_to_keep` 从 10 起**二分递减**到 2，每步都过 `safe_compaction_cutoff`，用预计算的 `remaining_suffix_chars` 判断剩余是否落回预算内；最终生成「应急摘要」`build_emergency_summary_text`（`lib.rs:449-492`），从被丢弃消息里**启发式提取工具名集合 + 文件引用集合**（`collect_emergency_summary_hints` / `extract_file_mentions`，`lib.rs:494-541`）。

**两处防竞态护栏**值得记录：

1. `ActiveCharEstimate`（`compaction.rs:81-126`）——滚动字符估计把「值 + dirty 标志」绑成一个结构体，强制所有变更走 `set_exact/append_exact/invalidate/reset_pending`，从类型上消灭「只更新值不清脏标志」导致 token 账目静默漂移的那类 bug（注释 `compaction.rs:71-80`）。
2. 过期后台结果丢弃（`compaction.rs:1166-1188`）——后台摘要开始时记录的 `pending_cutoff` 可能因期间发生了硬压缩而失效，应用前检测「`pending_cutoff` 会吞掉健康尾部」则**丢弃过期摘要**而非覆盖。这是「belt-and-suspenders」防重复压缩（对应 jcode 历史「kept 0 recent messages」事故）。

### 1.6 OpenAI 原生压缩（native compaction）

`Summary.openai_encrypted_content`（`lib.rs:91`）承载 OpenAI 原生 `encrypted_content`；`generate_compaction_artifact`（`compaction.rs:1675-1751`）优先 `provider.native_compact()`，失败回退 `build_compaction_prompt`（`lib.rs:138-152`）+ `complete_simple`。超限时 `discard_oversized_openai_native_compaction`（`compaction.rs:395-424`）丢弃原生载荷、降级文本摘要，避免「超大 encrypted_content 卡死会话」。

**对 Clawith（§6 详述）**：Clawith 无「原生 compaction」概念，但同样面对「压缩后前缀缓存失效」；jcode 的 `cache_tracker.reset()` + `provider_session_id=None`（`agent/compaction.rs:4-9` `note_compaction_applied`）是每次压缩后重置缓存状态的显式动作。

## 2. 应急恢复与 provider 前缀缓存口径

### 2.1 缓存口径统一（effective_context_tokens_from_usage）

`lib.rs:362-387` 是 jcode 的「上下文真实 token 数」唯一权威启发式，解决 Anthropic 与 OpenAI 对 `input_tokens` 的两种记账分歧：

- **Split 记账**（Anthropic）：`input_tokens` 只是未缓存余量，真实上下文 = `input + cache_read + cache_creation`。
- **Subset 记账**（OpenAI）：`input_tokens` 已含缓存，`cached_tokens` 是子集，**不能再加**。

判定启发式（`lib.rs:375-378`）：provider 名含 `anthropic/claude`、或 `cache_creation > 0`、或 **`cache_read > input_tokens`**（缓存读比输入还大 ⇒ 输入不可能已包含它）即判 split。注释强调侧边栏上下文显示与压缩管理器观测 token 必须共用此函数，否则二者打架（issue #441）。

### 2.2 413「请求体过大」与 token 超限分离

jcode 把两类失败显式拆开：

- **token 超限** → 正常压缩。
- **HTTP 413（请求体过大）** → 由内联 base64 图片主导，token 记账故意低估图片，普通压缩压不下去，需专门字节级恢复：`is_request_payload_too_large_error`（`lib.rs:603-612`，含独立状态码 413 判定 `contains_independent_status_code` `lib.rs:617-625`）+ `emergency_strip_large_images`（`lib.rs:638-692`，**最旧的先删**，保留尽量多近期图，每张替换为文本标记）。触发路径在 `agent/compaction.rs:192-242` `try_recover_after_payload_too_large`，恢复后重置缓存状态并重试同 turn。

应急工具结果截断 `emergency_truncated_tool_result`（`lib.rs:694-705`）保留头 1/2 + 尾 1/4，中段替换为 `[N chars truncated]` 标记，UTF-8 边界安全。

**对 Clawith**：Clawith 有前端 413 教训（上传 413），`token_tracker.py` 已做 DeepSeek 缓存双计防护（`extract_token_usage` 里「top-level 与 `prompt_tokens_details` 相同值只取一次」的 fallback，`token_tracker.py:157-171`）。jcode 的「cache_read > input ⇒ split」启发式与「413 独立字节恢复」是 Clawith 可补的两点。

## 3. 命令风险护栏（jcode-command-risk）

### 3.1 设计哲学（lib.rs docstring）

`lib.rs:1-33` 明确这是 **stage 1 的两级级联**：廉价、确定性、高召回静态过滤器，**从不调模型、从不碰网络**，安全路径零开销；stage 2（反射门）只在返回非 `Safe` 时触发。两条刻意的取舍（`lib.rs:18-25`）：

1. **按 blast radius 分类，不按命令名黑名单**——`rm -rf` 黑名单漏掉 `find -delete`/`shred`/`truncate`/`dd`/`>file`，问的是「这会摧毁什么、能否撤销」。
2. **偏向召回**——误报代价一次反射轮，漏报代价一个家目录；解析有歧义就升级而非放行。

背景：issue #604（用户因 `rm -rf ~` 丢了家目录）。

### 3.2 四级风险（lib.rs:44-69）

`RiskLevel`：`Safe`（无破坏，直接跑）/ `Low`（有界破坏，工作区内/可 git 恢复/临时目录，跑但记录）/ `Confirm`（静态无法判定目标，需模型重新对齐用户请求）/ `Catastrophic`（摧毁家目录/根/凭证，**永不执行、任何模型辩解都无法解锁**）。`is_absolute_deny`（`lib.rs:66-68`）。

### 3.3 解析管线（assess → assess_segment）

`assess`（`lib.rs:203-214`）按 `tokenize::split_segments`（`tokenize.rs:63-87`，按 `&&/||/;/|/换行/(/)/` 分段，`rm -rf a && rm -rf b` 两段都评）。`assess_segment`（`lib.rs:216-398`）关键点：

- **包装命令解包**（`lib.rs:228-277`）：`WRAPPER_COMMANDS`（`lib.rs:157-160`：sudo/doas/env/nice/xargs/command/exec/chroot/eval…）层层剥开，落到被包装的真实命令，否则 `sudo rm -rf ~` 根本看不到 `rm`。
- **shell 内联脚本递归评估**（`lib.rs:295-304`）：`sh -c "rm -rf ~"` 不是免费通行——`SHELL_COMMANDS`（`lib.rs:189`）把 `-c` 脚本再切段递归评估。
- **管道接收者升级**（`lib.rs:355-364`）：`find ~ | xargs rm` 两个分段单独看都不可见删除集合，`receives_pipe` 标记强制升级 `Confirm`。
- **重定向目标**（`lib.rs:320-348`）：`>` 截断目标也算破坏，`>>`（append）不算（`tokenize.rs:178-190`）；`/dev/null|stdout|stderr|NUL` 是安全 sink（`lib.rs:402-404`）。
- **无条件破坏但无目标** → 更可疑而非更安全，升级 `Confirm`（`lib.rs:368-378`）。

### 3.4 路径保护（paths.rs，安全关键核心）

`paths.rs:1-7` 自述「catastrophic 层是唯一不能被绕过的保护，因此写得**绝对且简单**：一小撮路径，规范化后比较，不依赖理解周围命令」。

- 保护集：`PROTECTED_CREDENTIAL_SUBPATHS`（`paths.rs:15`：`.ssh/.gnupg/.aws/.kube/.docker`，**递归**保护——删单个私钥与删目录同害）、`PROTECTED_HOME_SUBPATHS`（`paths.rs:20-28`：`.config/.jcode/.claude/.local/Documents/Desktop`，**精确匹配**——`~/.config` 保护但 `~/.config/app/stale.toml` 不保护）、`PROTECTED_SYSTEM_PATHS`（`paths.rs:31-52`）、`SYSTEM_PATHS_PROTECTED_RECURSIVELY`（`paths.rs:56-59`，`/etc/passwd` 之类）。
- **lexical normalize 防 `..` 逃逸**（`paths.rs:114-129`）：`rm -rf ~/../..` 不逃过保护检查（**故意不 symlink-aware**，见 docstring「defense in depth」）。
- `is_catastrophic_target`（`paths.rs:135-172`）与 `classify_target`（`paths.rs:175-289`）：glob（`*`/`?`）无法确定足迹 → 升级；`$`/`` ` `` 运行时变量/命令替换 → `Confirm`（`paths.rs:236-244`）；`/dev` 设备节点写入 → `Catastrophic`（`paths.rs:247-259`）。

### 3.5 反射门（gate.rs，stage 2）

`gate()`（`gate.rs:78-99`）：`Safe/Low → Allow`；`Catastrophic → Deny`（拒绝理由含「若用户真想要，须自己在 agent 外执行」）；`Confirm →` 看 `Justification.is_substantive()`——**实质性辩解**（`gate.rs:47-71`：长度 ≥ `MIN_JUSTIFICATION_LEN=25` 且不是「yes/ok/sure/proceed/…」纯肯定）则 Allow，否则 `Reflect` 返回 `reflection_prompt`（`gate.rs:107-122`）。

`gate.rs:6-19` 解释**为什么不用第二个 LLM 法官**：贵、慢、能被生产该命令的同款推理绕过去、还多一个要对齐的东西。改为「拒绝一次 + 结构化提示，逼生成模型补上它跳过的思考」——关键是**拒绝不可被简单重复满足**：盲重试同样的调用会再失败，必须提供 `justification` 说明服务的是用户的哪条请求。

## 4. harness 抽象（jcode-harness-api）

`crates/jcode-harness-api/src/lib.rs:1-14` 定位：harness 与任意 UI（TUI/桌面/Web/脚本）之间的**稳定、版本化**公共边界，刻意比内部 `jcode-protocol` 小（只放精选稳定面）。设计规则（`lib.rs:7-14`）：

- 每帧一行 JSON（NDJSON）；
- 每帧带 `v`（协议主版本）；
- 客户端**必须忽略未知字段、跳过未知事件**（枚举都带 `#[serde(other)] Unknown` 兜底）；
- 加性变更升 `API_VERSION_MINOR`，破坏性变更升 `API_VERSION_MAJOR` 且须握手协商（`lib.rs:37-39`）。

帧封套：`ClientFrame { v, id, ApiRequest }`（`lib.rs:43-50`）/ `ServerFrame { v, reply_to?, ApiEvent }`（`lib.rs:54-63`）。请求 `ApiRequest`（`requests.rs:8-202`）internally-tagged `"req"`，覆盖会话全生命周期：`Hello/ListSessions/CreateSession/AttachSession/ForkSession/SendMessage/Cancel/SoftInterrupt/GetHistory/PeekSession/Clear/Rewind/RewindUndo/PermissionResponse/Compact/SetModel/ReadFile/FindFiles/SearchText/FileStatus/SetApiKey/…`。事件 `ApiEvent`（`events.rs:8-267`）tagged `"ev"`，流式 `TextDelta/ReasoningDelta/ToolStart/ToolInputDelta/ToolExec/ToolDone/TokenUsage/TurnDone/BackgroundProgress/ConnectionPhase/…`。

值得记的两点：

- **`Compact` 是显式 API**（`requests.rs:175`），且 `Compacted` 事件（`events.rs:248-252`）明确「压缩**非同步**：daemon 在下一个安全点摘要，而非打断飞行中的 turn」——与 jcode 后台压缩语义一致。
- **`SessionInfo.transcript_bytes`**（`events.rs:290-297`）作为「会话有多大」的廉价单调代理，客户端可**不拉全量 transcript** 就按大小排序——对 Clawith 会话列表/成本估计有参考价值。

## 5. 检索与模糊匹配（jcode-embedding + jcode-fuzzy）

### 5.1 本地 embedding（jcode-embedding）

`all-MiniLM-L6-v2`（`lib.rs:10`）经 **tract ONNX** 推理（非 torch），`EMBEDDING_DIM=384`、`MAX_SEQ_LENGTH=256`（`lib.rs:85-86`）。要点：

- `Embedder.embed`（`lib.rs:172-240`）：mean-pooling + L2 归一化。
- **输入按名字绑定**（`lib.rs:98-121` `classify_input` + `input_plan`）：不同 exporter 的输入顺序（MiniLM input_ids 在前，e5/bge attention_mask 在前）与 dtype（f32 vs i64）不一致，**按模型声明而非位置绑定**。
- `CrossEncoder` 重排器（`lib.rs:285-363`）：一阶段召回（recall-5）后对 (query, passage) 联合打分重排。
- `top_k_scored`（`lib.rs:44-84`）用**最小堆**维护 top-k，O(n log k)；`find_similar`（`lib.rs:403-419`）阈值过滤 + top_k。
- `download_model`（`lib.rs:425-473`）：缺模型时 reqwest 阻塞下载到 `~/.jcode/models/…`，超时 300s。
- 数值稳定性回归测试（`lib.rs:520-570`）：固定参考向量，防 tract 升级导致持久化 embedding 静默不可比（`model_id` 不变检测不到数值漂移）。

### 5.2 typo 容忍模糊匹配（jcode-fuzzy）

共享于 TUI/桌面 UI 的 DP 匹配器（`lib.rs:1-6`）：子序列匹配 + 有界替换/相邻换位/多打字符；`MATCH/CONSECUTIVE/BOUNDARY/FIRST/GAP/LEADING_GAP/SUBSTITUTION/DELETION/TRANSPOSITION/EXACT` 计分（`lib.rs:12-21`）。工程要点：

- `prefilter`（`lib.rs:140-214`）：ASCII 位掩码 + 多重性计数两段廉价拒绝，避免对绝大多数不匹配条目跑 DP。
- `PositionSummary`（`lib.rs:60-79`）分配零开销跟踪器 vs `Vec<usize>`（高亮），`PreparedTokenQuery`（`lib.rs:524-581`）**每次击键复用 DP 草稿行**。
- `fuzzy_score_tokens`（`lib.rs:514-516`）：多词查询**每词必须匹配某字段**，防弱匹配跨「模型/提供商/详情」列缝合（`lib.rs:509-513`）。
- `command_fuzzy_match`（`lib.rs:654-656`）：斜杠命令匹配锚定首字母。

**对 Clawith**：Clawith 后端**无本地 embedding / 模糊检索层**——`verification.py` 里 `fuzzy` 只是注释（`verification.py:265`）；检索走 codebase-memory 知识图谱（MCP/CLI）与跨会话 list 检索（`cross_session_retrieval.py`）。jcode 的「本地 384 维 embedding + CrossEncoder 重排」对 Clawith 若要本地化工具/记忆检索是可直接参考的成熟最小实现。

## 6. Clawith 侧对照（真实行号核实）

### 6.1 压缩（run_compactor.py ↔ jcode compaction-core）

| 维度 | jcode | Clawith `run_compactor.py` |
|---|---|---|
| 触发阈值 | 80% 软 / 95% 硬 / 10% 手动（`compaction-core/lib.rs:9-16`） | 80% 水位（`reaches_compact_high_watermark`，`run_compactor.py:157-175`） |
| 压缩后水位 | 无显式低水位，靠 `remaining <= budget` 兜底 | **50% 低水位硬约束**（`compact_if_needed` 末尾 `run_compactor.py:1364-1370`） |
| 摘要预算 | 无独立预算 | 25% summary 帽 / 25% recent 帽（`compact_context_budgets`，`run_compactor.py:146-154`） |
| 摘要模板 | 4 段自然语言（`lib.rs:77-85`） | 8 段结构化 Markdown + 逐段规则（`run_compactor.py:56-120`） |
| 成对保护 | `safe_compaction_cutoff`（`lib.rs:238-291`） | Tool Exchange 原子边界 + 未结算屏障（`_compactable_prefix`，`run_compactor.py:350-426`） |
| 确定性事实 | 应急摘要启发式提取工具/文件（`lib.rs:449-541`） | **确定性 completed_actions / files_read 管线**（`build_completed_actions` `run_compactor.py:588-629`、`build_files_read` `:658-709`） |
| 摘要失败降级 | 回退文本摘要（`generate_compaction_artifact` `compaction.rs:1733-1742`） | 确定性降级摘要（`_degraded_summary` `run_compactor.py:1032-1084`） |
| 不缩小防护 | 无显式 | **shrink 安全网 + 二分切批**（`_compact_batch` `run_compactor.py:1123-1177`） |
| 循环检测 | 无 | **压缩健忘循环检测**（`detect_loop`，前缀/工具指纹 + `compaction_since_last_prefix`，`run_compactor.py:918-947`） |

**结论**：Clawith 压缩**已系统性更深**（低水位、确定性事实管线、Tool Exchange 原子边界、shrink 安全网、循环检测都是 jcode 没有的）。jcode 的增量在**策略层**（proactive/semantic 模式、反信号守卫）与**应急分层**（95% 同步硬压缩 + 413 独立恢复）。

### 6.2 前缀缓存（token_tracker.py ↔ jcode 缓存口径）

Clawith `token_tracker.py` 已做 jcode 同款工作且更细：

- DeepSeek `prompt_cache_hit_tokens` 双计防护（top-level 与 `prompt_tokens_details.cached_tokens` 只取一次，`token_tracker.py:157-171`）——对应 jcode `effective_context_tokens_from_usage` 的 subset 判定。
- 低命中 watchdog（`_maybe_warn_low_hit`，`token_tracker.py:80-102`）：`cache_miss / input >= 0.5` 且 `cache_miss >= 1024` 才告警，**30 分钟冷却**（`token_tracker.py:53`）压掉「压缩/缓存淘汰导致的锯齿尖峰」，只抓「持续低命中 = schema 重排/prompt 改动」的真断裂。这段注释（`token_tracker.py:38-52`）**显式承认压缩必然打爆前缀缓存**，与 jcode `cache_tracker.reset()`（`agent/compaction.rs:5`）是同一认识。

### 6.3 命令风险/审批（autonomy_service.py ↔ jcode command-risk）

Clawith 是**自治策略 + 人工审批**，jcode 是**确定性静态分类 + 反射门**，两者是互补的两条线：

- Clawith `check_and_enforce`（`autonomy_service.py:51-166`）：L1 自动执行+记录 / L2 通知+执行 / L3 建 `ApprovalRequest` 阻断。运行时审批身份 `uuid5(run_id, "runtime-approval:{action_type}:{tool_call_id}")`（`autonomy_service.py:30-49`）实现**同一 (run, tool_call) 幂等**。
- 审批通过后**精确续跑原 Run**：`_runtime_resume_details`（`autonomy_service.py:279-307`）+ `RuntimeCommandIntake.resume_run`（`autonomy_service.py:203-234`），与 open-swe 的「interrupt ≠ failure」一致。
- 权限：仅 agent 创建者或 platform_admin 可决议（`autonomy_service.py:182-186`）。

**缺口**：Clawith 的 `action_type` 是**粗粒度工具级**（`delete_files` 等，见 `_execute_approved_action` `autonomy_service.py:317-390` 按 tool_name 分发）；jcode 的 `command-risk` 是**命令级 blast-radius**（路径递归保护 + wrapper 解包 + 管道升级）。Clawith 缺一层「确定性前置过滤」，尤其对 `bash`/`shell` 类工具的参数内容本身不做静态风险分级。jcode 的 `Catastrophic` 绝对拒绝 + `Reflect` 反射门（`gate.rs`）可直接作为 Clawith bash 工具执行前的廉价前置层。

### 6.4 harness 分层（harness-api ↔ Clawith）

jcode `harness-api` 是**单进程本地 daemon 的稳定 NDJSON 协议**；Clawith 是 **HTTP/WS + LangGraph checkpoint + 命令收件箱**三层（`agent_run_commands` 收件箱 + `RuntimeCommandIntake` + WS 事件流）。可迁移的是**契约纪律**：jcode 的 `v` 版本字段 + `Unknown` 前向兼容 + 加性/破坏性分版本（`lib.rs:7-14`）对 Clawith 的 WS 事件 schema 演进（新增事件类型、未知字段忽略）是成熟先例。

### 6.5 RAM/资源占用（jcode 单进程 ↔ Clawith 连接池）

jcode 的省 RAM 来自「单进程、内存消息、无独立 DB 池」——与 Clawith 多租户架构**不可直接比**（README 宣称 27.8MB 是本地 harness 场景，`README.md:57-79`）。Clawith 的对应工程努力在**连接预算**：

- SQLAlchemy 主池 `DB_POOL_SIZE=8 / DB_MAX_OVERFLOW=4`、连接回收 `DB_POOL_RECYCLE_SECONDS=1800`（`config.py:110-116`，注释明确「chat 延迟不随池增大、过大基础池拖垮整库」）。
- 进程级共享 checkpoint 池 `CHECKPOINT_POOL_MIN_SIZE=1 / MAX_SIZE=4 / TIMEOUT=10s`（`config.py:122-124`），由 `get_shared_checkpoint_pool`（`checkpointer.py:213-242`）惰性打开、进程级复用，替代 `AsyncPostgresSaver.from_conn_string` 每次新连接的模式（注释 `checkpointer.py:199-205` 明确这是「checkpoint 需求钉死在固定预算内」）。
- 命令并发 `AGENT_RUNTIME_COMMAND_CONCURRENCY=10` + 租约 `CLAIM_TTL=60s / RENEW=20s`（`config.py:174-176`）。

jcode 侧真正可迁移的「省资源」手法是**agent-runtime 的 InterruptSignal**（`jcode-agent-runtime/src/lib.rs:33-118`）：`AtomicBool`（同步读）+ `tokio::Notify`（异步唤醒）+ **单调 epoch 计数**。`reset_if_epoch`（`lib.rs:78-90`）只重置「捕获 epoch 之后无新 fire」的信号，消除工具执行期间的忙等（spin-loop）与「延迟 reset 抹掉新 cancel」的竞态（issue #428）。这对 Clawith 的 cancel/resume 语义（`cancel_run` 两层修复、claim 租约）是零成本的设计参考。

## 7. 可迁移点 → Clawith 映射

| # | jcode 机制（文件:行） | Clawith 对标点 | 可借鉴要点 |
|---|---|---|---|
| 1 | 图片固定 token 计费 `IMAGE_TOKEN_COST`（`compaction-core/lib.rs:58,325`） | 多模态 token 计费 | 图片按分辨率计费而非 base64 长度，防压缩阈值被虚高触发 |
| 2 | 413 独立字节恢复 `is_request_payload_too_large_error`+`emergency_strip_large_images`（`lib.rs:603-692`） | 前端 413 教训 / 多模态 | 把「请求体过大」与「token 超限」分离，最旧图片先删+文本标记，恢复后重试同 turn |
| 3 | 缓存口径统一 `effective_context_tokens_from_usage`（`lib.rs:362-387`） | `token_tracker.py` 缓存记账 | 「`cache_read > input` ⇒ split 记账」启发式，侧边栏与压缩观测共用同一函数 |
| 4 | 三级阈值 80/95/10（`lib.rs:9-16`）+ 95% 同步硬压缩（`compaction.rs:932-1027`） | `run_compactor.py` 80% 水位 | 增加「95% 应急同步硬压缩」兜底 + 「手动 /compact 下限」 |
| 5 | 主动压缩 EWMA 预测 + 反信号（`compaction.rs:463-548`） | 无（Clawith 纯响应式） | proactive 模式：floor/min_samples/stall/cooldown 五重反信号防压缩风暴 |
| 6 | 语义压缩 topic-shift + 相关性保留（`compaction.rs:561-675`） | 无 embedding 检索层 | 本地 MiniLM 384 维 + 主题漂移检测，作为可选压缩策略 |
| 7 | 工具调用/结果成对保护 `safe_compaction_cutoff`（`lib.rs:238-291`） | Tool Exchange 原子边界（已更强） | jcode 版本可作「轻量前置」参考，Clawith 无需替换 |
| 8 | 应急摘要提取 tools/files（`lib.rs:449-541`） | 确定性 completed_actions/files_read（已更强） | jcode 启发式是「无 ledger 时的降级」，Clawith 已有确定性管线 |
| 9 | 滚动估计 dirty-flag 捆绑 `ActiveCharEstimate`（`compaction.rs:81-126`） | token 估算 | 值+脏标志同结构，杜绝「只更新值不清脏」的账目漂移 |
| 10 | 过期后台摘要丢弃（`compaction.rs:1166-1188`） | 部署杀→重放→分叉撞账本教训 | 后台任务结论应用前校验 `pending_cutoff` 仍对齐当前偏移，否则丢弃 |
| 11 | 命令四级风险 + blast-radius 分类（`command-risk/lib.rs:44-69,203-398`） | `autonomy_service.py` 粗粒度 action_type | bash 工具执行前加一层确定性静态风险分级 |
| 12 | 反射门 gate `Reflect`（`gate.rs:78-122`） | L3 人工审批 | Confirm 不硬阻断，要求模型 `justification` 对齐用户请求，拒绝「不可被重复满足」 |
| 13 | 路径递归保护 + lexical normalize 防 `..`（`paths.rs:114-172`） | workspace 边界 / 删除审批 | 凭证/家目录/系统路径递归保护，纯词法解析不碰 FS |
| 14 | 包装命令解包 + shell 内联脚本递归评估（`command-risk/lib.rs:228-304`） | 命令风险（若做确定性层） | `sudo rm -rf ~`、`sh -c`、`xargs rm` 三类绕黑名单的消除 |
| 15 | InterruptSignal epoch 防丢取消（`agent-runtime/lib.rs:33-118`） | cancel/resume 两层修复、claim 租约 | `reset_if_epoch` 只重置无新 fire 的信号，消除忙等与「延迟 reset 抹新 cancel」 |
| 16 | harness-api 版本化 NDJSON + Unknown 前向兼容（`harness-api/lib.rs:7-14`） | WS 事件流 schema 演进 | `v` 版本 + 未知事件/字段忽略，加性/破坏性分版本 |
| 17 | `SessionInfo.transcript_bytes`（`harness-api/events.rs:290-297`） | 会话列表/成本估计 | 廉价单调体积代理，避免拉全量 transcript 排序 |

## 8. 局限（诚实记录）

- **架构不同构**：jcode 是单进程本地 harness（消息在内存、checkpoint 是磁盘 JSON），无多租户、无队列、无审批流；Clawith 的 PG checkpoint / 命令收件箱 / 连接池在 jcode 里无对应物。RAM 对比（27.8MB）不可迁移，只在「本地嵌入、单进程」前提下成立。
- **压缩主体 Clawith 已更深**：低水位 50%、确定性 completed_actions/files_read、Tool Exchange 原子边界、shrink 安全网、循环检测，jcode 都没有。本文档 §7 的压缩项是**策略与应急分层**的增量，非重构方向。
- **命令风险 jcode 是「防御纵深非沙箱」**（`command-risk/lib.rs:27-32` 自认）：`sh -c "$(printf ...)"` 可击败任何静态解析器；其 `catastrophic` 层只是「小、绝对、基于路径」的硬拒绝。Clawith 若要引入，须定位为「bash 工具的前置过滤 + 反射提示」，**不可替代**人工 L3 审批与沙箱。
- **本次未深入**：20+ provider 适配 crate（`jcode-provider-*`）、TUI 全家桶（`jcode-tui-*`，20+ crate）、`jcode-swarm-core`（多 agent 编排）、`jcode-plan`/`jcode-overnight-core`（计划/过夜任务）、telemetry-worker、SDK。`jcode-gateway-types`（19 行）仅设备配对类型，模型网关本体未读。
- **`gateway-types` 名实不符**：crate 里只有 `PairedDevice`/`PairingCode`（`gateway-types/lib.rs:1-19`），真正的模型网关路由在 `jcode-provider-core` 与 `jcode-schema-dialect`，本次未深挖。
