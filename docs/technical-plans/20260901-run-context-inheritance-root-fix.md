# 20260901 Run 间结论继承断裂 — 根治方案（v4，六维评审修正）

## 0. 问题定义（证据：2026-09-01 18:12–18:16 CST，会话 61c27271）

用户新会话连发「现在app 还有优化的吗？」（run A）与「做 1、2、3、5」（run B）。
run B 开局注入的历史上下文仅一行：

> 历史上下文（非当前任务）：上一轮已完成，任务「现在app 还有优化的吗？」，产出 tool-result://ce2b2a8d…、tool-result://7d77ecc7…。

模型 reasoning 原话（agent_run_events）：*"I don't have those tool results directly
accessible in this context."* → 重读代码、重排编号、反问用户（waiting_started），
用户问题无人接答。同一问题先后被回答三遍，产出三套编号不同的清单。

## 1. 根因（三层，已核实）

1. **线程内（run 边界）**：`thread_visibility._prior_run_summary` 把上一轮折叠成
   「goal + 死引用」——closing 回复被防污染设计刻意丢弃（注释原文 "it is never
   copied"），`tool-result://` 在模型窗口内不可解析。
2. **会话内（持久层）**：清单从不落持久层。事实：`session_context_states.open_items`
   语义本就是「未决事项」，且 `context_builder.py:367` 已有同会话注入通道——但
   **写入方只有 compactor 的 LLM 提取**（`session_context_compactor.py:56-77`），
   run A 结束时未触发压缩 → open_items 无人写；`memory.md` 只记「已完成」。
3. **跨会话（检索层）**：没有跨会话投影/检索通道；新会话开局上下文为空，
   runtime 检测到引用不可访问后唯一动作是反问用户。

## 2. 根治判据（方案必须满足；每条注明满足点）

1. **清单产生即结构化落库**：数据通道而非文本重述——产出时固定格式 + 收尾
   确定性解析（不经过 LLM 摘要），杜绝「摘要再失真」。（→ R1/R5）
2. **编号引用走持久数据解析**：模型拿到的是解析后的条目，编号漂移被结构消灭；
   平台侧 intake 解析列为观察项，不默认实现。（→ R1/R7）
3. **上一轮全部产物（清单/决策/未决事项/错误）有统一持久通道 + 按需检索通道**：
   检索式而非投喂式；检索由 runtime 自动执行（检测信号触发），模型工具为观察
   补丁——避免 schema 膨胀与开局全量注入的预算膨胀/再污染。（→ R3）
4. **引用不可访问时自动检索闭环**：检测 → 自动检索注入 → 模型回答；检索无果
   才降级 waiting 反问。（→ R3）
5. **重问幂等**：同一问题重问时清单版本化延续（复核+增补），编号不重排。
   （→ R1 清单版本化）
6. **前端历史可辨可搜**。（→ R6）
7. **验收端到端**：新会话「做 1、2、3、5」命中编号执行、不反问；不只靠单测。
   （→ §7）

## 2.5 决定记录（grill-with-docs 审查定案，2026-09-01）

- **D1 清单合并 key** = (agent, workspace 项目名, 清单类型)。项目名为纯函数判定：
  取本 run 工具调用路径中最频繁的 `workspace/<name>` 首段；判定不出则退化会话级
  key（仅同会话合并）。否决：语义向量 key（违背确定性原则、成本高）；仅同会话
  合并（治不了跨会话重问，正是本次事故形态）。
- **D2 格式约束层级** = 全局 run 开局 system 段注入，全 agent（红线「不灰度」；
  一行规则对非清单任务是无害常量）。
- **D3 R7 触发** = 事件驱动，不定阈值：上线后若再出现「编号引用 → R3 兜不住 →
  模型解析失败/反问」事故，立即实施 R7；零事故则永不做。
- **D4 检索通道形态** = 默认只做 R3 runtime 自动检索（唯一检索通道），**不给模型
  新增检索工具**（schema 零膨胀，保护前缀缓存，见 [[deepseek-cache-tool-schema-facts]]：
  运行时工具 59–214 个、约 90% 未用）。模型主动检索工具降级为观察补丁：上线后
  若出现「无指代词的隐含引用」失败事故（如用户说「把深色模式修了」，深色模式在
  上一轮清单里但消息无任何指代词），再补工具。
- **D5 六维评审修正（2026-09-01）**：
  1. 清单本体存 `memory/清单.md`（结构化 markdown，数据权威、agent 可 read_file）；
     open_items 只存索引指针（list_id+路径），避免被 compactor 的 LLM 提取管线
     改写（compactor 会重述 open_items、completion 按 json identity 全等合并，
     确定性结构混入会被破坏/留多版本）。
  2. R3 与 waiting 解耦：命中则注入、未命中则 no-op，不做 waiting 决策——
     防「做 1、2、3 个测试用例」类裸编号误报打断正常指令；waiting 兜底保持现状。
  3. R4 砍掉清单节（同会话引用已由 R3 覆盖：R1 在 run 收尾写入、下一 run 开局前
     已完成），只保留 tool-result 内联摘录；清单提取纯函数唯一化、只服务 R1。

