# 编号指代断裂修复方案：呈现同源 + 解析优先级 + 兜底消歧（R5 v2）

- 日期：2026-09-04
- 状态：方案待用户确认（设计树 D1–D5 见 §6；确认前不动手实施）
- 事故对象：run `ce976e4c`（agent 950a1943「Android 工程师 07」，session 95767fe5，2026-09-04 06:04:12Z，部署 57c2e5b2→369cc4a9）
- 观察/先例：`docs/analysis/2026-09-03-compactor-loop-dc557d91.md`（「那执行 1→2→3→4（P1）」同型指代）、`docs/technical-plans/20260903-compaction-progress-anchor.md`（R3 链上下文）、`docs/analysis/2026-09-04-compaction-reference-implementation-comparison.md`
- 记忆：`settlement-channel-crash-and-number-reference`（双层根因+转义坑）、`plans-compare-reference-materials`（出方案两步走）、`direct-chat-run-boundary-fix`（user 角色合成消息措辞）

## 1. 问题与证据

用户发「执行 2」，agent 先按 65 项权威清单映射到第 2 项（「宿主侧 .git 完好」——核验结论，无可执行动作），绕 2 步对话才纠正到「优化项授权清单」（按键防抖等）并开始读源码。同 run 另有 D 结算崩溃（已修 369cc4a9），本文只处理编号指代层。

**同一时刻并存三套编号**（均已取证）：

| 编号体系 | 第 2 项所指 | 来源 |
|---|---|---|
| 权威清单 `4e6db819`「还有哪些待办？」（65 项，06:04 落盘清单.md） | 宿主侧 .git 完好 | R1 确定性落盘 + R3 注入 |
| agent 06:03Z 口头回复的自编 1–9 清单 | 优化项授权清单 | 对话历史（未落盘） |
| 授权清单子项 2–6（子编号） | 按键防抖 | 口头回复内嵌套 |

**关键在场性事实（git/探针实证）**：
- R3 注入正常：run ce976e4c 输入第 6160 字节处有 R3 注入块，65 项注入 20 条标题索引（探针实测 `retrieve()` 命中指针 4e6db819 + section）。
- R5 条文在场：`LIST_NUMBERING_CONTRACT` 引入于 `96535129`（2026-09-01），早于事故且当前部署（369cc4a9）包含——条文在系统提示 `model_step_service.py:1378`（`static_prompt + _MESSAGE_LAYOUT_NOTE + LIST_NUMBERING_CONTRACT`）。
- R1 落盘正常：65 项清单 06:04 写入清单.md 第 8 行起；但口头 1–9 视图**未落盘**（归因分支见 §7 V1）。

## 2. 根因分析

**根因一（呈现侧）：编号命名空间漂移。** agent 在 06:03Z 回复中呈现了自起编号的聚合视图（1–9），与清单.md 文件编号（65 项）分叉；用户自然采纳了对话里最近看到的编号。R1 只捕获 run 边界 closing 内容中满足 `N. 标题 — 说明` 格式的行——1–9 视图要么不在 closing 内容、要么格式不符被 parse 跳过（V1 待定），因此漂移编号未进入任何权威文件。

**根因二（解析侧）：R5 无优先级、无消歧。** 现行条文要求「用户以编号引用清单时，以上下文/历史检索注入的清单条目为准执行，不得自行重排或猜测候选」——agent **忠实执行**了条文：把「2」映射到注入的 65 项清单第 2 项，得到无动作结论。条文防住了「猜」，但没给「多套编号并存时谁优先、无法唯一确定怎么办」的规则，等于指令模型在歧义下静默选一个。

**问题分类（参考资料口径）**：how_to_fix_your_context 四类 failure modes 中属 **Context Confusion**——同一对象（待办项）以多套编号进入上下文，模型被迫二选一；不是上下文缺失（poisoning 的反面）。

## 3. 参考资料对照（显式，2026-09-04 实读取证）

八仓库本地源码实读 + 官方 notebook 实读，逐项映射：

