# 2026-09-01 runtime 直写不刷新 + 熔断失效修复方案（缺陷 A/B）

状态：v5（二轮复审（v4 增量）完成，发现全部合入，定稿进入 TDD 实施）
关联事故：2026-09-01 run `be39c1ad`（thread 61c27271，agent 950a1943「Android 工程师 07」，
mydome1 计算器项目）。12:19:21→12:21:59 死循环，8 次 `workspace_sync_conflict`，
P0.5 熔断（上限 3）未触发，最终被用户 cancel 截断后 delivered。
关联部署：`63b70e91`（P0+P0.5，2026-09-01 ~09:46Z 上线，本次两条缺陷均为上线后新暴露）
关联 ADR：ADR-0011（`646be775`，run workspace 刷新 seam）
基线：本地开发 HEAD `40b27381`；生产部署 `63b70e91`。方案基于本地 HEAD 实施。
评审报告：会话 scratchpad `review-A-correctness.md` / `review-B-references.md` /
`review-C-production.md`。

## 修订记录

- v1：初版（2026-09-01）。
- v2：三路并行评审（正确性 / 参考项目对照 / 生产工程）后修订：
  1. §3 参考对照重写：6 个引用经本地仓库取证，2 个失准（deepagents、E2B/CubeSandbox）、
     2 个限缩（SWE-agent、openai-agents）、OpenHands 补 StuckDetector；补 gptme 遗漏；
     加「来源级别」列兑现红线自查；
  2. §2.1 增补「读取前 early-exit」实现要求（评审 C HIGH-1：refresh 的 no-op 判断在
     整文件读取之后，未物化 run 每次直写白付一次读）；
  3. §2.2 补 `K is None` 与 `S < K` 判定顺序、迁移首条消息即冲突的测试；
  4. §6 增补三条风险：move recovered `overwrite=true` 窄反例（F1）、未物化 run 读取放大、
     熔断误杀面（真人并发/并行会话/跨工具）；
  5. §5 验证补四项（正向压测信号、terminal run 监控、观察时长、生产数据再对账）；
  6. 三处排除理由措辞精化（F2/F4）、定位声明：本方案是「过渡方案」，单一写路径收敛
     落待办（评审 B 候选 1）。
- v3：用户确认走根治路线，不再分批。§2.3 新增 L1 写路径收敛设计（legacy 委托
  runtime outcome、`cas_guard` 保留审批盲写语义、前置校验保留、AST 写路径守卫测试）；
  §3 对比表更新（候选 1 转本版实施、L2 出局不留待办）；§4/§5 补收敛测试与审批流冒烟；
  §6 R6 改写为闭环声明；§7 新增 D6/D7、D3 随 L1 消解。
- v4：决策点按推荐全部确认并合入——D1 熔断收窄到执行类工具（§2.2
  新增 TOOL_SCOPE 常量与过滤，非执行工具冲突不计入不打断）；D2 暂不加同路径条件，
  §5 增冲突来源分布监控兜底；D4 waiting_user 显式纠偏同步清零冲突连击（§2.2 接线
  `_wait:1451-1474` + fresh budget helper）；D6 保留审批盲写（`cas_guard=False`）；
  D7 接受三处行为漂移。§7 转为已确认记录。