## 3. 参考依据

| 参考 | 本地位置/出处 | 借鉴点 |
|---|---|---|
| deepseek-harness compaction 家族 | `docs/technical-plans/20260829-deepseek-harness-study.md`；源码 `/Users/shubinzhang/Documents/UGit/deepseek-harness/packages/compaction/` | pruner 确定性 head4096/marker/tail1024（**内容留在模型可见面**，不替换成死引用）；8 节结构化模板 + CHECKPOINT_PREAMBLE（防祈使句被当新指令，命中 [[direct-chat-run-boundary-fix]]）；`deriveMessages` 确定性纯函数投影思想 |
| LangGraph 官方 persistence/stores | `docs.langchain.com/oss/python/langgraph/persistence.md`、`stores.md` | checkpointer=线程内短时 / BaseStore=跨线程长时的**分层**；本方案三通道即此分层 |
| mem0 / Letta(MemGPT) | `/Users/shubinzhang/Documents/UGit/mem0`、`letta-code` | 记忆=事件→抽取→写入的确定性管线 |
| Clawith 既有机制 | open_items 生态（compactor 提取、completion resolve、context_builder 注入） | R1 复用现有通道，只补确定性写入路径 |
| Cognition《Don't Build Multi-Agents》 | 见 [[reference-projects]] | 克制：R2 工具、R7 解析均列为观察补丁/观察项 |

## 4. 架构：三层通道（LangGraph 分层落地）

```
通道一 线程内即时继承（短时，低延迟桥）
        run B 开局 ← run A 消息（thread_visibility 摘要升级，R4）
通道二 会话级持久快照（长时，数据权威）
        run 收尾 → memory/清单.md（清单本体）+ open_items 索引指针（R1）
通道三 跨会话自动检索（长时，检索式）
        runtime 检测引用信号 → 检索 open_items（本会话+跨会话）→ 注入（R3）
```

通道一是便捷桥（同会话紧邻 run，免工具调用）；数据权威在通道二；跨会话与
compaction 后场景靠通道三。三通道互补，任何一个单拎出来都不是根治。

## 5. 分层实施

### R1 数据层：清单产生即结构化落库（根治主干）

1. **产出时固定格式**（D2，提示词约束，为解析服务）：清单必须以编号列表输出，
   每条一行 `N. 标题 — 一句话说明`；散文式清单禁止。结构化责任前移到产生环节，
   收尾节点只做解析、不做摘要——消灭「事后 LLM 摘要再失真」。
2. **收尾确定性解析**（纯函数，dsh `deriveMessages` 思想）：run 收尾节点用规则
   提取 closing 消息中的编号行 → 组装结构化清单写入 **`memory/清单.md`**（数据
   权威，agent 可 read_file 直接引用）：
   ```markdown
   ## list:<uuid> | project: mydome1 | 标题：app 优化清单 | 2026-09-01 18:00
   1. 输入精度截断 — Calculator.kt:204 …
   2. 超大指数上限 — power() …
   ```
   同时在 `session_context_states.open_items` 写入**索引指针**
   `{"list_ref": "memory/清单.md", "list_id": "<uuid>", "project": "mydome1"}`——
   指针行内容极短，compactor 的 LLM 提取即使重述也低破坏；文件本体不在其管线内
   （D5-1）。
3. **清单版本化（重问幂等，判据 5 + D1）**：写 `memory/清单.md` 前先按 D1 key 查
   既有 list_id；命中则**延续**——新 run 的清单以「复核+增补」语义更新该 list
   （items 按 key 合并、编号延续、不重排），同一 list 只留最新版本，并同步替换
   open_items 中的指针行（R1 自行写全量 array，不依赖 completion 的 identity 合并）。
4. **同步写 agent 记忆**：即上一条——清单本体就是 `memory/清单.md`，与 open_items
   指针同源同 run 写入。
5. **挂点与现有生态的衔接（facts 已核实）**：open_items 现有写入方只有 compactor
   的 LLM 提取（`session_context_compactor.py:56-77/242-259`）；注入通道已存在
   （`context_builder.py:367/546`）；resolve 生命周期已存在
   （`session_context_completion.py:89-107`，确定性 identity 合并）。R1 只补两处：
   ①run 收尾的**确定性**解析分支（覆盖「未压缩时 open_items 无人写」的缺口，
   本次事故同会话也未继承即因此）；②跨会话检索（R3）。不新增迁移：open_items
   为 jsonb，指针行直接放入，schema 不变。

