# 20260901 同会话任务衔接 — 会话级任务状态（goal/phase/未决事项）+ 注入口径

> 范围裁定（用户拍板 2026-09-01）：**只做同会话（同 thread）衔接**；跨线程（新会话）
> 任务快照泛化、显式会话 mention（dsh session-reference 协议）**暂不做**，事故驱动时再立票。
> 本文档是 [[20260901-run-context-inheritance-root-fix]]（R1/R3/R4）的延续：上一份解决了
> 「清单数据通道」，本份补「任务状态一等公民」——run 收尾确定性写状态、下一 run 开局
> 无条件注入，不靠检测、不靠模型自觉。

## 0. 问题定义（证据：2026-09-01 事故 61c27271 + 真实链路 545e8262）

R1/R3/R4 上线后，「数据通道」已闭环：清单确定性落库、编号引用自动检索、tool-result
死引用内联。但**衔接口径仍单一**：`_prior_run_summary` 对上一 run 一律宣称
「上一轮已完成」（`thread_visibility.py:94`）。三种真实形态被同一种措辞覆盖：

1. **上一轮是 waiting**（模型反问、run 停在 waiting_user 非终态）——桥措辞说「已完成」，
   事实是「在等你的回复」；且 R1 只挂 `status=="completed"`（`list_persistence.py:491`），
   waiting 收尾的清单/结论无人落库（本次事故 run A 即此形态）。
2. **上一轮失败/取消**——桥措辞说「已完成」，模型可能基于失败产物继续推演。
3. **上一轮完成但留未决事项**（清单已交付、条目未执行）——桥只给 goal+产出摘录，
   「继续」时模型靠自觉重读，无确定性的未决事项指针。

三方实证共识（调研已完成，见 §2）：紧邻衔接 = **状态延续/无条件注入**（OpenHands 把
goal 状态当一等事件持久化、dsh 的 CHECKPOINT_PREAMBLE 无条件回注 GoalPhase、Letta
core memory 无条件注入）；没有一家靠正则猜「用户是否要继续」。

**上线后新实例（2026-09-01，会话承接「按你推荐的执行吧」）**：该消息命中不了任何通道——
R4 桥的产出摘录（末 3 个 tool-result、head2048/marker/tail512）不含上一轮推荐正文
（closing 回复被防污染设计丢弃，推荐正文的权威副本在 `memory/清单.md`），而
「按你推荐的执行吧」无编号、无「上一轮」代词、无清单类名词 → R3 检测不命中 → 无任何
通道把清单指给模型。实例证明：**检测式口径永远有漏网措辞，唯一稳健口径=任务状态
无条件注记**（phase=active 时每轮都带未决事项指针 + 文件路径，模型可 read_file 取全文）。

## 1. 现状核实（本方案依据的代码事实，均已 read_file 验证）

