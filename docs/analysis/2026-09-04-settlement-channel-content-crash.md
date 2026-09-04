# 2026-09-04 即时结算合成消息写入 LangGraph 通道崩溃（reconciliation_required）事故分析

- 状态：分析完成，根因实锤；F1+F3 已提交 `369cc4a9` 并上线（2026-09-04 06:29:15Z）；2026-09-04 下午完成深度复审（见 §9），晚间完成参考资料对照复审并修订方案（见 §10）
- Run：`ce976e4c-aac7-4de3-a704-88a34ae63a6f` · Command：`f5e0d280-c705-4194-a761-6c4791c063d1` · Trace：`f5e0d280c705`
- 当前部署：`369cc4a9`（F1+F3 通道崩溃修复，image dbd9b076942c）；事故版本 `57c2e5b2` 在其前，回滚标签 `pre-369cc4a9-82b5d538e0ea`

## 1. 现象

用户在 direct chat（agent `950a1943` Android 工程师 07，输入「执行 2」）收到任务失败：

```
任务执行未完成。
错误：Runtime could not reconcile the command after repeated attempts.
错误码：reconciliation_required
```

后台：start 命令 `f5e0d280-c705` 执行 3.5 分钟后于 `06:07:40.208Z` 被 `rejected`，
`attempt_count=5`（= `AGENT_RUNTIME_COMMAND_MAX_ATTEMPTS` 上限），`applied_checkpoint_id=NULL`。

## 2. 时间线（证据链）

| 时间 (Z) | 事件 | 证据 |
|---|---|---|
| 05:31:14 | 部署 57c2e5b2（票A：D 确定性即时结算 + completed_actions 流水；票B：压缩循环熔断） | registry / workspace facts |
| 06:04:12 | run 创建，start 命令入队 | `agent_run_commands.created_at` |
| 06:04:13–06:06:57 | 图执行 step 0–5 正常；每步结算被跳过：`[RuntimeStepSettlement] skipped code=unsafe_exchange_exceeds_recent_budget`（线程历史长，未决交换构成硬屏障超近期预算） | backend 日志 |
| 06:07:07–06:07:15 | step 6–7 正常（LLM-CacheFp、8 个 read_file 工具调用） | 日志 + Langfuse trace `e67968f7…` |
| 06:07:37 | **首次崩溃**：`Runtime Command Worker iteration failed`，pydantic `ValidationError: 2 validation errors for HumanMessage`，崩溃点在 LangGraph `apply_writes → add_messages → convert_to_messages → HumanMessage(content=dict)` | backend 日志（完整 traceback） |
| 06:07:37–06:07:40 | 命令被重试：attempt 预算 5 次在 ~3.2s 内耗尽。重试内部路径存在两候选机制（见 §9-R1：时间线与「每次重放崩溃」假说不兼容，更可能是 retry-path 0.1s 延迟而非每次 raise 崩溃）；Langfuse 佐证无新增 tool/llm span | `attempt_count=5`、Langfuse 无新 span |
| 06:07:40.208 | 耗尽路径 `_process_exhausted_locked`：checkpoint lifecycle 仍 `running`（非 waiting/terminal）→ 无法对账 → `_reject("reconciliation_required")` | `agent_run_commands.applied_at` / error_code |

## 3. 根因链

```
57c2e5b2 票A 上线：node_executor._tool 在工具批干净收尾后调用 settle_step_messages（D 结算）
  → settle_completed_exchanges 把窗口外完整终态交换块折叠成单条合成消息
  → _settlement_summary 构造 content = {"historical_tool_exchange": {...}}  ← dict！
  → _message_for_channel 只规范 role/tool_calls，dict content 原样透传
  → update["messages"] = RemoveMessage×N + 合成消息 + output_messages
  → LangGraph messages 通道 reducer add_messages → convert_to_messages
  → langchain_core 按 {"role":"user", content=dict} 构造 HumanMessage(content=dict)
  → pydantic 校验失败：content.str / content.list[union[str,dict]] 双错
  → 图执行崩溃（apply_writes 阶段，异常不受结算的 fail-soft try/except 保护）
```