### R3 自动检索闭环（唯一检索通道，判据 3/4 + D4 + D5-2）

1. **检测信号**（确定性规则，在模型请求组装前执行，挂点 `context_builder.py:546`
   附近）：
   - 编号引用：用户消息匹配 `(做|完成|实现|改|执行|处理)\s*(?:[PpNn][-—]?|[#＃])?\s*[0-9０-９]`
     类编号序列（含「做 1、2、3、5」与字母/符号前缀形态「执行P2」「处理N3」
     「完成#4」；2026-09-02 上线后观察「执行P2」不命中，补全动词集与前缀形态）；
   - 历史指代词：含「上一轮/上次/之前/刚才」且语及清单/结论/优化/方案类名词。
2. **自动检索**：按 (agent, user, project) 查 `session_context_states.open_items`
   指针行，范围 = 本会话当前状态 + 同 agent×同用户最近 N 会话（N 缺省 5）；命中
   指针 → 读 `memory/清单.md` 解析条目（key+summary）。
3. **注入与继续（D5-2：与 waiting 解耦）**：命中 → 清单条目注入模型窗口（措辞=
   过去时「历史上下文（非当前任务）」防误读框架，同 `_prior_run_summary`），run
   照常回答；**未命中 → no-op，不做任何 waiting 决策**——waiting 兜底保持现状
   逻辑，R3 只是「有机会注入时注入」的纯增益，防裸编号误报打断正常指令。
4. **工具=观察补丁（D4）**：不给模型新增检索工具。上线后若出现「无指代词的
   隐含引用」失败事故，再补 `search_prior_context` 工具（复用 E 通道
   `search_experience` 骨架，`experience_retrieval.py:227/361`）。

### R4 桥升级：`_prior_run_summary` 修复死引用（过渡桥，保留防污染）

1. **砍掉清单节（D5-3）**：同会话引用已由 R3 覆盖（R1 在 run 收尾写入、下一 run
   开局前已完成，R3 检测信号在同会话同样生效），摘要不再重复提取清单——清单
   提取纯函数唯一化、只服务 R1。摘要保留 goal + 未决事项，另做一处修复：
2. **产物摘录**：prior tool 消息的 `result_ref` 经 `ToolResultStore.resolve` 内联
   dsh pruner 式 head4096/marker/tail1024 截断，替代 `tool-result://` 死引用
   （解析通道现成：`model_step_service.py:2138` 已用同一 resolve 读 skill 内容）。
3. 框架措辞不变：「历史上下文（非当前任务）：上一轮已完成…」过去时格式，
   保留 direct-chat-run-boundary-fix 的防误读设计。
4. 定位声明：桥只服务同会话紧邻 run；跨会话/compaction 后场景以 R1/R3 数据层
   为权威。桥的提取失败不阻塞（fail-open 回退旧格式）。
5. 锚点测试：`backend/tests/test_thread_visibility.py`（现有 10 用例，
   `test_summary_drops_prior_plain_assistant_reply_and_keeps_only_goal_and_artifacts`
   需改行为）。新增：tool-result 内联摘录 / 措辞仍防误读 / goal 与未决事项保留。
   影响面已核实收窄：`bound_current_run_window` 唯一调用方 = `context_builder.py:546`；
   `model_visible_thread_messages` 的其余调用方（run_compactor:229、
   model_step_service:1447/2261/2429）不被本次改动触及。

### R5 编号契约（软约束，为 R1 服务）

run 开局 system 段（dsh CHECKPOINT_PREAMBLE 风格，D2 全局注入）：
「如产出编号清单，必须每行 `N. 标题 — 一句话说明`；同一清单重问时延续原编号、
只增补不重排；用户以编号引用清单时，以上下文/历史检索注入的清单条目为准执行，
不得自行重排或猜测候选。」注意：这是软约束，不作为正确性依赖——正确性由 R1
的结构化数据 + R3 自动检索保证。

### R6 前端（判据 6，独立排期）

会话列表：时间 + 末条摘要 + 搜索；新会话首屏提示「新会话不继承上一会话上下文，
延续话题请回原会话」。

### R7 观察项（D3 事件驱动，默认不做）

平台侧确定性编号解析：intake 时把「做 1、2、3、5」按继承清单解析为条目文本注入
（纯函数）。触发 = 上线后再出现「编号引用 → R3 兜不住 → 模型解析失败/反问」
事故，立即实施；零事故则永不做。参照 Cognition《Don't Build Multi-Agents》的
克制，不为低频交互形态加平台级解析器。

## 6. 交付顺序

1. **R1**（数据层，根治主干；含清单版本化）——单测：收尾解析纯函数（编号行/
   表格/散文拒收）、幂等合并（重问编号延续、D1 项目 key 判定）。
2. **R3**（自动检索闭环）——单测：两类检测信号命中/误报、命中继续、未命中进
   waiting、跨会话隔离与 N 会话范围。