| 参考 | 事实（file:line 级） | 对本方案的约束 |
|---|---|---|
| gptme（最强先例） | `tools/todo.py:68-71` 整数 ID 单调递增、删除不复用；`:136` **展示编号==存储 ID**；`:237` 查无 ID 显式报错不猜 | 「编号即身份」：呈现编号必须与权威存储编号一致 |
| open-swe | `agent/thread_ids.py:1-7` 身份由**外部稳定号**（issue/PR 号）确定性派生，绝不内部自编数字 | 编号身份取自稳定外部源——清单.md 文件编号 |
| mem0 | `configs/prompts.py:442/457` 更新沿用既有 ID；`:472` V3 ADDITIVE 只追加不覆盖 | 编号延续、只增补不重排（R1 append-only 已同构） |
| letta-code | `prompts/letta.md:28` 一块一文件，frontmatter name+description 为标识，按路径寻址；onboarding `[x]` 打勾 | 一个对象一个规范位置=一个编号命名空间（清单.md） |
| codex / gemini-cli | `plan_tool.rs:17-28` step 文本+顺序无 id；`write-todos.ts:29-34` description 身份、整表覆盖、编号渲染时重算 | 反例：不做编号回指也是合法设计；但 Clawith 已选编号交互（R1），只能绑稳定源，不能推翻 |
| how_to_fix_your_context | `README.md:37-41` 四 failure modes；修法=稳定标识+外置化（`:89` Tool registry UUID mapping、`:141` Offloading） | 本例=Context Confusion；修法同构=编号外置到清单.md 单一命名空间 |
| context_engineering | README 4 策略 Write/Select/Compress/Isolate；`1_write_context.ipynb` namespaced key 寻址 | Write/Isolate：清单是上下文外稳定对象，回指落稳定键 |
| 触发-消解分离（R3 教训，deepseek-harness 佐证） | 「正则词集解析用户意图」不是任何参考项目做法；行业分工=规则只召回、LLM 做语义、不确定就 no-op/追问 | **本方案零新增解析代码**：只改条文，不碰用户消息解析 |
| direct-chat-run-boundary-fix（Clawith 教训） | user 角色合成消息措辞必须过去式/疑问句，禁祈使（「目标：」被当新指令） | 兜底追问必须是疑问句+逐字引用清单原文 |
| deepseek-harness compaction 文档 | 「指代原话逐字保留」 | 复述候选条目时逐字引用文件原文，禁止转写 |

**防退化声明**：本次对比覆盖本地源码库（gptme/open-swe/mem0/letta-code/codex/gemini-cli 实读）+ 官方方法论文档/notebook（how_to_fix_your_context、context_engineering 实读）+ 已实读的压缩对照文档（dsh/deepagents 摘要引擎）。**评估基准类（SWE-bench/Terminal-Bench/RE-Bench）无「多轮人机编号指代」直接参考**——基准面向单次任务执行而非持久清单交互，显式声明未覆盖。

## 4. 方案选项与推荐

### Option 1（推荐）：R5 v2 条文修订——纯 prompt，零新机制

改 `list_persistence.py:51-53` 的 `LIST_NUMBERING_CONTRACT`，新增三条（草案全文）：

```
# Numbered Lists

如产出编号清单，必须每行 `N. 标题 — 一句话说明`；同一清单重问时延续原编号、只增补不重排。

- 编号同源：向用户呈现编号清单时，编号必须与该清单在 memory/清单.md 中的既有编号一致；确需呈现子集/聚合视图时标注源编号（如「第 6–10 项：按键防抖、无障碍、键盘输入、横屏适配、设置项」），绝不从 1 重新编号；清单尚未落盘时，先写入 memory/清单.md（延续该会话既有清单编号）再以文件编号呈现。
- 解析优先级：用户以裸编号（如「执行 2」）引用清单时，按次序解析：①本对话中最近一次向用户呈现的编号清单；②历史上下文/检索注入的清单条目。均以清单文件中的编号与条目原文为准，不得自行重排、猜测或补造候选。
- 兜底消歧：两套编号并存且无法唯一确定所指时，不得静默选定执行——先复述候选条目（编号+标题，逐字引用清单原文）并向用户确认「执行 2 是指『…』还是『…』？」，得到确认后再动工；查无该编号时如实说明并追问，绝不顺延到邻近编号。
```

**生效机理（对事故轨迹的重演）**：①同源条款下，06:03Z 型回复只能以「第 6–10 项」呈现授权项——「执行 2」不再产生；②即便产生，优先级条款把 ①（对话内最近呈现）提到注入清单之前，且最近呈现与文件同源后二者不再冲突；③残余歧义由兜底条款转为一次明确追问，而不是绕 2 步的错误执行。三条互為纵深，任一失效不堵死。

### Option 2（不采纳）：平台确定性改写/解析

