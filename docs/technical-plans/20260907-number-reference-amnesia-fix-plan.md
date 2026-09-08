# 编号指代断裂根治方案（对齐版）：清单线对齐 focus 范式迁移——显式工具 + 稳定编号即身份

- 日期：2026-09-07（第三版，对齐其他项目的「显式清单工具」路线；经独立评审修正——注入策略由「未完成项全量重注入」回摆为「有界注入 + 编号稳定」）
- 状态：已通过独立评审（有条件通过，7 项问题均已修正进正文）；仍待用户确认数据模型 §5 与分票节奏，确认后分票实施
- 事故对象：run `030f3687-7633-4e46-b47a-cc3507e6f75d`（agent 950a1943「Android 工程师 07」，session `b9ded136-fbc7-4ee2-8d5e-991a7d2fc3e6`，2026-09-07 13:51:28→13:56:04Z，部署 0b95c42b）
- 根因定位文档：`docs/analysis/2026-09-07-number-reference-amnesia-030f3687.md`
- 先例方案：`docs/technical-plans/20260904-number-reference-disambiguation.md`（R5 v2 三条款，已设计未合入）
- 定位口径：双源（PG 台账 + Langfuse `events_full` + 清单.md 文件 + 源码 read_file 核对）
- **相对第二版的变化**：第二版为「自由文本抽取架构上的结构性补丁（P1 呈现即落盘 + P2 注入最近呈现清单）」；本版按用户「跟其他项目对齐、方案更稳定」的要求，改为**复用 Clawith 内部已验证的 focus 范式做「清单线结构化迁移」**——引入显式清单工具（稳定编号即身份），**移除「从 closing answer 自由文本抽取」这一脆弱点本身**。

## 1. 裁决（开头）

**根治方案 = 把「清单」这条线做 focus 同款结构化迁移**，对齐 gptme/gemini-cli/codex 共同采用的「显式清单工具 + 稳定身份」范式：

> 清单身份不再由「自由文本抽取 + 数字续编」产生，而由**显式清单工具统一管理**——每条目一个稳定 `sort_order`（单调递增，永不重排）+ 语义 `key`。呈现编号、存储编号、注入编号三者天然同源，因为**它们都是同一个 `sort_order` 的渲染投影**，不存在第二套编号命名空间。根治靠「编号稳定 + 工具统一发号」；注入取**有界**活跃项索引（代码级上限），窗口外引用靠稳定编号一次 `list_list_items` 确定性解析——注入是否全量与根治无关（§4.3）。

- **核心动作（结构性）**：新增 `agent_list_items` 表 + `list_list_items`/`upsert_list_item`/`complete_list_item` 三个显式工具 + 有界注入（活跃项 title 索引，编号稳定）；**废弃 R1 的「closing answer 自由文本抽取 + 数字续编」+ 退役 open_items 清单 pointer**；改造 R3 注入、`session_task_state` 未决事项、`read_file` 拦截。
- **身份来源（堵新漂移轴）**：清单作用域 `project` 由**平台从当前 workspace 派生**（复用 R1 的 `extract_workspace_project`），条目身份 = `key`（模型提供 + `slugify_list_key` 归一化），编号 = `sort_order` per-(agent,project) 单调。**模型不发明任何身份**——否则「编号分叉」会平移成「清单分裂」。
- **这不是新发明，是复用**：Clawith 已经用完全相同的范式把 `focus.md` 迁移成了 `agent_focus_items` 表 + `list/upsert/complete_focus_item` 工具（详见 §3.2），已上线。清单线是这条路径的第二次应用。

第二版的 P1/P2 补丁**不采纳为最终方案**：它仍在「自由文本抽取」这条脆弱路线上打补丁（模型格式稍不合 `parse_numbered_list` 就抽取失败，§2 已证明这条路线本身产生三套编号分叉）。对齐其他项目的正解是**让清单身份由工具显式声明**，从根上消除抽取。

**诚实边界（§9）**：这是票级大改动（新表 + 3 工具 + 4 处改造 + 数据迁移），非一次性小补丁；但它复用 focus 已上线的范式，每一步有先例，风险可控。分票推进、每票独立可回滚（§8.0）。迁移期与迁移后行为变化（呈现编号=工具 sort_order、不再子集重编号）需 agent 工作流适配。

## 2. 根因（双源钉死，含三处结构性代码决策）

### 2.1 事实修正：8 项 P2「未落盘」的说法不准确

`memory/清单.md` `## list:4e6db819` 段**确实含有 8 项 P2**，落盘编号 **92–99**（`92. GitLab CI 缺失`、`93. 从未实际跑过 lint`、…、`99. allowBackup=true`）。R1（`ListPersistenceCompletionHandler`）正常工作。真正病根是**三套编号命名空间分叉**：

| 命名空间 | #1/#2 所指 | 来源（file:line 实读） |
|---|---|---|
| **呈现**（closing answer，13:51:19） | #1=GitLab CI 缺失、#2=从未跑过 lint（1–8 重编） | `chat_messages` assistant 原文（PG，`60d23fc7…`） |
| **存储**（清单.md list:4e6db819） | #92=GitLab CI、#93=lint（延续编号） | 清单.md line 100–101 |
| **注入**（R3，下一 run 首步输入） | #1=沙箱 git 仍不可用、#2=宿主侧 `.git` 完好（99 项前 20 项） | Langfuse `events_full` 消息[1] + `cross_session_retrieval.py` |