**根因（单句）**：结算合成消息的 `content` 是裸 dict，违反 LangGraph messages 通道的
`str | list[content-block]` 契约；该 dict 形态本是压缩 payload（JSON 序列化走 LLM 输入）
的合法形态，被同一 `historical_tool_exchange` 形状**误用到了通道写入路径**。

**放大为终态拒绝的次因**：

1. 写通道失败点在图内 `apply_writes`，在 `settle_step_messages` 的
   `except RunCompactorError`（fail-soft）之外——结算的防御只包了纯计算，没包写入。
2. 命令 worker 对未知异常一律 `release_for_retry + raise`（`command_execution_failed`），
   确定性崩溃照样烧 attempt；耗尽后 quarantine 见 checkpoint 仍 runnable，只能拒绝。
3. `_terminal_error_code` 对无 `.code` 的异常兜底返回 `reconciliation_required`
   （command_worker.py:309-318），把「代码 bug」伪装成「对账失败」，底层
   pydantic 错误只在日志，用户侧不可诊断。

**冒烟铁证（数据库现场）**：`langgraph_checkpoint.checkpoint_writes` 保留了崩溃那一刻的
未提交写——checkpoint `1f1a826e-dae5-63db-830d-6a67d834bd5c`（step 781，metadata
run=ce976e4c / command=f5e0d280-c705）task `75509625-b37b-c572-357d-4d5026231190` 的
`messages` 写 blob 含 `RemoveMessage×2 + {"role":"user","content":{"historical_tool_exchange":{…}}}`
（msgpack 明文可读）。全库扫描仅此 1 条写含该标记——无其他线程被污染。

## 4. 为什么 CI 没拦住

S4 集成测试（`test_agent_runtime_step_settlement_integration.py`）断言的是节点 update
的**字典形态**（`assert "historical_tool_exchange" in synthetic["content"]`——dict 形态是被
测试显式肯定的），从未把 update 送进真实 LangGraph 通道 reducer 做往返。全量 3613 passed
与此 bug 共存。缺失的测试类别 = 通道契约往返测试（见 F3）。

## 5. 影响面

- 触发条件：某工具批干净收尾时，滑动窗口外存在「全部工具调用已达账本终态」的完整交换块
  → 结算产出 ≥1 条合成消息。同线程长历史下 `unsafe_exchange_exceeds_recent_budget`
  会让结算先跳过若干步，屏障/窗口移动后才命中——所以不是每 run 必崩。
- 部署后 3 个 run：2 applied 正常（未触发结算）、1 崩（本例）。平台当前无在途命令。
- **持久污染点**：线程 `95767fe5-287d-40c8-b740-4de1921ea1ce` 的 3 行 pending writes
  （lifecycle/messages/branch:to:control_guard）。LangGraph 任务 ID 由 checkpoint ID 确定性
  派生，下次该线程任何新 run 恢复时会把这批毒写重放进通道 → **即使代码修好，该线程仍会
  确定性复崩**，必须数据治理（F4）。
- 其他线程只要未来 run 命中结算触发条件，在 F1 上线前同样会崩 → 修复有平台级紧迫性。

## 6. 生产级修复方案

### F1 根因修复（channel-legal 合成消息）— ✅ 已提交 `369cc4a9`

`run_compactor.py:_settlement_summary`：`content` 由裸 dict 改为确定性 JSON 字符串
（`json.dumps({...}, ensure_ascii=False)`）。字符串满足通道契约；结构化事实完整保留
（全库无代码消费方解析该 dict——只有测试读它）；确定性字节不变（summary 为 dataclass，
字段序固定、无时间戳/volatile 字段，`_short_result` 截断确定性）。压缩 payload 路径的
`historical_tool_exchange` dict 形态（`_summary_ready_blocks`，只进 payload JSON 不进通道）
不动。复审实测两结算测试文件 17 passed；提交说明含事故引用。