- v5（本版，定稿）：v4 增量二轮复审（A2/B2/C2）发现全部合入——
  1. D1 scope 剔除 android_compile（其失败只产 sandbox_execution_failed，从不产
     workspace_sync_conflict，A2/C2 双确认）；
  2. D7 修订：删除「write 6000 上限」漂移项（legacy `3292-3296` 早已有同常量同文案，
     委托前后行为相同，A2 P2-1/C2 H1 双确认）；补列 DB 异常漂移（`_execute_tool_direct
     `_execute_tool_direct
     :5697-5698` 现状返回含原始异常文本，委托后变 `_typed_unknown`）与文案下游面
     （execute_tool→llm/caller.py 回灌模型+流式前端，C2 M2）；
  3. 适配器复用既有 `_legacy_tool_outcome_text`（`3563-3575`，unknown→`⚠️` 而非 `❌`，
     A2 P3-1/C2 M1）；
  4. soul.md/tasks.json 保护标注 move-only（`3329-3330` + 服务层 `752-753` 双保险，
     A2 P3-3）；
  5. §2.3 补 session_id 透传要求（C2 L1）与守卫测试 import 绑定实现要点（C2 L4）；
  6. §4.3 测试清单扩为 7 类（含审批后 write/edit 必做用例，A2 P3-4/C2 双确认零覆盖）；
  7. §5 审批冒烟补盲写并发断言 + legacy 直连 write_file 冒烟；
  8. §3 两条 L1 引用精化（deepagents 两原语派生/gptme 单目录模型机制，B2 取证修正）。

---

## 1. 问题陈述（已核实的事实）

> 三路评审对 §1 行号级核实全部通过；生产数据事实（版本差 207、lifecycle 4 版本、
> 账本 8 例）属部署后取证，列入 §5 验证再对账项（评审 A F5）。

### 1.1 缺陷 A：runtime 路径 edit_file 直写后不刷新 run workspace → execute_code CAS 永久冲突

**现象（生产铁证）**：8 次 flush 的日志
`expected_version=…:7601`（恒定）vs `current_version=…:7808`（直写持续推进），
版本差 207 ≈ 21 次 edit_file + 5 次 write_file 量级；冲突文件每次相同
（CalculatorReducer.kt / CalculatorUiState.kt / CalculatorViewModel.kt），
严格命中被 edit_file 直写过的路径；第一次 flush `updated=['Calculator.kt']` 成功——
该文件没被直写碰过、manifest 是新的。

**根因链（行号已 read_file + 评审 A 双重核实）**：

1. run 早期 execute_code 首次物化 run workspace（`run_workspace.py:95 use_run_workspace`，
   进程内存 `_run_workspace_tasks`），manifest 基线定格在 7601 时代。
2. 之后模型改用 edit_file/write_file 直写 storage（版本推进到 7808），但**runtime 路径
   的成功分支不调用刷新钩子**：
   - chat run 的工具执行走 `tool_step_service.py` → `execute_builtin_tool_outcome`
     （`agent_tools.py:5168`）→ 四个 outcome 函数：
     `_write_file_outcome`(3823)、`_move_file_outcome`(4181)、`_delete_file_outcome`(4338)、
     `_edit_file_outcome`(4490)。
   - 四个 outcome 的成功路径均只有 persist_candidate → 直写 → discard_candidate → success：
     write `3962-3967`、move `4320-4335`、delete `4482-4487`、edit `4625-4633`，
     **无任何 `_refresh_run_workspace_after_direct_write` 调用**。
3. ADR-0011 的刷新钩子只挂在 legacy `_execute_workspace_mutation`
   （`agent_tools.py:3317/3348-3349/3375/3426`，调用方 autonomy_service.py:380 →
   `_execute_tool_direct`）——chat run 不走这条路。
4. 因此 run workspace 的 manifest/temp 文件停留在旧基线：后续每次 execute_code 结束的
   flush 逐文件 CAS 必败 → 模型死循环重试同一段代码 8 次（每次并行 read_file×4-5 侦察），
   12:22 后模型自愈改回 edit_file 完成。
5. contextvar 可用性已确认：chat run 经 command_worker 执行（command_worker.py:1008
   `sandbox_run_scope_id` 包住整个 run）；`tool_step_service.py:1575` 用 `create_task`
   （子任务继承 contextvar）→ outcome 内能读到 run_id；同 run 内 12:15:00/12:15:30
   android_compile 成功触发 `[RunWorkspaceRefresh]` 日志佐证补挂即可生效。

**影响面**：凡「run 内先 execute_code 物化 workspace、再 edit_file 直写、再 execute_code」
的顺序必然触发；这是最主流的编码工作流。冲突文案 + 3 连击熔断本应兜底，但熔断被缺陷 B 废掉，
于是表现为 2.6 分钟死循环（被用户 cancel 截断，否则可能不止 8 次）。

### 1.2 缺陷 B：P0.5 熔断器在生产未触发（消息级清零）

**现象（生产铁证）**：8 次冲突 > 3 上限，run 未 terminal、最终 delivered；
checkpoint 中 `workspace_conflict_budget={"version":1,"count":0}` 恒定（最终值 0）。

**根因链（行号已 read_file + 评审 A 双重核实）**：

1. `tool_repair_budget.py:70`：
   `is_conflict = execution_status=="failed" and error_code=="workspace_sync_conflict"`；
   第 71-72 行**任何非冲突消息**直接返回 `count=0` 的 budget——「连续」被定义为
   消息级连续。
2. `node_executor._tool`（`node_executor.py:993-1220`）每个 tool_call 一个 node task
   （`1025-1026` `current_call = calls[0]`，`1042-1046` `execute_pending(state, context,
   (current_call,))`），对 `result.messages` 逐条调
   `apply_workspace_sync_conflict`（`1069-1088`）。
3. 模型习惯是「execute_code + 并行 read_file×4-5」一条消息发出：execute_code 冲突消息
   计数 +1，紧接着同批 read_file 成功消息 → 清零。计数永远最多到 1，3 连击永远打不中。
4. lifecycle blob 只有 4 个版本（selective 持久化不落中间态）——budget 的中间计数
   本来就依赖确定性重算，与 b31f51ec 教训同构：合成单消息单测全绿，真实消息流打不中。

**关键结构事实（决定方案形态）**：`model_step_count` 在 `_model` 节点每次运行 +1
（`node_executor.py:724/747`，全 agent_runtime 唯一写点），即**一条 assistant 消息 =
一个 model step**；该消息产出的全部 tool_call（execute_code + 并行 read）在同一 model
step 内逐条排空。`_tool` 已把 `model_step` 传入 `apply_workspace_sync_conflict`
（`1082`）——调用点无需改动。

---

## 2. 修复设计

### 2.1 A：四个 outcome 成功路径补挂 refresh（复用 ADR-0011 seam）

**原则**：与 legacy `_execute_workspace_mutation` 完全对齐（`3317/3348-3349/3375/3426`）。
评审 A 确认：`.path` 字段存在（`WorkspaceWriteResult.path`，`workspace_collaboration.py:66-72`）；
四 outcome 在 `tool_step_service.py:1576` 锁外调用、`state.lock` 三处获取点均不与 outcome
同任务重叠 → **无死锁/重入风险**；execute_code 内部 flush 靠 `skip_workspace` 早退不重入。

| 挂点 | 位置 | 刷新调用 |
|---|---|---|
| write 成功 | `_write_file_outcome` `3962-3966` discard 后、`3967` return 前 | `_refresh_run_workspace_after_direct_write(agent_id, write_result.path)` |
| write recovered 分支 | `3943-3948` 返回前 | 同上（用 `normalized_path`，`write_result` 可能未绑定） |
| move 成功 | `_move_file_outcome` `4320-4334` discard 后、`4335` return 前 | 目标侧 `result.path` + 源侧 `normalize_workspace_path(source_path), deleted=True`（与 legacy `3348-3351` 同式） |
| move recovered 分支 | `4301-4306` 返回前 | 仅源侧 deleted=True（目标侧规范路径依赖服务端解析，异常路径不可得；窄反例见 §6 R3） |
| delete 成功 | `_delete_file_outcome` `4482-4486` discard 后、`4487` return 前 | `result.path, deleted=True` |
| delete recovered 分支 | `4464-4468` 返回前 | `normalized_path, deleted=True` |
| edit 成功 | `_edit_file_outcome` `4625-4629` discard 后、`4630-4633` return 前 | `result.path` |
| edit recovered 分支 | `4607-4611` 返回前 | `normalized_path` |

**实现要求（评审 C HIGH-1 增补）**：`_refresh_run_workspace_after_direct_write` 当前先
`get_version`+`read_bytes`（`2226/2245`）**之后**才在 `refresh_run_workspace_path` 里判断
run workspace 是否物化（`run_workspace.py:152-155`）——未物化 run（纯编辑 run、心跳、a2a）
每次直写白付一次整文件读（上限 50MB）。实现时暴露 `has_run_workspace(run_id)` 并在
`2220-2225` 读取**之前** early-exit；顺带把 `agent_tools.py:2228-2234` 的 is_dir/size
判断前移到读取前。刷新本体（`refresh_run_workspace_path`）不动。

说明：
- `result.path` 与 legacy 同源（`write_workspace_file`/`move_workspace_path`/
  `delete_workspace_file` 返回值携带服务端解析后的规范路径），edit 的成功消息已引用
  `result.path`（`4632`），无需自行重算目标路径。
- 目录 move/delete：钩子对 is_dir 目标自动 skip（`2228-2234`）；delete 的
  deleted=True 分支会移除 manifest 前缀条目（`run_workspace.py:177-192`）。
  与 legacy 行为一致，不扩大语义。
- `_typed_unknown` 分支不挂：不确定 outcome 不应提交 manifest 基线变更（尤其
  deleted=True 会抹掉 manifest 条目）；下轮 flush 的 CAS 保护仍是兜底安全网。
- 不做「flush 侧兜底」：真正原因是它会掩盖真实并发写（把冲突伪装成基线重算），
  且违背 ADR-0011「写时刷新、读时物化」的定位；全量 get_version 是次要代价。

### 2.2 B：熔断改为 model-step 级连续计数（budget v2）

**状态机**（纯函数 `apply_workspace_sync_conflict` 内改，调用点 `node_executor.py:1079-1084`
不动）：

- budget v2：`{"version": 2, "count": N, "model_step": K, "step_conflict": B}`
  - `K`：上一条已处理消息所属 model step；`B`：step K 内是否出现过 conflict。
- 对 step `S` 的一条消息：
  1. `S != K`（进入新 step，先结算旧 step）：旧 step 无 conflict（`not B`）→ `count = 0`
     （连续被干净 step 打断）；有 conflict → 保留（连续冲突 step 累积）。
  2. `K = S; B = False`。
  3. 消息是 conflict → `count += 1; B = True`；`count >= 3` → terminal。
- **判定顺序（评审 A F3）**：先判 `K is None`（v1 迁移，保留 count、只定 step），
  再判 `S < K`（fail-open 清零）——否则迁移首条消息可能误入 fail-open 分支。
- **迁移**：v1 budget（`{"version":1,"count":N}`）解析为 `K=None, B=False`；首次应用时
  `K is None` 保留 v1 计数（部署瞬间在途 run 的已计冲突不断档），随后按 v2 规则继续。
  解析失败（非 v1/v2、字段类型错）仍抛 `ToolRepairBudgetError`（现状 `41-49` 保持，
  解析器增加 v2 分支）。
- **熔断范围（D1 已确认：收窄到执行类工具）**：新增
  `WORKSPACE_SYNC_CONFLICT_TOOL_SCOPE = frozenset({"execute_code", "execute_code_e2b"})`，
  `is_conflict` 增加 `message.get("name") in scope` 条件（message 已带 `name`，
  tool_step_service.py:474）。convert/upload_image 等工具的 flush 冲突不再计入连击，
  也不打断连击（过滤后按普通非冲突消息处理）。android_compile 不纳入：其失败只产
  `sandbox_execution_failed`（agent_tools.py:5139），从不产 workspace_sync_conflict
  （评审 A2 核实）。
- **显式纠偏清零（D4 已确认）**：`node_executor._wait` 的显式纠偏块（`1451-1474`，
  现只 reset `tool_repair_episodes`）同步执行
  `lifecycle["workspace_conflict_budget"] = fresh_workspace_conflict_budget()`——新增
  helper 返回 v2 零值（`{"version":2,"count":0,"model_step":None,"step_conflict":False}`）。
  触发条件与 tool_repair reset 完全一致（waiting_user + 修复暂停 reason +
  `resume_type=="user_input"`），保证「用户显式纠偏 = 连击清零」与修复预算对称。

**生产回放推演**（12:19 循环）：step1 execute_code 冲突 → count=1；同 step 的 read 成功
消息不触发清零（`S == K`）→ step2 冲突 → count=2 → step3 冲突 → count=3 terminal。
死循环在第 3 次冲突即被平台截断，而不是等 8 次 + 用户 cancel。

**D2 已确认「暂不加同路径条件」**：真人/并行会话持续改文件 B 时，run 推进文件 A 也可能
被连击误杀（冲突路径目前只在 content 文本里无结构化字段，提取脆弱）。作为已知残余风险
入册 §6 R2，由 §5 冲突来源分布监控兜底：若生产出现该形态误杀，再启动结构化路径改造。

**为什么不选台账计数（agent_tool_executions 按 run 数）**：
- 台账是「总数」语义而非「连续」语义：一次 2 小时健康 run 里 3 次被真实竞态打散的孤立
  冲突会被误杀；
- 确定性重放是硬约束（deploy-kill-replay-divergence 教训）：budget 在 checkpoint 内、
  随 lifecycle 一起重放，台账计数则依赖「读取时刻的账本」且与执行写入的提交时序耦合；
- 热路径省一次 DB 读。graph-state-triage 的「run 隔离用台账」规则针对的是**跨 run 读别人
  状态**的探测器；本 budget 本来就每个 run 一份、隔离于 checkpoint，不适用该规则。

**文案**：`WORKSPACE_SYNC_CONFLICT_FAILURE_MESSAGE` 保持「conflicted 3 times in a row」——
新语义下（无干净 step 打断的连续 3 次冲突）表述依然准确。

### 2.3 根治 L1：写路径收敛——legacy 写分支委托 runtime outcome

**问题**：四工具的「直写+刷新」逻辑存在两份副本——legacy `_execute_workspace_mutation`
（`agent_tools.py:3271` 起，refresh 已挂 `3317/3348-3349/3375/3426`）与 runtime 四
outcome（本方案 §2.1 补挂）。「漏挂即回归」是结构性风险：本次事故正是其一漏挂。
仅补挂（L0）不消除该风险。

**调用方（已核实）**：`_execute_workspace_mutation` 仅两处调用——`_execute_tool_direct`
（`agent_tools.py:5644`，审批后执行路径，入口 `autonomy_service.py:380→384`）与
`execute_tool` legacy 兜底（`5852`，非 runtime 直连通道）。runtime 四 outcome 唯一入口
`tool_step_service.py:996`（评审 A/C 已核实）。收敛后：**所有通道的四个写工具 =
同一份「校验→直写→刷新→返回」核心**。

**设计**：

1. `_execute_workspace_mutation` 的 write/edit/move/delete 四分支**保留现有全部前置校验**
   （参数校验、soul.md/tasks.json 保护（仅 move 分支：`3329-3330` + 服务层
   `move_workspace_path:752-753` 双保险；write/edit/delete 本无）、focus/enterprise_info
   拦截、edit 的 old_string 存在性预检——这些仅存在于 legacy，runtime 模型通道不需要），
   仅把「直写+刷新+返回」委托给四个 outcome 函数，经适配器转字符串：复用既有
   `_legacy_tool_outcome_text`（`agent_tools.py:3569-3574`，success→`✅`、failure→`❌`、
   unknown→`⚠️`）或新增等价 `_outcome_to_legacy_text` 对齐其映射。
2. **legacy 语义保留（D6）**：outcome 的 write/edit 恒走 CAS（`expected_version_token`/
   `require_absent`，`workspace_collaboration.py:549-550/593-596`）；legacy 是「审批后
   盲写」语义。给 `_write_file_outcome`/`_edit_file_outcome` 加
   `cas_guard: bool = True`，legacy 传 `False`（不传版本条件、`require_absent=False`）；
   move/delete 在 scope=None 时本就不带版本条件，无需处理。审批窗口内并发修改的行为
   与现状一致（无回归）。
3. **reconciliation 天然跳过**：legacy 传 `tenant_id=None`/`runtime_run_id=None` →
   outcome 内 `if reconciliation_scope is not None` 守护全部生效（已核实四处），
   不产生 candidate。
4. **legacy 原 refresh 调用删除**（收敛后由 outcome 统一承担，刷新点物理上只剩一份）。
   **session_id 透传（评审 C2 L1）**：两个调用方分别透传各自现值——
   `_execute_tool_direct:5649` 传 None、`execute_tool:5857` 传 session_id——委托时显式
   保留，勿统一成 None（否则 legacy chat 丢 session_id）。
5. **写路径守卫测试（最终防线）**：新增 `backend/tests/test_write_path_guard.py`，
   AST 扫描 `agent_tools.py`，断言 `write_workspace_file`/`move_workspace_path`/
   `delete_workspace_file` 的直接调用点 ⊆ {四 outcome + 白名单 helper}——未来任何
   新写路径绕过 outcome 都会在 CI 红灯。实现要点（评审 C2 L4）：白名单按**函数名**匹配
   并解析 import 绑定（防别名/间接调用逃逸造成 false-negative）；「自身反例」断言只覆盖
   直接调用形态，别名逃逸不在反例内（已知局限，记录在测试 docstring）。其他模块
   （人类 API 上传等）不受限：无 run scope，contextvar 下刷新天然 no-op。

**如实列出的行为差异（D7，评审 A2 修订）**：

- 成功文案措辞：legacy 从 `"✅ {write_result.message}"` 变为 `"✅ {outcome content}"`
  （如 "Workspace file saved and verified."）——审批/直连工具的用户可见文案轻微变化；
  常见失败路径（参数错、old_string 不存在、ambiguous、protected）因前置校验保留而
  **文本不变**。下游面（评审 C2 M2）：`execute_tool:5851` 被 `llm/caller.py:429` 调用，
  返回字符串回灌模型（`caller.py:400-406`）并流式推前端（`453-462`）——直连通道文案
  变化对模型可见；审批 result 仅进 web 通知 body（`autonomy_service.py:246-247`）与
  日志，不落库不进飞书。均为低风险。实施时核实 caller 通道在生产是否仍活跃。
- DB 异常漂移（A2 P2-2 补列）：现状 `_execute_tool_direct:5697-5698` 捕获异常返回
  `"Error executing {tool}: {e}"`（保留原始异常信息）；委托后 outcome 吞异常改返回
  `_typed_unknown`（`3954-3957`），原始异常文本丢失、unknown 走 `⚠️` 映射。审批流
  排障信息降级，低频可接受。
- edit 双读：legacy 前置校验读一次、outcome 再读一次（审批流低频，可接受）。

**不做 L2**（刷新下沉写服务层 post-commit）：refresh 必须发生在 DB commit 之后
（读 storage 版本须见持久态），而 commit 由调用方执行——下沉需改提交契约或 session
after-commit 钩子，事务时序与 hidden coupling 风险大于收益；「新增路径漏挂」已由
守卫测试确定性覆盖。L2 出局，不留待办。

---

## 3. 参考资料对照（经本地仓库取证 + 来源分级）

> 红线自查结论（评审 B）：v1 的对比范围形式上未退化（过线），但 6 个引用 2 失准、
> 2 需限缩、未标注来源级别——红线问题在「覆盖了却没核实、没标级」。本版逐条落到
> 本地仓库文件/行号或明确标注转述。

| 问题 | 参考 | 借鉴点 / 差异 | 来源级别 |
|---|---|---|---|
| Agent 死循环熔断 | OpenHands StuckDetector | `stuck.py` 五种 loop_type → `AgentStuckInLoopError` → loop recovery，与 P0.5「连续熔断」同构（V0 时代 max_iterations 已被 `#14060` 删除，只作历史） | 本地核实：OpenHands 仓库 `openhands/controller/stuck.py`、`state.py:120-122`（git 历史） |
| 循环计数状态放哪 | langgraph `examples/code_assistant` | 迭代计数放 state、随 checkpoint 确定性重放——与 budget v2 放 lifecycle 同模式；但原例是硬编码 `max_iterations=3` 图迭代计数，非「连续失败」语义，仅限「计数器进 state」层面借鉴 | 本地核实：`langgraph/examples/code_assistant/langgraph_code_assistant.ipynb`（已归档） |
| 单一写路径（根治方向参照） | deepagents `SandboxBackend` | 文件工具由 `execute()`/`upload_files()` 两原语派生（docstring「all other operations are derived from those」＝一份核心＋其余派生），是 L1「单一核心」的现成范例；其 BackendProtocol 是**多后端可插拔路由**，与 Clawith「双代码路径」异质，仅取「单一原语核心」这层对照。v1 写的「直写无第二视图」不实（默认 `StateBackend` 存 graph state、`CompositeBackend` 显式多视图路由） | 本地核实：deepagents `sandbox.py:7-11` docstring、`write():1596`/`edit():1639`、`protocol.py:404/870`、`graph.py:629` |
| 文件工具与执行环境一致性 | gptme 单 workspace 目录 | 无沙箱单目录模型：文件工具走 `os.chdir`/`Path.cwd()`（save.py:137、read.py:183、patch.py:336），shell 走 `_workspace_cwd` ContextVar（shell.py:1084，仅驱动 shell cwd；gptme 自注文件工具迁移未完成）——从根上杜绝双视图漂移，贴合缺陷 A | 本地核实：gptme `tools/save.py:137`、`shell.py:1084`、`session_step.py:736-739` |
| 工具接口统一（ACI） | SWE-agent | ACI 是「模型侧接口统一」（docs/background/aci.md、`tools.py` ToolHandler），非「存储/执行单视图」——仅限此层面借鉴；其文件编辑=沙箱内专用工具（`str_replace_editor` 脚本）是「全部经沙箱执行」的极端范例，恰为 L1 否决的备选方向，留作对照 | 本地核实：SWE-agent 仓库 docs/background/aci.md、`tools/edit_anthropic/bin/str_replace_editor` |
| 运行期护栏（tripwire） | openai-agents-python guardrails | `guardrail.py` tripwire_triggered→halt 属实，但它是语义护栏一次性判定非连续计数——仅在「halt-rather-than-retry 定位」层类比成立 | 本地核实：openai-agents-python `src/agents/guardrail.py:31` |
| （删除 E2B/CubeSandbox 行） | — | E2B 是单文件系统直写（无第二视图），CubeSandbox statesync 是 VM 生命周期状态——均无「工作区双视图同步」同构机制，类比空泛 | 本地核实后判定不适用 |