### 2.2 三层根因（结构性主因在第 1、2 层）

1. **呈现侧·编号漂移（结构性）**：上一 run 在 closing answer 用新编号 1–8 重聚合 8 个候选。R1 落盘按「延续编号」存 92–99——`list_persistence.py:217-236` `merge_list_items` **故意忽略 incoming 编号、从 max 继续**。呈现（1–8）与存储（92–99）必然分叉。
2. **呈现视图跨 run 无载体（结构性）**：`thread_visibility.py:45-134` `_prior_run_summary` **刻意不复制上一轮 closing assistant 回复**（`:55-58` 防 stale 污染），「1–8 呈现视图」跨 run 无载体。
3. **上下文侧·注入窗口截断 + 错源（结构性）**：`cross_session_retrieval.py:48` `MAX_INJECTED_ITEMS=20`、`:270` `section.items[:MAX_INJECTED_ITEMS]` 只注入前 20 项，且注入的是**累积清单的逐项编号**（head-20 = 原始优化清单 1–20），与呈现清单（P2 的 1–8）是两套编号。Langfuse 埋点：模型输入「GitLab CI」「从未实际跑过 lint」**0 次命中**（trace `1c63b25a…`）。
4. **解析侧·契约缺失**：`LIST_NUMBERING_CONTRACT`（`list_persistence.py:51-53`）把权威指向「注入的截断窗口」，模型静默锚定错误 #1/#2。

**一句话根因：清单身份由「自由文本抽取 + 数字续编」产生，编号在呈现/存储/注入三个环节各自重编，跨 run 无稳定载体 → 用户编号引用无法唯一命中。**

## 3. 参考对比（Phase 1，≥10 项目 + Clawith 内部已验证先例）

### 3.1 外部项目：显式清单工具 + 稳定身份（实读）

| 参考 | 事实（file:line 级，实读） | 对齐结论 |
|---|---|---|
| **gptme（设计锚点①）** | `tools/todo.py:68-71` `_generate_todo_id`=max+1 单调；`:136` 展示编号==存储 ID；`:237/:259` `Error: Todo {id} not found` 查无显式报错不猜 | 「编号即身份」+ 显式工具 + 查无报错 |
| gemini-cli | `tools.ts:944-947` `Todo{description,status}`；`write-todos.ts:60-61` 编号 `index+1` 渲染期重算、`:31` 整表覆盖 | 显式整表工具，编号是渲染投影 |
| codex（反例） | `protocol/src/plan_tool.rs:16-19` `PlanItemArg{step,status}` 无 id，Vec 顺序即身份 | 显式整表工具 |
| open-swe | `thread_ids.py:1-7` 身份由外部稳定号确定性派生 | 稳定身份取自稳定源 |
| mem0 | `configs/prompts.py:442` 更新沿用既有 ID；`:472` ADD-only | 稳定 ID + 只增补 |
| letta-code | `memory-constants.ts` 只读 `memory_filesystem` block；`memory_filesystem.mdx` 只暴露索引、正文按需读 | 结构化落盘 + 指针注入 |
| how_to_fix_your_context | `README.md:38-41` Clash/Confusion；`:89` Tool registry UUID mapping；`:144` Offloading 外置化 | 稳定标识 + 外置化 |
| Anthropic context engineering | structured note-taking；just-in-time + lightweight identifiers；「attention budget」最小高信号 token | 结构化 note + 指针 + 最小注入 |
| deepseek-harness compaction | 「the newest tool result remains verbatim」 | 最新结果逐字保留 |
| Claude Code best practices | plan mode「explore→plan→code」+ todo 是「剩余工作唯一真相」（code.claude.com/docs/en/best-practices） | 显式 plan/todo 工具 |
| 12-factor-agents（Factor 5） | 「Unify execution state and business state」——一份状态一个 owner，禁止跨系统复制状态（humanlayer/12-factor-agents） | 单一权威，退役重复状态 |
| **PenguinHarness（同生态 Agent 平台先例）** | `plugins/goal/hooks/lib.mjs:134-146` `GOAL.json` 单一权威状态文件 + 模型只写 `status` 字段；`:153-208` 每轮重注入目标原文 + 「PLAN.md 存续跨压缩」；`state/memory.ts:19-20` 只注入索引、正文按需读 + `:129-132` workspace key 平台从路径哈希派生。**无多条目清单工具**（grep 确认） | 单一权威状态载体 + 平台派生身份 + 索引注入、正文按需读 |