### F2 纵深防御（写前通道校验 + fail-soft）— 待实施，设计经复审修正

两层：
1. **choke-point 不变量**（改在 `_message_for_channel`，node_executor.py:486）：content 只
   允许 `str | list`（多模态 image block 是 list，必须放行），dict/其他类型直接
   `RuntimeNodeTransitionError`——覆盖模型输出/结算/压缩回写/延迟恢复全部通道入口，
   任何未来形状 bug 在写点快速失败并带明确错误，而非深埋在 langgraph `apply_writes`。
2. **结算点 fail-soft**（node_executor._tool 结算块 ~1302-1312）：把「构造写入列表」纳入
   现有 `except RunCompactorError` 同级保护，写前对合成消息做 `convert_to_messages`
   往返校验；失败 → `logger.warning` + 跳过本次结算（结算本就是优化，跳过永远安全）。
   目标：任何形状 bug 从「确定性杀死 run」降级为「跳过结算一步」。

### F3 回归测试（通道 reducer 往返）— ✅ 已随 `369cc4a9` 提交

`test_settlement_update_survives_messages_channel_reducer`：把 Tool 节点 update 送进真实
`langgraph.graph.message.add_messages` 归约 + `convert_to_messages`，断言每条存活消息
`content ∈ str|list`（旧 shape 实测 ValidationError）。正是本 bug 的测试盲区，作为 CI
永久门禁。另有 3 处既有断言从 dict 形态改为字符串形态、`_synthetic_content` helper 改为
解析 JSON。

### F4 数据治理（清毒写，需用户批准 DB 写）

F1 上线后执行（顺序不可反——先清数据会撞旧代码再次复崩）。复审已核实目标精确：
该 checkpoint 下恰 3 行 pending writes、全属同一 task（lifecycle idx0 / messages idx1 /
branch:to:control_guard idx2），删除即可把崩溃的那个 superstep 完整回滚：

```sql
DELETE FROM langgraph_checkpoint.checkpoint_writes
WHERE thread_id      = '95767fe5-287d-40c8-b740-4de1921ea1ce'
  AND checkpoint_id = '1f1a826e-dae5-63db-830d-6a67d834bd5c'
  AND task_id       = '75509625-b37b-c572-357d-4d5026231190';  -- 3 行
```

清理后该线程下次 run 从 checkpoint `1f1a826e` 一致状态重放工具节点（幂等复用账本终态），
模型步继续，结算用新代码产出合法消息。删除前建议 `SELECT` 备份 3 行 blob 到本机文件。
复审另核实：`pg_locks` 无 advisory 残留——崩溃后线程锁已正确释放，无锁卡死风险；
不清则「执行 3」必崩，属阻塞性治理项。

### F5 尝试预算/可观测性加固（独立票，不阻塞 F1 上线）— 设计经复审修正

1. **确定性崩溃提前终态化（最小方案）**：`release_command_claim` 本就把 error_code 写回
   命令行——release 时写入崩溃指纹（如 `crash:pydantic_validation` 或异常类型+代码，
   控制在 100 字符契约内），claim 时若行上残留指纹与新异常指纹相同 → 第二次即直接
   终态拒绝，不再烧满 5 次。比「in-memory per-command 指纹」更简单且天然跨 daemon 生效。
2. **每 attempt 独立日志**（本事故 5 次 attempt 仅 1 条 ERROR 记录）：在 `_begin_attempt`
   与各 retry/release 路径补 `command_id/attempt_count/error_code` 的结构化 INFO 行，
   终结「重试内部时序不可观测」状态（见 §9-R1）。
3. `_terminal_error_code` 兜底码评估：`reconciliation_required` 语义是「对账无法收敛」，
   用它兜底 pydantic 类代码缺陷会误导排障方向；考虑兜底改为更中性的
   `command_execution_failed` 或按异常基类映射。**（§10.2-2 对照参考系后已定：与
   worker 重试白名单化合并为一个改动点，不再单独改文案。）**

