# deepseek-harness 整库研究报告

- 日期：2026-08-29
- 研究对象：`/Users/shubinzhang/Documents/UGit/deepseek-harness`（TS monorepo，dsh 0.1.2-alpha.1，Cordis 全插件式 agent harness）
- 研究方法：6 个并行只读子代理分片深读 + 主代理亲自读核心文档与关键代码抽查。全部证据带仓库相对路径+行号，子代理原始笔记在 `.scratch/deepseek-harness-study/notes/`（不入库）。
- 代码抽查已核实：前缀重放（`packages/compaction/compaction-basic/src/region.ts` buildSummarizationInput）、8 节压缩模板（`.../summarizer.ts:31-66`）、reasoning_content 回传与 `content:""`（`packages/llm/llm-deepseek/src/serialize.ts:218-234`）、disjoint usage（`.../translate.ts:54-71`）。
- 关联记忆：[[reference-projects]]（已登记 deepseek-harness 六要点速查）、[[deepseek-reasoning-content-protocol]]、[[deepseek-token-estimation-facts]]、[[deepseek-cache-tool-schema-facts]]。

---

## 1. 项目概览

**定位**：DeepSeek 官方工程出品（`packages/*`、`docs/subsystems/*`、`.agents/notes/*` 三套文档+代码+决策笔记并行维护）。一个「模型中立、DeepSeek 优先」的 agent harness：核心词汇 provider-neutral，DeepSeek 专有协议在 adapter 层闭环。

**架构基调**：
- **Cordis 全插件式**：一切能力都是可逆 effect——注册即副作用、卸载即逆注册序回滚（`vendor/cordis/src/fiber.ts:71,406`）。加载顺序由 `inject` 依赖声明表达，无手工 boot 序列。
- **事件驱动**：五分发模式（emit/waterfall/parallel/serial/bail），事件分三域——session（持久事实、可重放）/ agent（飞行中拦截）/ capability（给 seam 挂策略）。`docs/architecture.md:66-72` 明言「事件是扩展点，选对域是多数改动的第一个决策」。
- **capability seam 三角色方法论**：每个可替换能力 = Service Definition（`ctx.<key>` + 词汇类型，绝不用 interface）+ 若干 Provider + 若干 Consumer；角色独立成包。`ctx.shell`/`ctx.fs`/`ctx.subprocess`/`ctx.subagents` 等 36+ seam。
- **三层组合模型**：profile（命名组合，Harness home 级）→ bundle（配置+代码的分发单位，`dsh-base` 为 web/headless/sdk/acp 共享首层）→ preset（每会话 agent 组合，`agent.cordis.yml` 经 isolate realm 挂 scope 子树）。patch 按行 id 替换整行 config。
- **底层哲学**：一切编排状态（plan/goal/todo/schedule/agent-team）都是会话日志事件 + fold，进程内派生态绝不写回持久记录；「模型可见 ⟺ 已记录」三层硬保证贯穿始终。

**工程纪律基线**：per-file 100% 覆盖率门禁、keyless snapshot replay（record/refresh/replay 三模式，CI 只读 replay）、invariant companion 门禁、50+ doc-sync 校验脚本、Agent Notes 制度（每非平凡改动 MUST 附 note，六分类，必带 Alternatives）。

---

## 2. 核心架构发现（跨分片共性）

1. **「模型可见 ⟺ 已记录」三层硬保证**：append 时 lossless-JSON + surfaceOp 校验 → `deriveMessages()` 纯函数投影 → agent-loop 每次 llm/stream 前断言请求 ≡ 日志投影。这是 token-meter 对账、retry 同轮重跑、compaction 前缀重放全部成立的地基。
2. **发布与持久化解耦 + 显式 flush barrier**：写日志先行，通知/账目是消息投递问题（quiet inject vs wakeup steer 两档），runtime 账目用独立 message source kind，transcript 永不冒充模型/子代理亲笔。
3. **失败归一化单点 + code-only 路由**：adapter 把 status+provider 错误文本归一为稳定 code（`AUTH`/`QUOTA`/`RATE_LIMIT`/`CONTEXT_WINDOW_EXCEEDED`/`EMPTY_RESPONSE`…），上层消费者绝不解析 provider 文本。
4. **确定性请求构造保 KV 前缀**：工具顺序稳定（toolOrder）、system 逐字节稳定、reasoning 逐轮追加、usage 用「canonical envelope 完全匹配」才复用——缓存是构造出来的，不是技巧挣来的。
5. **capability seam 判据**：同一能力出现第二个实现候选 / 消费者与实现演化速度不同 / 一个 provider 替换带动整个产品换世界（fs+subprocess 共享执行世界）。第二个 provider 出现时，是加分支还是立 seam——写进评审 checklist。