**共同结论**：所有主流实现都让清单身份**由工具显式声明、稳定、不随自由文本抽取漂移**；gptme 用数字 id（单调），gemini-cli/codex 用「整表 + 渲染序」。没有任何一个项目从 agent 回复里「抽取编号清单」——Clawith 的 R1 抽取是独有且已被证明会分叉的路径。12-factor Factor 5 进一步背书本方案「`agent_list_items` 单一权威、退役 free-text/文件/pointer 三份重复状态」的裁决方向。PenguinHarness 是比 gptme/gemini-cli 更有说服力的先例——它与 Clawith 同为「Agent 平台」（非单个 coding agent）、且是本机正在运行其上的开源实现：它用「GOAL.json 单一权威 + 每轮重注入目标原文 + 索引注入正文按需读」解决同构的「跨轮/跨压缩丢上下文」。但它**没有**多条目清单工具，且其目标身份是用户的一句话 objective——**天然稳定、无编号、无需抽取**，每轮重注入原文即可保身份。Clawith 清单与它的差别**不在 token 规模**：R3 现状已是「有界 title 索引」——`MAX_INJECTED_ITEMS=20`、只注 `number+title`（不含 description）、截断显式标注、全文走 `read_file`（`cross_session_retrieval.py:48/:270` + `render_retrieval_note:85-113`），token 早已被挡住。清单的真正病根是**身份靠自由文本抽取 + 数字续编**（三套编号分叉）。因此工具的必要性**不是「避免 token 爆炸」**，而是「**让清单身份像 objective 一样天然稳定**」：工具显式声明 + 平台铸号（`sort_order` 单调）= 消除编号分叉，DB 单一权威 = 退役 free-text/pointer 重复状态。其中注入策略取**有界注入**（活跃项 title 索引，代码级上限 `limit_active/max_chars`，见 §4.3），「工具拉全文」是取 description / completed 历史 / 跨 project 的结构化查询——token 高效只是附带收益，非工具存在的理由。

### 3.2 Clawith 内部已验证先例：focus 迁移（决定性，实读）

Clawith 已经把 `focus.md` 用**完全相同**的范式迁移成了结构化工具，已上线：

| focus 范式组件 | 实读位置 | 清单线对应物 |
|---|---|---|
| 模型 `AgentFocusItem`/`agent_focus_items` | `models/focus.py:13-41`；alembic `055_add_agent_focus_items.py`+`059` | `AgentListItem`/`agent_list_items` |
| **稳定编号** `sort_order=(max+1)` 单调（内部排序键） | `dao/focus_dao.py:84-98` | `sort_order` 单调、永不重排（focus 工具输出**无数字**、身份=key；**可见数字编号对齐 gptme** `_generate_todo_id`，是清单线新增设计，见 §4.2 注） |
| **语义 key** 身份 `slugify_focus_key` | `services/focus_service.py:42-47`；`UniqueConstraint(agent_id,key)` | `key` + `UniqueConstraint(agent_id,project,key)` |
| 工具 `list/upsert/complete_focus_item` | `builtin_tool_definitions.py:100/120/141`；`agent_tools.py:3991-4079` | `list/upsert/complete_list_item` |
| **查无报错** `focus_item_not_found` | `agent_tools.py:4074-4078`（= gptme `Todo not found`） | `list_item_not_found` |
| **read_file 拦截** `is_focus_file_path`→`focus_file_path_removed` | `focus_service.py:50-52`；`agent_tools.py:4095-4099` | `清单.md` → 引导用 `list_list_items` |
| **有界注入** `render_focus_context` | `focus_service.py:344-403`；`agent_context.py:614-626`（`limit_active=5, max_chars=1500`） | `render_list_context` **有界注入**（活跃项 title 索引，代码级上限对齐 `limit_active/max_chars`），但**落点在 R3 路径**（有 project），非 agent_context（见 §4.3） |
| 工具引导 prompt | `llm/caller.py:330-331` | 清单工具引导 |
| trim/路由注册 | `llm/tool_trim.py:37/48`；`llm/content_router.py:27` | 清单工具 trim/路由 |

**这就是答案**：「跟其他项目对齐、更稳定」的正解不需要发明新机制——Clawith 自己已经用 focus 验证过「自由文本 → 显式结构化工具」这条迁移路径，清单线照做一遍即可。

## 4. 根治设计（对齐版：清单线 focus 范式迁移）

### 4.1 数据模型与身份（结构性核心）

新增 `agent_list_items` 表（对齐 `agent_focus_items`，增加「分项目」维度）：

| 字段 | 说明 | 对齐 |
|---|---|---|
| `project` | **清单作用域（= 现有 R1 的 D1 merge key）**，平台从当前 workspace 名派生（`workspace/<name>/` 首段，复用 `extract_workspace_project`），**非模型提供、恒非空**；派生不出时合成 `session:<session_id>` | R1 `project` merge key |
| `key` | 语义标识（`gitlab_ci`、`run_lint`），模型提供 + `slugify_list_key` 归一化；`UniqueConstraint(agent_id,project,key)` | `focus.key` + `slugify_focus_key` |
| `sort_order` | **稳定编号**，per-(agent,project) `max+1` 单调递增、永不重排（**并发防护**：分配须行锁/唯一约束防撞号，见 §4.2 注） | `focus_dao.sort_order`（内部排序键）；可见数字编号对齐 gptme `_generate_todo_id` |
| `title` / `description` / `status`(`pending/in_progress/completed`) | 条目内容与状态 | `focus.title/description/status` |

**身份四条铁律**（对齐 focus 已验证机制，并堵住「模型发明身份」这一新漂移轴）：