## 7. 部署与回滚

- 走 `clawith-prod-deploy`（红线：不灰度，一步全量）。**F1+F3 已上线（2026-09-04 06:29:15Z，
  image dbd9b076942c，容器内特征已验）；F2 待实施（下批）；F4 在上线验收后执行；F5 独立票。**
- 前置检查：deploy 前确认无在途命令（`agent_run_commands` 无 claimed/pending，本次已查空）。
- 回滚链：`pre-369cc4a9-82b5d538e0ea`（=57c2e5b2 镜像）→ `pre-57c2e5b2-8409860e97de`
  （=63e1e9dd 镜像，无结算代码，最彻底）。注意：回滚后该线程毒写仍在，需先 F4 清数据或
  保持线程冻结。
- 上线验收（已执行，2026-09-04 06:29Z 并行会话）：health/前端 200、容器内 grep 新 content
  形态 `"content": json.dumps(`（run_compactor.py:668）、部署后日志无 ValidationError/
  iteration failed。settle 冒烟（造一个含终态交换的测试线程）可待首个真实 run 触发时观察。

## 8. 验证清单（事后）

1. F4 后用户在「执行 2」线程发新消息，run 正常推进（无需复现即可观察）。
2. 后续 run 触发结算时日志出现 `[RuntimeStepSettlement]` 正常路径，不再有
   `unsafe_exchange_exceeds_recent_budget` 之外的异常；Langfuse 工具/llm span 连续无断层。
3. `checkpoint_writes` 扫描 `%historical_tool_exchange%` 保持 0 新增毒写。
4. 全量 pytest（基线 ~3613）+ ruff + pyright + arch-guard 过。

## 9. 深度复审记录（2026-09-04 下午，用户要求重新研究后）

复审方式：读 `369cc4a9` 提交全文并实测测试；逐路径走读结算/压缩/模型输入三方下游；
扫 57c2e5b2 其余运行时文件查同类缺陷；查 `pg_locks` 与 `checkpoint_writes` 精确核实治理
目标。逐项结论：

- **R1 重试机制与时间线矛盾（新发现，已修正 §2/§3 表述）**：daemon `error_delay_seconds=1.0s`，
  若 5 次 attempt 每次都 raise 崩溃，最短需 ~4.5s，与实测 3.2s 不符；`retry_delay=0.1s`
  的 retry-path（`RetryableCommandError`/`ThreadLockNotAcquired`/`ToolExecutionReconciliationPending`，
  不 raise、不记 ERROR）与 3.2s 吻合。故 attempt 2–5 更可能走了 retry-path（如
  `classify_checkpoint` 对崩溃残态的判定分支）而非「每次重放崩溃」。**不影响根因与修复**：
  首崩证据（traceback+毒写）充分，最终拒绝路径经代码走读确认；但重试内部时序目前不可
  观测，F5-2 的 per-attempt 日志应优先实施以终结该盲区。
- **R2 压缩路径为何从未崩（机制级证明）**：压缩写回通道的 `recent_messages = _flatten(retained)`
  （run_compactor.py:1231）——只有 retained 原始消息，dict 合成消息只存在于 payload 的
  `covered_messages`（送 LLM 的 JSON），从不进通道。这解释了同一 `historical_tool_exchange`
  dict 形态在压缩路径长期无害、在结算路径即崩——也证明 F1 只改结算侧是充分且最小的。
- **R3 F1 修复正确性**：channel 契约由 traceback 本身权威确认（`content.str | content.list`）；
  JSON 字符串选择优于 content-block 列表（harness 模型输入 builder `_message_content`
  返回 `str|list`，字符串是唯一零改动路径）；确定性保持（`ToolExchangeCompactionSummary`
  dataclass 字段序固定、无 volatile 字段、`_short_result` 截断确定）；下游兼容
  （`build_message_blocks` 把结算消息归为 normal 块；压缩对 settled 历史已有
  `D×compact` 交错测试覆盖）。实测两结算测试文件 17 passed。