| 事实 | 位置 |
|---|---|
| 终态 = {completed, failed, cancelled}；`waiting_user/external/agent` **非终态** | `state.py:15-26`（LifecycleStatus）、`checkpoint_side_effects.py:31` |
| 终态收尾 handler 机制：`terminal_handlers` 按注册序执行；`checkpoint_handlers` 每个已提交 checkpoint 都执行（含 waiting） | `checkpoint_side_effects.py:1036-1053`、协议 `RuntimeTerminalProductHandler`/`RuntimeCheckpointProductHandler`（:83-91） |
| waiting 交付 `kind="waiting"`，lifecycle.waiting_request 承载提问内容 | `checkpoint_side_effects.py:137-152` |
| direct chat waiting 后：用户回复走 **resume command 续接同一 run**（非新 run）→ 同 run 无桥、无「已完成」误报 | `chat_intake.py:311-326` |
| R1 收尾 handler 只处理 `completed`；waiting 的清单不落库 | `list_persistence.py:491` |
| direct chat 跳过 session delta 合并（D-015，thread 即唯一短时真相）；但 R1 指针仍写 `session_context_states.open_items`、R3 仍读（已实测生效） | `session_context_completion.py:144-151` |
| 桥 `_prior_run_summary` 固定措辞「历史上下文（非当前任务）：上一轮已完成」；唯一调用方 `context_builder.py:644`；R3 检索注记在同处 prepend | `thread_visibility.py:45-125`、`context_builder.py:644-659` |
| handler 注册点 | `worker_service.py:325-347`（checkpoint_handlers / terminal_handlers 两个元组） |
| 收尾 handler 可读写 session_context（load_snapshot + compare_and_swap，R1 同款） | `list_persistence.py:409-483` |
| 模型可见契约须同步 backend/AGENTS.md「Model-facing contracts」段 + 单测钉死 | `backend/AGENTS.md:126`、`test_list_persistence.py:286` 模式 |
| 文件即记忆模式（agent 可 read_file）：`memory/清单.md` 结构化 section + open_items 纯索引指针 | R1 已验证 |
| `AgentRun` **无执行状态列**（`delivery_status` 是交付态非执行态；执行状态只在 checkpoint） | `backend/app/models/agent_run.py` 全列已核 |
| feishu 卡片/一对一 run 亦 `source_type=="chat"`、thread==session（D-6 按 thread==session 自然涵盖，语义同 direct chat） | `backend/app/api/feishu.py:93,688`、`chat_intake.py:239-269` |

## 2. 三方实证（本地源码已核实）

| 参考 | 事实 | 借鉴点 |
|---|---|---|
| OpenHands（`~/Documents/UGit/software-agent-sdk/openhands-agent-server/openhands/agent_server/event_service.py`，`resume_goal_loop` :1563、`_last_goal_loop_status` 事件扫描） | goal 状态以 `ConversationStateUpdateEvent(key="goal")` **事件持久化**（重启可恢复）；`resume_goal_loop()` 显式续做 API；status 含 complete/capped 判不可续做 | 任务状态一等公民、确定性写 |
| deepseek-harness（`~/Documents/UGit/deepseek-harness`，`packages/compaction/compaction-basic/src/summarizer.ts:69`、`packages/goal/tool-goal/src/index.ts:92`） | Session=append-only 事件日志，一切投影派生；CHECKPOINT_PREAMBLE 摘要**无条件**回注；GoalPhase 枚举精确四值 `['active','paused','blocked','complete']` + continuation round | phase 语义集、无条件注入、过去时防误读措辞 |
| openai-agents-python（src/agents/memory/session.py，`get_items(limit=N)`） | Session=append-only items、最近 N 条回放、零额外机制 | 「回放即记忆」下限——本方案比它多一步确定性 phase 标注 |
| deepagents（`~/Documents/UGit/deepagents`，talon runtime `memory/AGENTS.md` 加载路径） | todo 工具 + 记忆=AGENTS.md（文件即记忆） | 文件通道可被 agent read_file 自引用 |
| LangGraph 官方 | checkpointer=thread 内短时（同 thread 自动延续）/ BaseStore=跨 thread 长时 | 本方案=checkpointer 层内的状态投影；跨线程留事故驱动 |

**共识**：紧邻衔接靠状态延续，不靠检测；远距衔接靠显式写入+检索（跨线程，已搁置）。

## 3. 方案：会话级任务状态（写入侧 + 注入侧）

### 3.1 数据结构

`memory/任务状态.md`（agent workspace 文件，数据权威，agent 可 read_file；每 agent 一份、
**每会话一节**、同会话最新替换；仿 `清单.md` 解析/渲染模式，外来内容原样保留）：

```markdown
## task:<session_id> | phase: active | ended: completed | 目标：<goal 截断 100 字符> | run:<run_id> | 2026-09-01 20:00
未决事项：list:<list_id> 清单「title」（N 项）
```