1. **作用域 `project` 由平台派生，绝不由模型发明**——同一 workspace 名的清单跨 run 自动归并；若让模型自造 `list_id`，换 run 即清单分裂，等于把「编号分叉」平移成「清单分裂」，病根未除。`list_id` 只是 DB 代理键（`slugify(project)` 或主键），模型不可见、不可传。
2. **条目身份 = `key`（模型提供 + `slugify_list_key` 归一化）**——对齐 focus 的 `UniqueConstraint(agent_id,key)` upsert 机制：按 key 合并、不重复造条目。
3. **编号稳定性 = `sort_order` per-(agent,project) `max+1` 单调**——渲染编号 = `sort_order`，永不重排；呈现子集时**编号保留原 sort_order（不子集内重编号）**，因为编号就是身份，重编号等于换身份。注意 focus 的 `sort_order=max+1`（`focus_dao.upsert_item`）是 select-max 后 +1 insert、**非原子**（无锁/无唯一约束）——focus 里撞号只影响排序不影响身份（其身份=key、工具输出无数字），清单线的「可见编号=sort_order」让撞号直接破坏「编号即身份」，故清单线 DAO 须加并发防护（§4.2）。
4. **最小可写面（对齐 PenguinHarness `GOAL.json`「模型只写 `status`」）**——`upsert_list_item` 模型只写 `key`/`title`/`description`/`status` 语义与内容字段，**不得指定或修改 `project`/`sort_order` 身份字段**（身份字段干脆不出现在工具入参的可写集里：`project` 平台派生、`sort_order` 平台分配、`key` 平台 slugify）。模型提供语义、平台铸号——这是堵「模型发明身份」的第二道锁（第一道是第 1 条的作用域平台派生）。

**一个 (agent, project) 至多一张活跃清单**（对齐 R1 现状：project 即清单身份，唯一 kind=编号清单）。「候选 vs 执行」不再拆清单，如需分类用 `key` 前缀或标题区分（见 §9 非目标）。

### 4.2 显式工具（对齐 focus 三件套）

- `upsert_list_item(project?, key, title, description)` → `project` 缺省 = 当前 workspace（平台注入，模型不必发明）；返回 `"{sort_order}. {title} ({key}) — {description}"`，编号=工具分配的 sort_order。**最小可写面**（§4.1-4）：`project`/`sort_order` 身份字段不在模型可写集，模型只写 `key`/`title`/`description`/`status`。
- `complete_list_item(key, project?)` → **以 `key` 为唯一完成凭据**（对齐 focus 的 `complete_item(agent_id, key)`）；查无显式报错 `list_item_not_found`（对齐 gptme `Todo not found`）。**不收裸 `sort_order` 作为跨清单凭据**——`sort_order` 仅 per-(agent,project) 内唯一，跨项目裸编号歧义；模型只有编号时先 `list_list_items(project)` 把编号解析为 key 再完成。
- `list_list_items(project?)` → 返回 `N. title (key) — description`，N=sort_order，稳定排序；是完整清单的唯一入口。**默认只返回未完成项**（对齐 focus `list_by_agent(include_completed=False)`），可选 `include_completed=true`——工具返回保持 token 高效（Anthropic「tools return token-efficient info」）。

工具 schema/执行/trim/路由照抄 focus 三件套的注册点（§3.2 表）。

**清单线相对 focus 模板的两处必要加固**：

- **并发防护（发号原子性）**：focus 的 `sort_order=max+1`（`focus_dao.upsert_item:84-96`）是 select-max 后 +1 insert、非原子，但 focus 工具输出**无数字**（`agent_tools.py:4016/4020/4053` 输出 `- {title} ({key})`、身份=key），撞号只影响排序不影响身份；清单线「可见编号=sort_order」让撞号直接破坏「编号即身份」。故 DAO 分配须加行锁（`SELECT ... FOR UPDATE`）或 `(agent_id, project, sort_order)` 唯一约束 + 冲突重试，把 `max+1` 从「非原子读改写」升级为「原子发号」。
- **可见编号是清单线新增设计（非 focus 已验证）**：focus 的数字编号从未作为身份暴露给模型/用户（身份=key）；清单线为服务用户「#N」引用，**刻意**把 `sort_order` 渲染成可见数字编号。可见数字编号对齐 gptme `_generate_todo_id`，而非 focus 已有行为——§3.2 的「= gptme」表述在此澄清。

### 4.3 有界注入 + 稳定编号（对齐 focus 有界注入 + gptme 稳定编号）

根治「编号分叉」靠**编号稳定**（`sort_order` 永不重排 + 每项带 `key`），**不是靠注入全量**；注入是否全量与「编号引用能否唯一命中」无关——窗口外引用靠稳定编号做一次 `list_list_items` 确定性解析（查得准，不是猜），只是多一次确定性 tool call。

**注入落点走 R3 路径**（`cross_session_retrieval` → `render_retrieval_note`），那里有 `project`（`extract_workspace_project(thread_messages)` 派生，`context_builder.py:389`）；**不在 `agent_context.build_agent_context` 新开注入点**——后者无 project/thread 入参（`agent_context.py:531-538`），「当前 project」在提示词构建期不可得。

注入内容 = **有界活跃项 title 索引**（每项 `{sort_order}. {title} ({key})`，按 sort_order 升序），并标注「description / completed 历史 / 跨 project 见 `list_list_items`」。**token 硬边界是代码级上限**（`limit_active`/`max_chars`，对齐 focus `focus_service.py:344-403` 的 `limit_active=5, max_chars=1500`），**不依赖模型是否调用 `complete_list_item`**——completed 归档只是把「活跃项」压小，不是有界的必要条件；模型不调 complete（事故恰是失忆场景）时，有界注入仍在代码级上限内，失败模式是「降级」（窗口外引用多一次确定性 `list_list_items`），不是「失效」。