3. **R4**（桥升级）——test_thread_visibility.py 三用例。
4. **R5**（契约文本，随 R1/R3 一起上）。
5. **R6**（前端，独立）。
6. **R2 工具 / R7**（观察补丁/观察项，事故驱动）。

R1 完成后即可修复本事故场景（数据在，检索才有东西可查）；R3 让引用自动化；
R4 让同会话紧邻 run 零工具调用直达。

## 7. 验收

- **单测**：R1 解析纯函数（编号行/表格/散文拒收）、幂等合并（重问编号延续、
  跨会话 D1 key 命中）；R3 检测规则（编号引用/历史指代词/无信号不注入）、
  命中注入、未命中 no-op（不触发 waiting）；R4 用例（tool-result 内联摘录/
  措辞防误读/goal 与未决事项保留）；防污染旧用例全绿。
- **真实 checkpoint**（clawith-graph-state-triage 规则，禁止只信合成消息单测）：
  导出 thread `61c27271-fab8-4314-91c7-462d4f249a57` 真实状态，重放 run B 开局
  窗口，断言模型可见清单条目与解析后的工具结果摘录。
- **端到端场景**（判据 7）：新会话「做 1、2、3、5」→ 命中清单编号执行、不反问；
  同一问题重问 → 编号延续、清单版本化增补；「上一轮的清单还在吗」→ 自动检索
  注入回答。
- **防污染回归**：过去时「非当前任务」措辞不变；旧用例全绿；注入行不进 user
  指令语义。

## 8. 票映射

- R4 的产物摘录/结构化模板 → 并入既有 compaction 四票
  `.scratch/compaction-slimming/01-tool-result-pruning`、`02-structured-compact-prompt`。
- 新票：①R1 清单结构化落库+版本化（根治主干）；②R3 自动检索闭环（检测信号+
  跨会话检索+注入）；③R6 前端会话列表（独立）；④R2 工具（观察补丁，暂不排期）；
  ⑤R7 平台解析（观察项，事故驱动）。

## 9. 实施记录（2026-09-01，R1/R3/R4 已落地）

- **R1**：`backend/app/services/agent_runtime/list_persistence.py`（确定性解析、
  `memory/清单.md` 写入、open_items 指针、D1 版本化合并、`ListPersistenceCompletionHandler`
  run 收尾挂点）；编号契约常量 `LIST_NUMBERING_CONTRACT` 注入 run 开局 system 段
  （`model_step_service._prompt_messages`）。契约文本以本方案 §R5 为 owning
  contract，代码与文档必须同步更新（backend/AGENTS.md model-facing contracts）。
- **R3**：`backend/app/services/agent_runtime/cross_session_retrieval.py`（纯函数
  检测 + 检索 + 过去时注记），挂点 context build 打包路径，命中注入、未命中 no-op、
  不做 waiting 决策。检测词集按 §R3-1 原文（代词「上一轮/上次/之前/刚才」+
  同义词；名词「清单/结论/优化/方案」+同义词）。检索 project 退化：查询方
  project 判定不出 → 不过滤；指针行 project=None（落库时 D1 退化）→ 对任何
  已知 project 按通配兜底、exact 命中优先。
- **R4**：`thread_visibility._prior_run_summary` 经 `ToolResultStore.resolve` 内联
  摘录。**有意偏离 §R4-2**：摘录预算 head2048/marker/tail512（方案原 4096/1024），
  注释标注 token 成本纪律；框架措辞与防污染设计不变。
- **保护性参数**（非蔓延，简化为默认）：`MAX_SESSIONS_DEFAULT=5`、`MAX_INJECTED_ITEMS=20`、
  标题截断 40 字符。
- **验证**：单测 88（list_persistence 25 / cross_session_retrieval 26 / thread_visibility
  17 / worker 接线 20）；全量 3461 passed 1 skipped；arch-guard 通过。真实 checkpoint
  E2E 留待部署后按 §7 验证清单执行（本地运行容器为旧镜像）。

## 10. 上线后观察与补全（2026-09-02）

- 真实链路验证（会话 545e8262，「排出待办优先级」→「执行P2」）：R1 落库+
  open_items 指针、agent 自行 read_file 引用清单、edit_file 回注「✅ 已执行」、
  收尾版本化合并（新项延续编号 4-6）、R4 摘要无死引用——全链路生效，零反问。
- 检测盲区补全：goal「执行P2」不命中 R3（动词「执行」∉原词集且「P2」非裸数字），
  本次由 R1 文件通道兜底成功；为防换模型/长上下文后兜底失效重演反问，检测定义
  补全为动词 +{执行,处理}、编号支持 P/N/# 前缀（§R3-1 已同步）。未触发 R7
  （无「模型解析失败/反问」事故），属检测定义修复而非新机制。