**最佳方案对比（缺陷 A，评审 B）**：

| 方案 | 代价 | 风险 | 结论 |
|---|---|---|---|
| 候选 0（补挂 refresh） | 8 挂点 + recovered 分支信息不全 | 漏挂即回归（本次事故正是 legacy 挂了 runtime 没挂） | 本版 §2.1 实施，作为 L1 的落点 |
| 候选 1（单一写路径收敛） | legacy 委托 outcome + `cas_guard` + 前置校验保留（§2.3） | 行为漂移面（D6/D7）已如实列出 | **本版实施（§2.3）** |
| 候选 2（flush 侧权威重建） | 全量 get_version、掩盖真实并发写 | 一致性窗口放大、违背 ADR-0011 | 已拒绝（v1 即拒绝，评审确认理由成立） |
| L2（刷新下沉写服务层 post-commit） | 改提交契约或 session after-commit 钩子 | 事务时序 + hidden coupling | **出局，不留待办**（新增路径由守卫测试覆盖） |

---

## 4. 测试计划

### 4.1 缺陷 A（复用 `test_agent_tools_storage_workspace.py` 现有设施）

现有设施：`_patch_storage` / `_patch_workspace_db` / `_materialize_run_workspace` /
`sandbox_run_scope_id.set`（legacy 用例 `988-1021` 已用全套）。