> **为何从「未完成项全量重注入」回摆为「有界注入」**（独立评审后修正）：用户的「全量=100% 命中」动机成立，但全量的 token 边界押在「模型正确调用 `complete_list_item`」这一**未验证行为依赖**上——focus 的 `complete_item` 是已上线先例，但清单线的 `complete_list_item` 是全新行为，事故恰是模型失忆场景，全量时若 complete 不被调用则「未完成项=全部项」持续膨胀，失败模式从「降级」变「失效」。根治的充分必要条件是「编号稳定 + 工具统一发号」，注入全量与否不改变这一点；有界注入 + 稳定编号在「编号唯一命中」上同样 100% 可靠，且 token 有代码级硬边界。注入位置本身位于 uncached 尾（`caller.py:566` `LLMMessage(dynamic_content=...)`），每请求付费、无前缀缓存摊销，token 越界是实打实成本——必须用代码级上限锁死。

### 4.4 移除 R1 抽取 + 退役 pointer + 改造下游

1. **废弃** `list_persistence.py` 的「closing answer 抽取 + `merge_list_items` 数字续编」——清单由工具显式声明，不再从回复抽取。
2. **退役 open_items 的清单 pointer（`list_ref`/`list_id`）**：R1 抽取废弃后，pointer 唯一写入方消失；三处读者（R3 `_collect_list_pointer_ids`、`session_task_state._load_pending_lists`/`_load_list_titles_and_counts`、R1 `_load_session_pointer_list_id`）全部改为**按 (agent, project) 直查 `agent_list_items`**，pointer 成为死机制、随 R1 一并删除。`agent_list_items` 成为清单唯一权威——同时消掉「pointer 与文件不一致」这一整类隐患。
3. **保留** `migrate_legacy_list_file` 一次性导入旧 `清单.md`（对齐 `focus_service.migrate_legacy_focus_file`，`:204-248`）。
4. **改造** `cross_session_retrieval.py` 的 head-20 数字索引 → 基于 `agent_list_items` 按 (agent, project) 直查、**有界注入（活跃项 N 项，代码级上限 `limit_active/max_chars`）按 sort_order 升序**（替换「head-20 只注前 20 项」的截断错源，但保留有界语义、把「数量魔法数 20」升级为「对齐 focus 的代码级上限」）、稳定编号渲染（`project` 复用现有 `extract_workspace_project` 从 workspace 派生）。
5. **改造** `session_task_state.py` 未决事项与 phase 投影（`_load_pending_lists`/`_load_list_titles_and_counts` 解析清单.md → 查表未完成项计数；`map_phase` 的 `has_open_list_items` 由「有无 pointer」改为「该 (agent, project) 有无未完成清单项」）。
6. **read_file 拦截** `清单.md` → 引导用 `list_list_items`（对齐 `is_focus_file_path`→`focus_file_path_removed`）。
7. **契约更新** `LIST_NUMBERING_CONTRACT` → 「清单用工具声明；作用域 project 由平台派生；呈现编号=工具 sort_order；不子集内重编号」。

## 5. 数据模型与条文（需用户过目）

### 5.1 `agent_list_items` 表（alembic 迁移，模板 `055_add_agent_focus_items.py`）

```
agent_id, project（清单作用域：workspace 名，平台派生、恒非空）,
key（语义 slug，模型提供 + slugify 归一化）, sort_order（稳定编号，per-(agent,project) 单调 max+1）,
title, description, status（pending/in_progress/completed）, created_at, completed_at
UniqueConstraint(agent_id, project, key)
```

`project` 的派生与降级：复用 R1 的 `extract_workspace_project`（`workspace/<name>/` 首段）作为作用域；派生不出（无 workspace 路径）时合成 `session:<session_id>` 作作用域（**恒非空**，避免 PostgreSQL 对 NULL 在 unique 约束下互不排斥、导致不同 session 的清单误并）。迁移旧 `清单.md` 的 `project=None` 段时，以旧 list_id 作合成作用域 `legacy:<list_id>`（见 §6-4）。作用域始终由平台定、不由模型定。

### 5.2 工具输出示例（编号=sort_order，永不重排）

```
92. GitLab CI 缺失 (gitlab_ci) — 当前无 .gitlab-ci.yml
93. 从未实际跑过 lint (run_lint) — 项目未接入 lint
```

（编号 92/93 为迁移后保留的 sort_order；即便这是某个子集/筛选视图，编号仍是 92/93，绝不从 1 重编号。）

### 5.3 契约条文草案（替代现行 `LIST_NUMBERING_CONTRACT`）