---

## 3. 六大分片要点

### 3.1 核心骨架（core-spine）
- 事件三域 + turn/step/claim 流转；typed Decision 的瀑布 `next()` 委托语义（不调用即短路，单决策事件是设计、观察型监听器忘了是 bug）。
- 工具 call/result 配对不变量 + 中断时 synthetic 补记（账目完整，但不冒充亲笔）。
- 取消语义：已启动的工具必须 drain 不 abandon；每包 invariant companion + verify-package-invariants 门禁。

### 3.2 上下文生命周期（对 Clawith 最重要）
- **压缩完整事务序列**：`compaction/start` 同步落锁 → 摘要 → shrink 校验 → `compaction/summary` → replace user/message → `compaction/end`。
- **双触发源**：pressure 0.8 水位 + context-overflow（认 `CONTEXT_WINDOW_EXCEEDED`，绕过保留策略 retain=0 强缩；surface generation 前进才 retry；`maxOverflowRetries` 限次防「压缩→溢出」死循环）。
- **逐字节前缀重放吃 warm cache**（已抽查核实，`region.ts` buildSummarizationInput）：重放 requestHeader 的 system+tools + 被遮蔽区 `deriveEventMessage` 消息，压缩指令作为**最后一条 user 消息**追加——辅助调用成为上一请求的真前缀，KV cache 命中而非失效。
- **8 节结构化压缩指令**（已抽查核实，`summarizer.ts:31-66`）：Primary Request and Intent / Key Technical Concepts / Files and Code / Errors and Fixes / Pending Jobs / Current Work / Next Step / Critical Context + Rules（保留路径/命令/错误串/签名原文、不回抄旧 checkpoint 块）+ CHECKPOINT_PREAMBLE（把摘要框定为「已确立背景，不要重述」——直接回应 Clawith 的 [[direct-chat-run-boundary-fix]] 教训：user 角色摘要措辞绝不能是祈使/目标句）。
- **shrink 校验**：摘要必须严格小于被遮蔽 span，否则抛错不落 checkpoint；finish=max-tokens 视为失败。
- **pruner 确定性算法**：8192 code points / head 4096 / tail 1024，先 prune 重计量，压力解除即跳过摘要，仅触发后运行。
- **token-meter**：CHARS_PER_TOKEN=4 启发式（官方自认 CJK 严重低估，与 [[deepseek-token-estimation-facts]] 实测一致）+ 真实 usage 锚点「envelope 完全匹配才复用」+ 有符号增量。
- **spill**：工具执行时 maxInlineBytes 预防机制。

### 3.3 LLM 与 DeepSeek 适配层
- **adapter seam**：core 定义一次中性词汇（Message/StreamChunk/FinishReason/TokenUsage），adapter 只翻译 wire，consumer 永不接触 provider 文本；正确性敏感元数据按 exact model route 解析，catalog 只 advisory。
- **reasoning_content 成熟解法**（已抽查核实）：reasoning 是 durable 一等 block，序列化 assistant 历史时**逐轮无条件回传**（工具轮必须、非工具轮无害且保 gateway 重编码签名）；纯文本轮 `content:""` 绝不发 null——**reasoning-only 轮发 null 会被 400 且因消息已 durable 而 brick 该会话后续所有轮**。
- **disjoint usage**（已抽查核实）：DeepSeek `prompt_tokens` 含 cache hit，harness 约定 `inputTokens = prompt_tokens - cached`，cacheReadTokens 单列。
- **空响应=可重试错误**：terminal stop 且零 block → `EMPTY_RESPONSE` 默认重试（否则 turn 静默结束）。
- **context overflow 单点归一**：400 时正则分类 code/type/message → 唯一规范码 `CONTEXT_WINDOW_EXCEEDED`，上层只按 code 路由。
- **replayState 缝**：换 provider 时私有重放元数据丢弃、退化为中性转换并记诊断，durable content 永远权威。