- **R4 同类缺陷扫描（57c2e5b2 其余文件）**：model_step_service（预算 profile/指纹事件，仅
  出站消息准备）、tool_exchange（改名+ledger 派生）、tool_step_service（load_run_ledger）、
  worker_service（配置透传）——均无通道 dict-content 写入，无第二处同类缺陷。
- **R5 F4 治理精度**：目标 checkpoint 下恰 3 行 pending writes、全属 task `75509625`；
  `pg_locks` 无 advisory 残留（崩溃后锁已释放，无卡锁风险）。治理安全。
- **R6 F2 设计修正**：原方案只在结算点校验；复审升级为 choke-point（`_message_for_channel`）
  不变量 + 结算点 fail-soft 双层。choke-point 必须放行 `list`（多模态 image block 是 list，
  model_step_service.py:2633 实锤），只拒绝 dict/其他类型。
- **R7 F5 最小化修正**：指纹不复用 in-memory 结构，直接借 `release_command_claim` 已写回的
  `error_code` 字段存崩溃指纹，claim 时对比——零 schema 变更、跨 daemon 天然生效。

**复审后方案状态**：F1/F3 已提交待部署；F2（两层设计）待实施；F4 待批准执行；F5 独立票
（其中 F5-2 优先级提升：本事故暴露的重试不可观测性影响未来一切排障）。

**遗留开放项**：① R1 重试路径确证（per-attempt 日志落地后下一次异常即验证）；②
`unsafe_exchange_exceeds_recent_budget` WARNING 每步一条，长 run 刷日志——按 run+code 去重
（P3 顺带）；③ 部署与 F4 执行顺序、审批流程见 §7。

## 10. 参考资料对照（2026-09-04 晚，用户要求结合参考仓库重出方案后）

纪律：全部结论基于本地源码实读（langchain / langgraph / deepagents 克隆仓库 +
deepseek-harness、open-swe 代码级研究文档），逐决策点映射；无先例处明说。与本事故无关的
压缩方案整体对比见 `docs/analysis/2026-09-04-compaction-reference-implementation-comparison.md`
（不在此重复）。

### 10.1 逐决策点对照表