```
# Numbered Lists

清单（待办/候选/未决事项）必须通过 list_list_items / upsert_list_item / complete_list_item 工具声明与维护，
不得只写在回复文本或手写 memory/清单.md。清单作用域（project）由平台派生，不得自行发明 list_id。

- 编号即身份：每条目编号 = 工具分配的 sort_order（per-project 单调递增、永不重排）。向用户呈现清单时编号必须与
  list_list_items 输出一致；呈现子集/过滤视图时保留原 sort_order 编号，绝不从 1 重新编号。
- 解析优先级：用户以裸编号（如「先做 #92+#93」）引用清单时，按次序解析：①本对话注入的清单索引
  （= system 提示词动态尾注入的活跃项 title 索引，逐项 `{sort_order}. {title} ({key})` 原文）；②list_list_items 读出的当前清单。
  均以工具输出/清单索引的编号与条目原文为准，不得自行重排、猜测或补造候选。注入窗口有界（活跃项 N 项），
  窗口外编号引用（含 completed 项）不在注入索引里——此时用稳定编号做一次 list_list_items 确定性解析（查得准，非猜）；
  若查无该编号/key（如 completed 项已归档后仍被引用），如实说明该编号无对应未完成项并追问，绝不顺延到邻近编号。
- 完成凭据：complete_list_item 以 key 为凭据；注入索引每项已带 key，直接用 key 完成；只有裸编号且无 key 时才 list_list_items 解析。
- 兜底消歧：多份清单并存且无法唯一确定所指时，先复述候选条目（编号+标题，逐字引用）向用户确认后再动工；
  查无该编号/ key 时如实说明并追问，绝不顺延到邻近编号。
```

## 6. 测试与影响面

1. **DAO/service 单测**：`upsert_list_item` 分配 `sort_order=max+1`（per-(agent,project) 单调、永不重排）；`complete_list_item` 以 key 完成、查无返回 `None`；`list_list_items` 按 sort_order 稳定排序；`project` 派生——同一 workspace 名跨 run 归并到同一清单。
2. **工具单测**：`upsert/complete/list_list_item` 三工具 typed outcome；`list_item_not_found` 显式报错；`complete_list_item` 不收裸 sort_order（拒绝或要求先解析 key）。
3. **注入单测**：`render_list_context` 有界注入（活跃项 N 项、代码级上限 `limit_active/max_chars`、completed 不进注入）、带稳定编号、按 sort_order 升序、标注「description / completed 历史 / 跨 project 见 list_list_items」；超出上限截断 + 截断显式标注。
4. **迁移单测**：`migrate_legacy_list_file` 一次性导入旧 `清单.md`（含 92–99 编号 → sort_order 92–99 保留），幂等；`project=None` 段以旧 list_id 作合成作用域 `legacy:<list_id>`（避免不同 NULL 段误并）。
5. **契约断言**：`LIST_NUMBERING_CONTRACT` 含「编号即身份」「list_list_items」「sort_order」「project 由平台派生」。
6. **eval 用例（run-loop-eval）**：固化 030f3687 场景——清单由工具声明（sort_order 92=GitLab CI、93=lint），用户「先做 #92+#93」→ 解析到 GitLab CI + lint；用户「先做 #1+#2」（歧义）→ 复述追问，不得静默映射。

**影响面（爆炸半径，`mcp__codebase-memory__trace_path` inbound 实查）**：

| 改动对象 | callers（trace_path 实查） | 结论 |
|---|---|---|
| `ListPersistenceCompletionHandler`（废弃抽取） | 2：`worker_service.build_runtime_worker_components` + `runtime_worker_context`（terminal+checkpoint 两处注册） | 爆炸半径局限在 worker_service 注册点 |
| `parse_list_file`（清单.md 解析器） | 3 直接（`cross_session_retrieval.py:260`、`list_persistence.py:524`、`session_task_state.py:347`）+ 2 间接（`_SessionTaskStatePersistence._load_list_titles_and_counts`/`_load_pending_lists` 包装调用） | **R1/R3/未决事项三处共用**，废弃+改造恰好覆盖这三处，无第四处消费者 |
| `render_list_context`（注入模板） | 由 R3 的 `render_retrieval_note`（`cross_session_retrieval.py`）调用，有 `project` | 落点在 R3 路径（有 project），非 `agent_context.build_agent_context`（后者无 project 入参） |
| `CrossSessionListRetriever`（R3） | 2：worker_service 注册 + ContextBuilder 注入 | 改造局限在 R3 自身 |

新增 1 表 + 3 工具 + 1 注入；废弃 1 抽取 handler + 退役 pointer 机制；改造 3 处（R3/未决事项/read_file）；契约更新。**不触碰** checkpoint/durable run、exactly-once、前缀缓存结构、多租户隔离、飞书/WS 通道。所有改动对齐 focus 已上线的对应组件，无未验证的新机制。

## 7. 七角度评审（含负向探针）

### Q1 根因找的是否正确？——通过
- **正向**：三套编号分叉 + `_prior_run_summary` 丢弃呈现视图 + R3 head-20 错源 + 旧契约指向注入窗口，四者共同解释双源每个证据。
- **负向探针**：若根因只是「8 项未落盘」，清单.md 不应有「GitLab CI」——实测 line 100 有，推翻「未落盘」；根因修正为「身份由自由文本抽取产生、三环节各自重编」。

### Q2 根治方案是否正确？——通过
- **正向**：显式工具 + 稳定 sort_order 从机制上消除「三套编号分叉」——编号只有一套（sort_order），呈现/存储/注入都是它的投影。这是对齐 gptme「编号即身份」+ gemini-cli/codex「显式工具」+ Clawith focus 已验证范式的三重合证。
- **负向探针（删除测试）**：删掉「移除 R1 抽取、改工具显式声明」，问「自由文本抽取还在吗」——还在，编号分叉必然复发；故「显式工具替代抽取」是根治所必需。