### 3.4 能力与安全
- **sandbox seam 只包子进程 argv**：`confine(argv, policy) → 替换 argv`，边界不含 in-process 工具（fs 用 path fence 自管）；三模式只承诺文件效应，网络不在词汇表。
- **bwrap 例外：`--unshare-pid` 私有 PID 命名空间**——procfs magic link 能把路径带出 mount profile（对照 Clawith bwrap 僵尸问题的对照素材）。
- **SandboxEnforcement full|partial 是报告的事实**：Landlock 旧 ABI/Windows ACL 报 partial，绝不夸大。
- **fail-closed 铁律**：无可用 backend `confine()` 抛 `SANDBOX_UNAVAILABLE`，静默无沙盒直通永不合法。
- **approval**：allowed-once 单次授权不持久化；asked+decided 审计对 log-only 不入模型转录；`never` 策略 service 内先于 waterfall 强制；`ApprovalRequest` 刻意不带工具参数（answerer 挂 callId，避免第二份漂移渲染）。
- **权限预设**：把 sandbox/mode + approval/policy 两 knob 捆绑命名预设，预设只记录意图、不拥有执行，经 canonical setter 写穿。
- **subprocess 全套防御**：scrubbedParentEnv（丢 `/KEY|PASSWORD|SECRET|TOKEN/i`）+ 显式 undefined tombstone；终止唯一动词且 tree-scoped（SIGTERM→grace→SIGKILL，detached 组信号，`waitForExit()` 等整树，live 持有到树消失）；`kill(0)` 对只剩 zombie 的组仍答活的专门处理；collect 溢出保 TAIL + 0700 随机名 spill。
- **guard 是提醒式非硬熔断**：repeat-tool-reminder 阈值 [3,5,8] advisory 注入（denied call 也计数）；timeout-policy 合作式（deadline 只经 signal 请求停止，绝不硬杀）。

### 3.5 工程平台
- **Cordis 可逆效果**：fiber 卸载逆注册序 unwind；`INACTIVE_EFFECT` 拒绝卸载后注册。
- **waterfall 陷阱两条**：观察型监听器忘 `next()` = 静默吞功能；dispatcher 必须兜住回调异常（一个坏订阅者不打断核心生命周期）。
- **isolate realm 两血泪**：服务发布进根 realm → 第二会话 collide 且 unhandled rejection 静默失败；消费者在 host 平面而服务被挪进 agent 平面 → 启动失败（拆隔离域前必须 grep 全部 injector，含 host 平面）。
- **keyless snapshot replay**：`session.jsonl` 同时供用户输入与模型重放；record/refresh 仅本地+人工评审 diff，CI 强制只读 replay；fixture 剥离 key 与 body 时序信封。
- **with-key e2e**：「inference is cheap here」——smoke = boot 真实 profile + 发 prompt + 检查真实世界（postmortem 0001：mock 抓不到的 "green unit tests, broken product"）。
- **verify-the-world-not-self-report**：e2e 断言重跑命令/重读文件，不用 agent 自报；未触碰文件必须字节一致。
- **Agent Notes 制度**：每非平凡改动 MUST 附 note，六分类（feature/bug-fix/simplification/architecture/process/testing），implemented 禁用 Proposal 措辞；现况 implemented 下 594 feature / 516 architecture / 291 bug-fix。

### 3.6 编排
- **一切状态=会话日志+fold**：plan 只是 log-only 布尔（非状态机）；goal durable phase 与 process-local activation 分离；todo whole-list 替换无 id。
- **jobs 完成收尾语义**（对 Clawith heartbeat 最重要）：busy owner 注入下一步（inject）/ idle owner 开新 turn（wakeup）；`reported` 标志 first-wins 单次通知；`maxConsecutiveWakes=3` 防自激链，用户输入重置预算；teardown 时 owner 已销毁直接标 reported 不开模型 turn。
- **subagent 上下文继承三档**：fresh child / fork 传 balanced completed-turn prefix（到最后一个 turn/end，不含 in-flight 尾巴）/ delegated turn；**绝不继承工具/服务/权限**，每个 child 新 flat scope。
- **capability 先验拒绝**：provider 不支持的能力（outputSchema/toolFilter 等）报 `UNSUPPORTED_CAPABILITY`，绝不 accept-then-ignore。
- **fatal 错误纪律**：组合器重抛 fatal 而非映射 null；非 completed stopReason 一律 isError，绝不把部分输出报成功。
- **skills**：frontmatter 扩展键 `disable-model-invocation`/`user-invocable` → 调用策略归一；layered registry 最近层胜出；每 get 重读 body 不缓存。