新增 4 例（走 runtime 入口 `execute_builtin_tool_outcome`，传 `runtime_tenant_id` +
`runtime_run_id` 使 reconciliation scope 生效）：

1. `test_write_file_outcome_refreshes_run_workspace`：物化 → write_file 直写 →
   校验 flush 无 conflict 且内容以 storage 为准。
2. `test_edit_file_outcome_refreshes_run_workspace`：与 legacy 用例 `988-1021` 同构造，
   但走 runtime 入口（这是生产事故路径，必须有专门覆盖）。
3. `test_move_file_outcome_refreshes_run_workspace`：move 后 flush 无 conflict，
   目标在 manifest、源已移除。
4. `test_delete_file_outcome_refreshes_run_workspace`：delete 后 manifest 条目消失，
   flush 无 conflict。

新增 1 例（评审 C HIGH-1）：

5. `test_refresh_early_exit_without_run_workspace`：无 run scope（或未物化）时刷新
   调用**不读 storage**（用调用计数断言 early-exit 在读取之前）。

### 4.2 缺陷 B（`test_workspace_publication_filter.py`，纯函数）

- 保留 `test_workspace_sync_conflict_breaker_trips_at_limit`（不同 step 的连续冲突，
  新语义下仍 3 次 terminal）——**不修改**。