### Q3 参考的资料是否正确？——通过
- **正向**：gptme/gemini-cli/codex 实读确认显式工具范式；**Clawith 自己的 focus 迁移（`agent_focus_items` + 三工具 + sort_order=max+1 + 查无报错 + read_file 拦截 + 有界注入）实读确认已上线**——这是最强证据：同库同栈已验证的迁移路径。
- **负向探针**：focus 迁移是否真的「已上线」而非纸面？`git log -S agent_focus_items` 命中多 commit（ad606146、c381d479 等）+ 工具在 `agent_tools.py:3991-4079` 实执行 + `agent_context.py:614-626` 实注入——确认已上线，非纸面。

### Q4 副作用与爆炸半径是否排查完？——通过
- **正向**：票级改动，但每个组件对齐 focus 已上线对应物（建表/DAO/service/工具/注入/拦截/迁移），无未验证机制；影响面 §6 已逐项列出。
- **负向探针**：我特意找「废弃 R1 抽取后，`parse_list_file` 的 5 个 callers 里谁会静默断裂」——`session_task_state._SessionTaskStatePersistence._load_pending_lists`（`:354-391`）与 `_load_list_titles_and_counts`（`:335-352`）现在直接解析 `清单.md`，若只废弃 R1 不改这两处，未决事项渲染会拿空清单导致 active/complete phase 投影错误。核下来：方案 §4.4-5 已把这两处改为查 `agent_list_items`，同步切换，无静默断裂；**并进一步核过「谁在写 pointer」**——pointer 唯一写入方是 R1 的 `_replace_pointer`（`:449-497`），R1 废弃后无写入方，故 §4.4-2 连 pointer 一并退役，三处读者全部改直查 DB，不留「读者还在读、写入方已删」的半残状态；数据侧 `migrate_legacy_list_file` 对齐 `migrate_legacy_focus_file`（`focus_service.py:204-248`，一次性、幂等、`on_conflict_do_nothing`）。

### Q5 这是最优且必要的方案吗？——通过
- **正向**：Ponytail 阶梯——纯 prompt（第一版）不能根治 → 抽取架构补丁（第二版）仍留抽取脆弱点 → 对齐 focus 的显式工具迁移（本版）才是机制根治。复用已验证范式，非重复造轮子。
- **负向探针**：能否只改 R3 注入「尾部 20 项」而不引入工具？不能——编号分叉仍在（呈现 1–8 vs 存储 92–99），只有「身份由工具统一发号」才能消除分叉本身。

### Q6 是否已经有可复用的逻辑？——通过
- **正向**：focus 全套（模型/DAO/service/工具/注入/拦截/迁移）就是现成模板，清单线是参数化复用，代码量约等于「再复制一份 focus 栈 + 加 project 作用域维度」。
- **负向探针**（反证复用声明，实读源码）：我核对「sort_order=max+1 单调」是否真是 focus 已上线代码而非我的转述——实读 `focus_dao.py:84-98`：`max_order = select(func.max(AgentFocusItem.sort_order)).where(agent_id==agent_id)` → `sort_order=(max_order or 0)+1`，确认为已上线实现（= gptme `_generate_todo_id` 同构）。差异点也逐条核过：focus 的 `where(agent_id==…)` 是**单 agent 全局单调**，清单需**per-(agent,project) 单调**（`where(agent_id==… AND project==…)`），即把排序作用域从 `agent_id` 换成 `(agent_id, project)`——这是唯一需要参数化的地方，已在 §4.1 `sort_order` per-(agent,project) 单调 + `project` 作用域显式写出，非遗漏。

### Q7 会破坏 Clawith 的特性吗？——通过
- **正向**：逐条过宪法 C1–C6——C1 证据先行（双源+源码+focus 先例）✓；C2 最小改动（复用 focus 栈）✓；C3 状态所有权（清单身份收归 `agent_list_items` 单一权威）✓；C4 测试证行为 ✓；C5 保留既有工作（focus 线不受影响）✓；C6 模块化边界 ✓。红线不触碰。
- **负向探针**（对红线逐条做反证尝试）：
  - 我试过把「废弃 R1 抽取」碰 `durable run / checkpoint` 红线——R1 **确实挂在 checkpoint 写路径上**（`worker_service.py:342-355` 注册于 checkpoint_handlers，terminal 也注册，共两处，见 §6），但它只是 completion 阶段的 best-effort 副作用（写 `清单.md`/pointer），**不参与 checkpoint 的序列化/反序列化本身**；废弃它须**同时移除 checkpoint + terminal 两处注册**（§4.4-1 已含），不触碰 exactly-once / checkpoint 语义 → **不碰**。
  - 我试过碰 `前缀缓存前缀` 红线——本方案只新增表+工具、改注入渲染，不改 token 前缀/缓存结构 → **不碰**。
  - 我试过碰 `session_task_state` active/complete phase 投影——`_load_pending_lists`(:354-391)/`_load_list_titles_and_counts`(:335-352) 现直接解析 `清单.md`，若只废弃 R1 不改这两处，未决事项渲染会拿空清单；方案 §4.4-5 已同步改为查 `agent_list_items` 未完成项（投影语义保留、数据源切换）→ **已覆盖，非静默断裂**。