`session_context_states.open_items` 写入**纯索引指针** `{"task_state_ref": "memory/任务状态.md"}`
（不复制 goal/phase——权威在文件；**已核实 direct chat 的 open_items 无 LLM 写入方**：
后台 scanner 只扫 group（`session_context_background.py:124` 拒绝非 group）、无 API compact
入口、group-cutoff 重建仅 group 路径、D-015 跳过 direct 的 delta 合并——指针行只有确定性
写入方（R1 + 本票），重述风险为零，D5-1 论证在此场景自动满足；零 schema 迁移）。

### 3.2 phase 语义（D-8，确定性映射，无 LLM）

| run 落点 | phase | 语义 |
|---|---|---|
| `waiting_user/external/agent` | paused | 暂停，等回复/审批 |
| `failed` / `cancelled` | blocked | 未完成（已中断） |
| `completed` 且无未决事项（open_items 无 list 指针） | complete | 已完成 |
| `completed` 且有未决事项 | active | 已交付、仍有未决事项 |

未决事项 = 收尾时刻 open_items 中的 `list_ref==memory/清单.md` 指针行（direct chat 下
open_items 的唯一写入方就是 R1，即「清单已交付但条目未执行」）。`ended` 记录 run 落点
状态（completed/waiting_user/failed/cancelled），注记措辞按 **(phase, ended)** 派生——
blocked 相位因此可区分 failed/cancelled 措辞，phase 枚举本身保持 dsh GoalPhase
四相位不变。注：direct chat 的
open_items 无 resolve 方，指针长期留存 → phase=active 可持续多轮——这是期望语义
（「继续」时始终有据可依），文档化而非视为缺陷。注记中的清单 title/count 是任务状态
**写时刻的快照**，清单后续版本合并更新标题后可能漂移——注记只作指针语义，可接受。

### 3.3 写入侧（票 04）：两个挂点，补 waiting 缺口

新模块 `backend/app/services/agent_runtime/session_task_state.py`：

1. `SessionTaskStateWaitingHandler`（**checkpoint_handlers** 注册，`worker_service.py:325`
   元组内、PlanningCheckpointScheduler 之后）：`lifecycle.status` 以 `waiting_` 开头时
   → phase=paused 写状态（文件节 + 指针行）。补齐「waiting 无终态 handler」的缺口。
2. `SessionTaskStateTerminalHandler`（**terminal_handlers 注册，元组最后一位**，
   `worker_service.py:331` 元组末尾）：status∈终态时按 §3.2 映射写状态。**位置最后是
   硬约束**：须在 SessionContextCompletionHandler / ListPersistenceCompletionHandler
   之后运行，才能读到合并后的 open_items 决定 active/complete。

共同约束：仅 direct chat（`run.thread_id == run.session_id`，D-6）——group 会话的
任务语义（多 agent 交错、公开消息流）与「上一轮任务」概念不匹配，列为观察项；
幂等（同会话节整体替换，重放/重试收敛到同内容）；写失败 log 不阻塞收尾
（沿用 R1 的 best-effort 定位）；前置 handler 失败时（`checkpoint_side_effects.py:1042-1053`
错误只 append 不中断），TerminalHandler 读到的 open_items 可能欠账、phase 偏 complete——
fail-open 接受，不阻塞收尾；waiting 收尾的清单落库由票 06（D-12）独立承担，不在本票。

### 3.4 注入侧（票 05）：phase≠complete 口径，无条件注入（D-9）

`ContextBuilder.build`（`context_builder.py:644` 桥调用处）增加一次
`SessionTaskStateLoader.load(tenant_id, session_id, agent_id, current_run_id)`
（R3 retriever 同款：内部自开 session_factory，读指针行→读文件节→解析；任何失败
→ None 不阻塞开局）。

- **守卫**：`state.source_run_id == context.run_id` 跳过（同 run 的 resume/重放，
  消息本就全窗口可见，勿自我标注）。