对用户消息做编号提取正则→强制映射到清单文件，或改写 agent 回复呈现。**违反触发-消解分离教训**（2026-09-03 词集补盲已被否：打永远打不完的地鼠），且已呈现给用户的内容不可改写。仅当 Option 1 经 eval 证实失效时才重新考虑。

### Option 3（可选增量，默认不做）：呈现漂移可观测性

R1 merge 时若 incoming 条目标题与既有 section 高度相似（重呈现签名）→ 不发新编号、记 Langfuse event + WARNING。价值=量化漂移频率、给条文是否生效提供数据；但事故中的 1–9 视图根本未被 R1 捕获（parse 跳过），此检测器测不到该形态。**列为 phase 2 观察项，由 eval 结果决定是否实施。**

## 5. 测试策略

1. **条文断言**（`backend/tests/test_list_persistence.py`）：`LIST_NUMBERING_CONTRACT` 含三条款关键词断言（「编号同源」「解析优先级」「兜底消歧」+ 追问疑问句样例）——先例：run_compactor 摘要指令 precedence 文案断言。
2. **系统提示组装回归**（model_step 现有测试）：`static_prompt + _MESSAGE_LAYOUT_NOTE + LIST_NUMBERING_CONTRACT` 组装不变；契约变长不影响组装结构。
3. **回归**：`test_list_persistence.py` / `test_cross_session_retrieval.py` 全过；全量 pytest 基线 3614 passed。
4. **eval 用例（平台化，待 D5）**：把 ce976e4c 场景做成 run-loop-eval 用例——注入 65 项清单 + 对话内 1–9 呈现 + 用户「执行 2」→ 期望：按 ① 解析到「优化项授权清单」或发起追问，**不得**静默映射到「宿主侧 .git 完好」。手法见记忆 `skill-creator-run-loop-eval`（DeepSeek 端点 env、源码覆盖 agent_data 旧版脚本、eval set 抽取）。
5. **实施前验证 V1**：读 run 95767fe5 的 06:03Z closing answer 原文（Langfuse trace 或容器探针），确认 1–9 视图未落盘的归因分支（a. 不在 closing 内容／b. 行格式缺 ` — ` 被 `parse_numbered_list` 跳过）。两分支均已由方案覆盖，不阻塞实施，但结论决定 Option 3 是否值得做。

## 6. 设计树与决策点（待用户确认后实施）

- **D1** 采纳 Option 1（R5 v2 三条款）？→ 推荐：是。
- **D2** 条文原文按 §4 草案全文定稿？（用词需用户过目——模型可见契约变更）
- **D3** 兜底触发口径：仅「无法唯一确定」时追问 vs 任何两套编号并存都先列候选再执行？→ 推荐前者（少打扰；同源条款生效后两套并存应属罕见）。
- **D4** 是否同步做 Option 3 漂移可观测？→ 推荐：暂不做，先条文 + eval 观察。
- **D5** eval 用例是否纳入 run-loop-eval 平台？→ 推荐：纳入（1 个 case）。

## 7. 实施与门禁（D1–D5 确认后）

1. 改 `list_persistence.py` `LIST_NUMBERING_CONTRACT`（仅文案）；`backend/AGENTS.md` 登记 model-visible contract change（文件注释已明示此纪律）。
2. 测试按 §5 落地；ruff / pyright 基线 / `scripts/arch-guard.sh` / 全量 pytest 全过。
3. 部署走 skill `clawith-prod-deploy`：清华源 export 覆盖、回滚标签先打、check-inflight-runs 空才部署、红线不灰度全量一步、验收清单照跑（沙箱冒烟等）。
4. 上线后观察：D 结算+compaction 正常、无 run_failed；抽查真实「执行 N」类指令的消歧行为。

## 8. 风险

- **prompt-only 约束力有限**：模型仍可能漂移编号 → eval 量化；持续漂移则激活 Option 3 或「呈现即落盘」工具化（结构性兜底，另行立项）。
- **条文成本**：系统提示固定 +约 200 字/run，可忽略。
- **追问过度打扰**：由 D3 口径限定在「无法唯一确定」。
- **「先落盘再呈现」引入 agent 手写格式风险**：`parse_list_file` 对坏块降级 raw 保留、不丢数据；编号一致性靠 eval 观察。
- **用户侧临时建议保持有效**（发全文「执行优化项授权清单」或按文件编号「执行 6–10」）——同源条款将其固化为常态路径。