---

## 4. 对 Clawith 的可迁移点清单（按收益×风险排序）

### A 区：直接命中既有踩坑，收益最高

| # | 迁移点 | dsh 证据 | 对照的 Clawith 坑/记忆 | 评估 |
|---|---|---|---|---|
| A1 | **压缩前缀逐字节重放吃 warm cache** | region.ts buildSummarizationInput；summarizer 注释 | 首轮压缩三次调用 cache_read 全 256（零命中） | 收益高、风险中 |
| A2 | **8 节结构化压缩指令 + maxTokens 上限 + shrink 校验** | summarizer.ts:31-66 | 弱模型 74% 重述、无硬约束 | 收益高、风险低 |
| A3 | **reasoning_content 无条件逐轮回传 + durable 分离存储** | serialize.ts:218-234 | [[deepseek-reasoning-content-protocol]]（工具轮必须回传否则 400） | 收益高、风险低（前提：落库时 reasoning 与 text 分离） |
| A4 | **`content:""` 非 null 的坑位清单** | serialize.ts:218-227 | 某会话从此所有请求 400 时先查 null-content assistant 消息 | 收益高、风险低（防御性检查） |
| A5 | **disjoint usage 对账（cache read 单列 + envelope 匹配才复用）** | translate.ts:54-71；token-meter README L43 | [[deepseek-cache-tool-schema-facts]]；Langfuse 计费接入时 inputTokens 与 billed 对不上 | 收益高、风险低 |
| A6 | **稳定错误码层 + code-only 路由** | adapter.ts:332-344；error.ts:50-86 | 多调用点散落判断 provider 文本 | 收益高、风险低（改动最小） |
| A7 | **bwrap `--unshare-pid` + tree-scoped terminate 整树 quiescence** | subprocess spawn.ts:392-393；index.ts:79-102 | Clawith bwrap 僵尸/孤儿 | 收益高、风险中 |

### B 区：结构改进，收益中高

| # | 迁移点 | 评估 |
|---|---|---|
| B1 | **jobs 完成通知投递语义（reported + wake 预算 + quiet/wakeup）→ heartbeat 收尾** | 收益高、风险低；落地=run 完成回调加 reported 位 + 每 agent 连续 wake 预算 |
| B2 | **CONTEXT_WINDOW_EXCEEDED 单点归一 + 压缩后「surface 前进才 retry + 上限」** | 收益高、风险中；与 A1/A2 同票落地 |
| B3 | **subagent 上下文继承三档（fresh/fork-balanced-prefix/delegated）→ a2a delegate** | 收益高、风险中；与 [[direct-chat-run-boundary-fix]] 同源 |
| B4 | **fs 版本守卫（FsObservation + FS_STALE_VERSION/FS_NOT_OBSERVED）** | 收益中、风险低 |
| B5 | **env scrub（`/KEY|PASSWORD|SECRET|TOKEN/i`）+ 0700 随机名 spill** | 收益中、风险低，立即可抄 |
| B6 | **错误/失败分类先于文本（runner 失败 vs denial 分开上报；timedOut/aborted 互斥）** | 收益中、风险低；改进 bwrap 诊断 |
| B7 | **approval 单次授权 + audit 对 + 能力门控字段（只在能兑现的 executor 挂载时才暴露）** | 收益中、风险低；对照 L3 审批字段设计 |
| B8 | **空响应=可重试错误（EMPTY_RESPONSE）** | 收益中、风险低 |

### C 区：工程纪律，成本低

1. **keyless snapshot replay → Clawith 等价物**：已有材料（checkpoint 导出 + Langfuse traces）。录真实 run 为 fixture（剥离 key/时间信封），mock LLM 重放，CI 只读重放。抓 "green unit, broken product"。
2. **Agent Notes 制度轻量版**：`docs/technical-plans` 已有雏形；补「非平凡改动必附 note + 必带 Alternatives」+ pre-commit 引用校验脚本。
3. **verify-the-world-not-self-report**：approval 转发、run 状态、文件效果断言改为重读外部事实。
4. **正交结果独立上报**：run 超时/取消/错误状态字段保持独立。
5. **invariant 门禁轻量版**：把已踩坑（card-mode failed 渲染、run boundary 污染、artifact freshness）固化为启动/每 run 断言。