- **桥活跃**（thread 有上一 run 消息）：`bound_current_run_window(..., prior_task_phase=…)`
  透传给 `_prior_run_summary`，固定短语「上一轮已完成」替换为 phase 措辞
  （D-11 契约常量 `PHASE_COMPLETION_PHRASES`）：
  - complete → 「上一轮已完成」（行为不变）
  - active → 「上一轮任务已交付，仍有未决事项」
  - paused → 「上一轮任务暂停，等待你的回复」
  - blocked + ended=failed → 「上一轮任务未完成（未成功）」
  - blocked + ended=cancelled → 「上一轮任务未完成（已取消）」
  - active 且未决事项非空：桥末追加指针行 `未决事项：清单「title」（N 项，见 memory/清单.md）`
    （有界）——「按你推荐的执行吧」实例的闭环路径，模型可 read_file 取全文
  - state 缺失（legacy）→ 默认「上一轮已完成」（向后兼容，参数缺省 None）
- **桥不活跃**（上一 run 已被 compact 移出窗口）且 phase≠complete：prepend 独立注记
  （R3 `render_retrieval_note` 同款过去时框架）：
  「历史上下文（非当前任务）：[phase 措辞]。任务「goal」。[未决事项：清单「title」
  （N 项，见 memory/清单.md）…，有界截断]」。
  phase=complete 时不注入任何注记（零增量，常见路径零成本）。
- 措辞红线：全部过去时、非祈使、「非当前任务」标记——命中
  [[direct-chat-run-boundary-fix]]，防被模型读成新指令。

### 3.5 决定记录

- **D-6 作用域**：写入+注入仅 direct chat（thread==session）；feishu 卡片/一对一 run
  同为 source_type="chat"、thread==session，自然涵盖（语义同 direct chat，卡片 waiting
  同样需要 paused 状态）。group 任务状态为观察项
  （群聊 short-term truth 是 group context pack，另立并行真相有冲突风险，同 D-015 论证）。
- **D-7 存储**：文件 `memory/任务状态.md` 权威 + open_items 纯索引指针。否决 open_items
  内联 goal/phase（D5-1：compactor LLM 重述会改写）；否决新表/新列（本票零迁移；
  若后续需要跨会话枚举检索再评估 store 形态）；**否决「build 时从 DB 派生」替代**
  （已核实 `AgentRun` 无执行状态列，状态只在 checkpoint——派生须每 run 开局读
  checkpoint，成本不可接受，且失去快照固化语义与 agent read_file 可读性）。
- **D-8 phase 映射**：见 §3.2 表。不引入 LLM 判定；「active=已完成但有未决事项」
  是产品语义决策，用户拍板点之一。
- **D-9 注入原则**：同会话 phase≠complete 即无条件注入（无检测正则、无检索工具、
  无 schema 膨胀——延续 D4 克制）；不新增模型工具。
- **D-10 容错**：写失败不阻塞收尾（log+continue）、读失败不阻塞开局（无注记）；
  重放幂等。
- **D-11 模型可见契约**：5 条短语（按 (phase, ended) 派生；blocked 区分 failed/cancelled，
  phase 枚举不变）+ 注记模板为契约常量，同步 backend/AGENTS.md「Model-facing contracts」
  段，单测钉死措辞（test_list_persistence.py:286 同款模式）。
- **D-12 waiting 清单落库纳入本票群（票 06）**：对齐 dsh append-only 事件日志哲学
  （一切投影派生、无「非终态不持久」）与 OpenHands 状态转换事件持久化（goal 状态非
  只在 complete 时记录）；R1 触发条件扩 waiting_*（`trigger_statuses` 参数泛化），
  收尾内容提取经 **`delivery.waiting_content()`** 权威提取器（已核实
  `waiting_request` 无 `content` 字段，实际字段 question/prompt/reason，
  `checkpoint_side_effects.py:33` 同款）；parse_numbered_list 拒收非清单内容天然安全
  （waiting 提问多数无清单则 no-op）。

## 4. 挂点与影响面（已核实）