- 保留 `test_workspace_sync_conflict_breaker_resets_on_other_tool_result`
  （冲突 step → 干净 step → 冲突，清零重启）——**不修改**。
- 新增「生产模式回归」（b31f51ec 教训的直接转化）：
  `test_breaker_survives_parallel_reads_within_same_step`：每 step =
  [execute_code conflict + read_file success]，3 个 step → terminal True
  （旧实现下此用例到不了 terminal，是本次缺陷 B 的精确编码）。
- 新增：v1 → v2 迁移（v1 `{"version":1,"count":1}` 续计两条冲突 → terminal；
  `count` 保留不丢）；**迁移首条消息即冲突**（`K is None` 判定顺序回归，评审 A F3）；
  异常 budget（version=99 / count 非整型）仍抛 `ToolRepairBudgetError`。
- 新增：`S < K` 防御路径 fail-open。
- 新增（D1 确认）：非执行类工具（convert/upload_image）的冲突消息不计入连击、
  同 step 内不打断既有连击（`[execute_code 冲突, convert 冲突, read 成功]` 同 step →
  只计 1）。
- 新增（D4 确认）：`fresh_workspace_conflict_budget()` 返回 v2 零值；零值 budget 下
  第一条冲突计 1 且不 terminal（清零语义正确性）。