## 8. 实施与门禁（分票推进，用户确认后）

### 8.0 可回退设计（Phase 3 生产级门槛：最小、可回退）

三票各自**独立上线、独立回滚**，互不耦合，任一票回滚不损坏数据、不拖累已上线票：

- **票①（表/DAO/service）**：纯新增，回滚 = `alembic downgrade`（删 `agent_list_items`）+ 删新增 DAO/service 文件；无既有行为变更。
- **票②（工具三件套）**：纯新增工具 schema/执行/trim/路由，回滚 = 移除工具注册（模型不再可见）；无数据副作用。
- **票③（注入 + 废弃 R1 + 退役 pointer + 改造 R3/未决事项/read_file + 契约）**：唯一改既有行为的票，回滚 = `git revert` 恢复 R1 抽取注册 + 恢复 pointer 机制 + 恢复 `清单.md` 解析路径 + 回退契约条文。
- **顺序保证**：票①→②→③ 依次上线；回滚票③ 只回到「工具存在但抽取仍在」的中间态，不产生数据损坏。
- **数据安全**：`migrate_legacy_list_file` 一次性导入且 `on_conflict_do_nothing`（对齐 `focus_dao.bulk_insert_legacy_rows:31`），幂等；导入只**读**旧 `清单.md` 不改写，回滚后旧文件仍在、可再导入。

1. **票①**：`agent_list_items` 模型 + alembic 迁移（模板 `055_add_agent_focus_items.py`）+ `list_dao.py` + `list_service.py`（含 `render_list_context`、`migrate_legacy_list_file`、`slugify_list_key`）。
2. **票②**：工具三件套 `list/upsert/complete_list_item`（schema + 执行 + trim + 路由，照抄 focus 注册点）。
3. **票③**：`agent_context.py` 注入 + 废弃 R1 抽取 + 改造 R3 + 改造 `session_task_state` + `read_file` 拦截 + 契约更新。
4. 每票：ruff / pyright 基线 / `scripts/arch-guard.sh` / 全量 pytest 全过。
5. 部署走 skill `clawith-prod-deploy`（清华源覆盖、回滚标签先打、check-inflight-runs 空才部署、红线不灰度全量、验收清单照跑）。
6. 上线后观察：真实「执行 N / 先做 #N」指令命中率（Langfuse 埋点）；若「多清单并存歧义」频发，评估注入时给清单加显式标题维度消歧。

## 9. 风险（诚实边界）

- **票级改动**：非一次性小补丁，需分票 + 灰度；但复用 focus 已上线范式，每票有先例。
- **行为变化**：迁移后呈现编号=工具 `sort_order`（可能 92–99 而非 1–8），用户从「先做 #1+#2」变为「先做 #92+#93」——这是**契约强制**（编号即身份），需 agent 工作流适配，不依赖模型自觉。
- **自由文本清单不再落盘（回归风险）**：废弃 R1 抽取后，若模型仍以自由文本产出编号清单、却不调用工具，该清单将**彻底丢失**（旧系统至少有「带编号分叉的落盘」）。缓解：契约 §5.3 强制 + 工具引导 prompt + read_file 拦截三重约束；上线后观察「自由文本清单未落盘」频率，若频发再评估注入「检测到编号清单但无工具调用」的补录提醒（**不恢复抽取**，只提醒）。
- **迁移期数据**：旧 `清单.md` 一次性导入；导入后 `read_file 清单.md` 被拦截引导用工具，旧文件不再读写。`project` 派生不出的 session 作用域段（合成 `session:<id>`/`legacy:<id>`）、`slugify_list_key` 键冲突（同 key 不同条目）在迁移时以 `on_conflict_do_nothing` 幂等处理。
- **「整表 vs 增量」+ 注入的 token 边界**：本方案取 gptme 式「增量 + 单调编号」（保留跨 run 累积能力，对齐现有「未决事项台账」语义），而非 gemini-cli 式整表覆盖。注入取**有界注入**（活跃项 N 项，代码级上限 `limit_active/max_chars`，§4.3），token 硬边界**由代码级上限锁死、不依赖模型调用 `complete_list_item`**（completed 归档只是把活跃集压小，非有界前提）；若某 project 活跃未完成项膨胀到数百项，代码级上限已挡在注入口，再按项目做 **DAO 级滚动归档**（`archive_completed_list_items(agent_id, project, before=…)` 或 maintenance 任务：把 completed 项移出活跃集/软删，`sort_order` 恒增不回收）作二级防线——归档是**代码机制**，不是「假设模型会调 complete」。
- **非目标（诚实声明）**：本方案只根治「编号身份分叉」，不引入「一个项目多张清单（候选/执行分表）」——现状 R1 就是 project 即清单、单 kind，保持之；如需分类用 `key` 前缀或标题区分，不在本方案范围。

## 10. Phase 5 待办

本方案为**纸面方案**，未实现。用户确认数据模型（§5）+ 分票节奏后实施，每票完成后跑 `code-review` 对照本方案复核 diff（Spec 轴：diff 是否忠实实现 §4 与 §7 裁决，无夹带范围外改动），才算交付闭环。