- 新增文件：`session_task_state.py`（纯函数 + 3 个类：WaitingHandler / TerminalHandler / Loader）。
- 改动文件：
  - `worker_service.py`（三处注册：checkpoint_handlers 元组加 WaitingHandler（票 04）与
    waiting 清单落库 handler（票 06）、terminal_handlers 元组末尾加 TerminalHandler）；
  - `list_persistence.py`（票 06：触发条件扩 waiting_*、`_closing_content` 增
    waiting_request 分支）；
  - `context_builder.py`（构造参数 `task_state_loader`；build 内 load + 透传 phase +
    bridge-inactive 注记 prepend；挂点 :644-659 区域）；
  - `thread_visibility.py`（`bound_current_run_window`/`_prior_run_summary` 增
    `prior_task_phase: str | None = None` 参数；唯一调用方已核实仅 context_builder:644，
    test_thread_visibility 17 用例适配）；
  - `backend/AGENTS.md`（Model-facing contracts 段）。
- 不触碰：`model_visible_thread_messages`（其余调用方 run_compactor/model_step_service
  不受影响）、R3 检测/检索、LIST_NUMBERING_CONTRACT、waiting 交付链。

## 5. 验收

- **单测**：
  - `test_session_task_state.py`（新）：phase 映射纯函数全分支；文件节解析/渲染
    round-trip、同会话替换、外来内容保留；WaitingHandler 仅 waiting_* 触发、direct-chat
    守卫；TerminalHandler 四映射 + 指针行 upsert + 重放幂等；Loader 命中/缺失/fail-open/
    自 run 守卫；契约短语钉死。
  - `test_thread_visibility.py`：5 短语措辞（blocked 按 ended 区分）+ legacy 缺省 +
    桥活跃透传。
  - `test_list_persistence.py`（票 06）：waiting_* 触发落库 / waiting 提问无清单 no-op /
    completed 行为不变。
  - context build 集成：bridge-active 注记进桥、bridge-inactive 独立注记、
    complete 零注记（断言模型窗口内容）。
  - `test_agent_runtime_worker_service.py`：两处注册接线与顺序（TerminalHandler 位末）。
  - 全量 pytest + arch-guard.sh。
- **真实 checkpoint 重放**（clawith-graph-state-triage 规则，禁止只信合成消息）：
  取真实 direct-chat 多 run thread（含 545e8262 同款）导出 checkpoint 重放
  ContextBuilder.build，验证**无任务状态时的向后兼容**（legacy 措辞、零注记、模型
  窗口与部署前一致）与新签名（`prior_task_phase` 透传）在真实消息上的行为。
  旧 checkpoint 不含任务状态（新代码未部署时无写入），phase 注记/paused 落库的
  证据链 = 单测 + 部署后 §5 端到端场景，不由重放产出。
- **端到端（部署后）**：①「有哪些可优化」→清单→新消息「继续」→注记注入、模型延续
  不反问；②模型反问（waiting）→**清单落库**（票 06）→用户回复→同 run 续接、无「已
  完成」误报；③completed 无未决→窗口零增量（旧行为）。

## 6. 票映射

| 票 | 内容 | 依赖 |
|---|---|---|
| 04 | 会话任务状态确定性落库（waiting 缺口 + 终态映射 + 文件/指针） | 无 |
| 05 | phase-aware 桥措辞 + bridge-inactive 独立注记（注入口径扩展） | 04 |
| 06 | R1 扩 waiting 触发：waiting 收尾清单确定性落库（D-12） | 无 |

跨线程任务快照（store 泛化 / session mention 协议）**暂不立票**，事故驱动。

## 7. 范围外与观察项

- 跨线程（新会话）任务快照与显式 mention（用户裁定暂不做）。
- group 会话任务状态（D-6）。
- 未决事项执行完毕后指针清理（R1 版本合并已有替代路径，观察即可）。
- 部署：本票群与 39bad6c8 均待部署（红线：需用户明确说「部署」）。