### 4.3 写路径收敛（L1，§2.3）

- 适配器单测：复用 `_legacy_tool_outcome_text`（`3563-3575`）三态映射
  （success→`✅`、failure→`❌`、unknown→`⚠️`），断言委托路径三态正确。
- 委托行为测试（复用 legacy 现有测试设施，评审 C2 扩为 7 类）：
  1. `cas_guard=False` 盲写：预置「文件在审批窗口被并发修改」，写仍成功（与现状一致）；
  2. edit/move/delete 三工具委托各自成功路径（含 delete 的 deleted=True 刷新）；
  3. protected 路径仍被拦（soul.md/tasks.json，move-only，委托后不因 outcome 无此校验
     而放行）；
  4. scope=None 不产生 reconciliation candidate（断言 candidate 未被 persist）；
  5. 三态映射（success/failure/unknown → `✅`/`❌`/`⚠️`）；
  6. session_id 透传（`execute_tool` 调用方 session_id 传入 outcome，不丢）；
  7. 审批后执行 write_file、edit_file 各 1 例（成功 + 参数失败映射）——现有三个
     autonomy/approval 测试文件对委托路径**零覆盖**（评审 A2/C2 双确认），此项必做。
- 守卫测试：`test_write_path_guard.py` AST 断言（白名单按函数名 + import 绑定解析；
  含自身反例——临时插入违例调用点断言必须失败，防止守卫测试写成恒真）。