| 决策点 | 参考实现（实读位置） | 对照结论 |
|---|---|---|
| F1 content 用 JSON 字符串 | langchain v1 `SummarizationMiddleware._build_new_messages`（`langchain_v1/langchain/agents/middleware/summarization.py:755`）与 deepagents `_build_new_messages_with_path`（`middleware/summarization.py:734`）均以**纯 str** 构造摘要 HumanMessage；契约本体 `BaseMessage.content: str \| list[str \| dict]`（`langchain_core/messages/base.py:103`，`human.py:35/42/49` overload 同） | **强先例**：合成消息 content=字符串是生态通用形态；dsh 更进一步把结构化摘要包进带 `CHECKPOINT_PREAMBLE`+`<compacted-summary>` 标签的 str user 消息，与我们的 JSON-字符串同思路 |
| F2① choke-point 不变量 | 合法性强制点：langchain-core 在消息构造时（pydantic 字段）；deepagents `_messages_reducer.py` 在通道边界 `convert_to_messages` 强转 | **先例支持边界校验**：生态把强制点放在通道入口（reducer/构造器）而非使用方；我们把强制点前移到自家唯一收口函数 `_message_for_channel`，是同一模式的镜像 |
| F2② 结算 fail-soft | deepagents `_apply_event_to_messages`（`summarization.py:783-820`）：malformed 摘要事件/越界 cutoff → `logger.warning` + 原样降级 | **强先例**：合成状态畸形时「记日志+降级跳过」是 deepagents 的标准防御；结算本就是优化项，跳过永远安全，语义一致 |
| F3 通道 reducer 往返测试 | dsh 工程纪律「invariant companion 门禁」（dsh 研究文档）；deepagents reducer docstring 明文讨论 replay 确定性 | **同类先例**：dsh 以「不变量伴随门禁」防契约漂移，往返测试即我们的门禁。参考系中未见针对「合成写进通道」的现成往返测试——属把通用纪律应用到新落点 |
| F4 毒写外科清理 | **无直接先例**。最近似：langgraph 官方 `delete_thread`（`checkpoint/postgres/__init__.py:381`、`aio.py:665`）——整线程删除，粒度太粗（连历史一起丢）；官方亦不鼓励手改 checkpoint | 3 行 DELETE 是精确替代；作为「无先例手术」，以备份 SELECT + 行数断言 + F1 上线后执行三重约束补位（§6-F4） |
| F5-1 崩溃指纹提前终态化 | **无直接先例**。最近邻：dsh「错误分类先于文本」+ 规范错误码路由；open-swe `task_retry.py:62-66` 白名单重试 | 属新设计（借 `release_command_claim` 已写回字段存指纹、claim 时对比，零 schema 变更） |
| F5-2 per-attempt 日志 | open-swe `task_retry.py:62-66` + `server.py:1324-1331`：重试接线显式、可观测 | 弱先例（他们靠显式中间件接线获得可观测，我们靠日志补齐） |
| F5-3 兜底错误码中性化 + 重试白名单 | langgraph `pregel/_retry.py`：无匹配 retry policy 立即上抛、绝不伪装（`:799`/`:809`；`_should_retry_on` 在 `:841`）；open-swe `retry_on` 白名单=仅 5xx/408/409/425/429/529 与传输类异常；「reconcile」在 open-swe 语义=超时 pending 周期清扫（`reconcile.py:37-119`），与「对账失败」无关 | **强先例**：上游哲学是「未知异常如实上抛」；我们用 `reconciliation_required` 兜底把代码缺陷伪装成对账失败，与两处独立先例均相悖，改名+白名单化有据 |

### 10.2 对照后对方案的增量修正

1. **F2 位置确认不迁移**：choke-point 放 `_message_for_channel` 正确——它是唯一把所有通道写
   （模型输出/结算/压缩回写/延迟恢复）收口的函数，等价于生态里 reducer/构造器的位置。
2. **F5-3 从「评估」升为「决定」**：langgraph（不匹配即上抛）与 open-swe（白名单仅传输类
   /5xx、`max_retries=2`）两处独立先例一致表明「未知异常盲重试 + 兜底伪装码」是双重反模式。
   兜底码改中性应与 worker 重试白名单化合并为同一改动点（`command_worker` 的 retry 判定处），
   而非只改错误码文案。方向与参考系收敛：窄白名单 + 小预算（open-swe 为 2），
   确定性代码缺陷第一次即终态。
3. **F1 补一个 P3 可选增强（不阻塞本票）**：deepagents/langchain 对合成摘要消息打
   `additional_kwargs={"lc_source": "summarization"}` 标记供下游过滤/识别；我们的结算合成
   消息可加同类标记（如 `lc_source="settlement"`），利于压缩器与调试识别。需先评估
   `build_message_blocks` 等下游对 additional_kwargs 的兼容性，单独小票。
4. **F4 无先例声明**：不因缺先例而改用整线程 `delete_thread`——粒度损失（丢全线程历史）
   大于手术风险；以 §6-F4 三重约束（备份/行数断言/顺序）补位。
5. **重试预算方向确认**：参考系重试都是「窄白名单 + 小预算」；我们对未知异常全量重试
   `MAX_ATTEMPTS=5` 是生态中罕见的宽进宽出形态，F5 方向正确且优先级应提升（本事故的
   5×放大正源于此）。

**对照后最终方案**：F1/F3 已提交待部署；F2 双层设计不变（choke-point + fail-soft，位置经
对照确认）；F4 待批准（无先例手术，约束已配）；F5 按 §10.2-2/5 收敛为「重试白名单化 +
中性兜底码 + 指纹提前终态化 + per-attempt 日志」一个独立票，其中白名单化+兜底码为同一
改动点。