### D 区：不建议照搬

- per-file 100% 覆盖率（Python monolith 代价极高；取「未覆盖行=死代码候选」姿态 + 关键路径分区门禁即可）。
- type-equiv 逐字文档同步、Host/Client 双 tsconfig + Typert 契约生成（TS 基建特定）。
- 594+516 条 note 的完整文化（取轻量版）。

---

## 5. 与既有待办/票的映射

`.scratch/compaction-slimming/` 四票与本次研究的对应关系（dsh 为每票提供了成熟参考实现）：

| 票 | 内容 | dsh 参考 | 本次研究增量 |
|---|---|---|---|
| 01-tool-result-pruning | 工具结果剪枝 | pruner 确定性算法（8192/head4096/tail1024，先 prune 重计量、压力解除跳过摘要、仅触发后运行） | 可在票中直接引用 region 计算与「剪枝先行于摘要」的顺序 |
| 02-structured-compact-prompt | 结构化压缩指令 | 8 节模板全文 + maxTokens 8192 + shrink 校验（摘要必须严格小于被遮蔽 span）+ CHECKPOINT_PREAMBLE（防祈使句误当新指令——命中 [[direct-chat-run-boundary-fix]]） | 模板已逐字摘录于笔记 context-lifecycle.md |
| 03-compact-prefix-cache-reuse | 前缀缓存复用 | buildSummarizationInput 逐字节重放（system+tools+deriveEventMessage+指令作最后 user 消息）| 已抽查核实代码 |
| 04-chinese-token-estimation | 中文 token 估算 | dsh 承认 4 chars/token 对 CJK 低估、只作占用显示；真实 usage 永远以 provider 为准 + envelope 匹配锚点 | Clawith 实测 bytes/4 已优于 dsh 启发式；可补「对账锚点规则」防误用 usage |

其余映射：B1→heartbeat 收尾通知待办；B3→a2a delegate 上下文模型（[[direct-chat-run-boundary-fix]] 同源）；A7→bwrap 僵尸防护（[[pg-connection-exhaustion-incident]] 等运行时韧性待办）；C1-C5→无票，建议按工程纪律增量引入。

---

## 6. 未解疑问（留待后续）

1. reasoning 逐轮无条件回传的输入 token 增量未实测（dsh 取舍是「KV 命中摊薄」；Clawith v4-flash/pro 需实测）。
2. snapshot replay 对多 provider 的适用性（llm-replay 注入机制细节未深读）。
3. preset 热重载与运行中会话的交互边界（unmount-then-mount 失败恢复路径未读实现）。
4. isolate realm → DB 行模型的映射成本（~3ms 是进程内成本，DB 模型需物化查询）。
5. dsh 明确不管网络隔离；Clawith docker sandbox 的网络需求需自行设计。
6. jobs LocalJobRegistry 是 process-local；Clawith 多 worker 部署需进程外等价物。
7. schedule 无 cron/日历表达式；Clawith heartbeat 若有 cron 需求需放宽。

---

## 7. 证据索引（主代理亲自核实）

- 前缀重放：`packages/compaction/compaction-basic/src/region.ts` buildSummarizationInput（重放 header.system/tools + deriveEventMessage 逐条；注释 "genuine prefix … reuses the provider's KV cache"）。
- 8 节模板：`packages/compaction/compaction-basic/src/summarizer.ts:31-66`（COMPACTION_INSTRUCTION + CHECKPOINT_PREAMBLE）。
- reasoning 回传与 content:""：`packages/llm/llm-deepseek/src/serialize.ts:218-234`（"Text-less turns send "" — NEVER null"；reasoning-only 轮 null 会 400 且 brick 后续）。
- disjoint usage：`packages/llm/llm-deepseek/src/translate.ts:54-71`（inputTokens = prompt_tokens - cached）。
- 完整分片证据见 `.scratch/deepseek-harness-study/notes/{core-spine,context-lifecycle,llm-deepseek,capability-security,engineering-platform,orchestration}.md`。