### 4.4 不做的测试

- node_executor 端到端仿真（成本高、纯函数语义已覆盖）；生产验证以 §5 账本/日志为准。
- 熔断触发后 terminal 报错路径：P0.5 已测（`test_workspace_publication_filter.py` 现有
  覆盖），本次未改该路径。

---

## 5. 部署与验证

1. `scripts/arch-guard.sh` 过检；只 `ruff check` 改动文件（**禁止对 agent_tools.py 跑
   ruff format**，避免整文件重排噪音）。
2. 测试环境红线：不灰度、一步全量；`scripts/deploy.sh --commit <ref> --require-idle`。
   部署前 `git status` 核对剥离他人改动（多会话并行）。
3. 部署后验证：
   - **主账本**：`SELECT count(*) FROM agent_tool_executions WHERE
     result_metadata->>'error_code'='workspace_sync_conflict' AND started_at>部署时刻`
     → 应 0；若 >0，检查同 run 是否在 3 次内熔断 terminal（`agent_runs.delivery_status`
     出现 `workspace_sync_conflict_limit_reached` 失败 reason）。
   - **日志**：`docker logs clawith-agent-backend-1 --since 部署时刻 | grep -E
     "RunWorkspaceRefresh|WorkspaceFlushConflict"` → 应看到 runtime edit_file/write_file
     后的 `[RunWorkspaceRefresh] refreshed path`，且不再出现恒定 expected_version 的
     flush 冲突。
   - **正向压测信号（评审 C MED-4）**：统计部署后「同 run 内先 edit_file/write_file、
     后 execute_code」的 run 数，其 execute_code 冲突须为 0——这是修复生效的直接证据，
     「总数 0」无法区分「生效」与「恰好没走该路径」。
   - **负向信号**：`workspace_sync_conflict_limit_reached` 的 terminal run 监控
     （0 = 熔断未被误触发；>0 需立即检查是否误杀健康 run）。
   - **观察时长**：至少覆盖与 be39c1ad 同形态的 Android 编码任务 2-3 个 run 或 24h。
   - **冲突来源分布监控（D2 兜底）**：若部署后出现 conflict，按 tool_name/路径分布
     区分「模型重试死循环（应被熔断截断）」与「真人并发/并行会话改写（健康 run 不应
     被误杀）」；出现后者即启动 D2 的结构化路径改造。
   - **生产数据再对账（评审 A F5）**：以账本直方图 + checkpoint blob 复核 §1.1 版本差
     207 与 §1.2 budget 恒 0 的证据（属部署后取证，代码无法独立验证）。
   - **审批流冒烟（L1）**：测试环境触发一个 L2 审批写动作（write_file 或 edit_file），
     验证「审批→执行」成功且用户可见文案可读（关联记忆：删除审批转发 PenguinHarness
     会话）；**并断言盲写语义**：审批窗口内并发修改文件后，执行仍成功（cas_guard=False
     不被 CAS 拒）；同时对 legacy 直连路径（execute_tool）冒烟 write_file 一次。
   - **守卫测试进 CI**：`test_write_path_guard.py` 随 pytest 全量自动运行，红灯 =
     新写路径未收敛（部署前本地全量测试必须含它）。
4. 回滚：标签 `clawith-agent-backend:pre-63b70e91-2ab25eb38c8c` 保留；本次部署前由
   deploy.sh 打新 pre 标签（`pre-<新commit>`）作为回滚点。

---

## 6. 风险与取舍

- **R1（评审 C HIGH-1）未物化 run 读取放大**：已由 §2.1 early-exit 实现要求解决；
  未解决前的存量行为不影响正确性，只影响纯编辑 run 的 IO 开销。
- **R2 熔断误杀面（D1 已收窄 / D2 残余入册）**：跨工具误连已由 D1 消除（收窄到执行类
  工具）；缺同路径条件（真人并发/并行会话改写他文件时连击误杀）为已知残余风险——概率
  低（A 修复后冲突应趋零、step 级过滤已强、执行类工具过滤又收窄一层），由 §5 冲突来源
  分布监控兜底，出现误杀形态再启动结构化路径改造。
- **R3（评审 A F1）move recovered `overwrite=true` 窄反例**：异常恢复路径只刷源侧，
  若 move 以 overwrite 覆盖了已物化目标，manifest 会残留目标旧条目 → 下轮 flush 可能
  对该路径 CAS 冲突。概率极低（需同时命中 DB 异常 + recovered 成功 + 覆盖已物化目标），
  且 CAS 冲突文案+熔断仍兜底。留待候选 1 一并根治。
- **R4 v1→v2 迁移丢弃 v1 的 step 信息**：v1 只有 count 无 step，跨版本续计按「保留
  count、重定 step」处理；部署瞬间在途 run 的熔断语义最多保守一步，可接受。
- **R5 `S < K` fail-open**：正常执行 model_step 单调不减；此分支纯防御，宁可漏熔断
  不误杀。
- **R6（已闭环）**：v3 实施 L1 后，「漏挂即回归」对现有四工具物理上不可能（写+刷新
  仅存一份）；未来新增写路径由守卫测试在 CI 确定性拦截。L2 不采纳（事务时序 + hidden
  coupling），不留待办。L1 引入的残余风险即 D6/D7 两处行为漂移（已如实列于 §2.3）。

---

## 7. 决策点记录（v4 已全部按推荐项确认并合入对应章节）

| # | 决策 | 确认结论 | 依据 |
|---|---|---|---|
| D1 | 熔断收窄到执行类工具 | **收窄**：`name in {execute_code, execute_code_e2b, android_compile}`（§2.2） | 评审 C MED-2：convert/upload_image 的 flush 冲突共用 breaker 属误连；message 已有 `name`（tool_step_service.py:474），一行过滤 |
| D2 | 熔断加「同路径」条件 | **暂不加**：风险入册 §6 R2 + §5 冲突来源分布监控兜底 | 评审 C MED-3：真人并发误杀面概率低；content 文本解析脆弱，结构化字段改造超出本修复范围 |
| D3 | 抽共享「直写+刷新」helper | **随 L1 消解** | L1 后刷新唯一落在 outcome 四函数（物理一份）；再抽 helper 只剩样式意义 |
| D4 | waiting_user 纠偏恢复清零冲突连击 | **清零**：`fresh_workspace_conflict_budget()` 接线 `_wait:1451-1474`（§2.2） | 评审 C LOW-5：纠偏后模型改方向，与 tool_repair_episodes 对称，改动一行级 |
| D5 | refresh 读取前 early-exit | **已定为实现要求**（§2.1） | 评审 C HIGH-1：未物化 run 每次直写白付一次整文件读 |
| D6 | legacy 委托后的写语义 | **保留审批盲写**：`cas_guard=False`（§2.3） | 审批语义「批准即执行」与 CAS 拒写冲突；保盲写=零回归 |
| D7 | 接受 §2.3 行为漂移（文案措辞 / DB 异常信息降级 / edit 双读） | **接受** | 逐项还原会重新引入分支分叉，与收敛目标相悖；漂移面窄且低频（审批通道） |

---

## 8. 后续动作

- [x] 三路并行评审（报告在会话 scratchpad）
- [x] 根治路线确认（L1 写路径收敛纳入，§2.3 设计落笔）
- [x] 决策点 D1/D2/D4/D6/D7 按推荐确认 → v4
- [x] v4 增量二轮复审（A2/B2/C2）→ 发现全部合入 → v5 定稿
- [ ] TDD 实施（先红后绿：§4.2 生产模式用例 + §4.3 守卫测试先行；§2.3 L1 委托 + §2.1 挂点 + §2.2 状态机）
- [ ] arch-guard + ruff check + 全量相关测试
- [ ] 提交推送 → `deploy.sh --commit <ref> --require-idle` → §5 验证（含审批流冒烟）
