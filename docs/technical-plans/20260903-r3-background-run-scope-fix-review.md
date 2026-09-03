# R3 补丁评审：后台 trigger/heartbeat run 清单注入断链修复（2026-09-03）

> 评审对象：`docs/technical-plans/20260903-r3-background-run-scope-fix.md`
> 评审依据：真实代码逐行核对 + 真实任务执行日志（run 35338e16 / CH events_full / session_context_states 时序）+ 参考资料清单（letta-code 研究报告、Codex 行为级、Anthropic 方法论）。
> 结论先行：**9 问 8 过，1 处引用精度需修正（Q3），2 处计划文档补注（a2a 会话类型、heartbeat 无会话）**。方案可进入实施。

## 一、逐问结论

### Q1 根因找的是否正确？——**正确（日志+代码双实锤）**

代码链（全部本次重新核对）：
- `trigger_runtime/intake.py:308-335` `StartRunCommand(...)` 只传 `origin_user_id`，`actor_user_id` 保持默认 None → `persistence.py:361` 落库 `AgentRunCommand.actor_user_id=None` → `command_worker.py:345` `RuntimeContext.actor_user_id=None`。
- `context_builder.py:388` `user_id = _optional_uuid(context.actor_user_id)` → None。
- `cross_session_retrieval.py:221-233`：`if user_id is not None:` 才走 `load_recent_sessions_open_items`——None 时跨会话分支整体跳过。
- `trigger_runtime/intake.py:147` `session_id = uuid5(execution.id)` → 每 fire 全新 `session_type='trigger'` 会话；指针写入方实锤=`session_context_completion.py:89-107`（run 尾 completion 节点应用 resolved/new_open_items delta）。

日志实锤：run 35338e16 两次模型调用（03:53:28Z/03:53:35Z）输入全文无注入标记（CH `position()`=0，全长 27331/8986）；该 session 指针 `updated_at=03:53:41Z`——**晚于全部模型步**；同 tenant+agent 的 direct 主会话 e47a3f7e 指针自 17:17Z 就在，但被 user_id=None 挡在查询外。对照 probe（同上下文补 user_id=创建者→sections=2）证明断点只在 scope 解析。**结论：根因描述与「双重断链」表述准确，无遗漏的第三断点。**

### Q2 根治方案是否正确？——**正确，边界诚实**

回退 scope=「本 agent 的 trigger 会话」精确覆盖断点：①不依赖 actor 语义（断点二修复）；②trigger 会话指针由每 fire 尾 R1 写回，下一次 fire 的回退查询必然命中（断点一修复）；③tenant+agent 过滤保证无跨租户/跨用户泄漏（trigger 会话 user_id 恒为 creator，见 intake.py:157）。已知边界 D（direct 建清单后的第一次 fire 仍 no-op）是**时差问题非正确性问题**，且方案已显式记录+给出升级触发条件——诚实。

### Q3 参考的资料是否正确？——**1 处精度需修正**

- **letta-code**：研究报告 `docs/technical-plans/20260903-letta-code-study.md:33`「deferred 目录不注入、靠索引发现」支撑 R3 检索器设计，引用正确；但 `:75` 的隔离论述是 **跨 agent 隔离（memory-confinement）**，且 letta 是**单用户 harness**——方案文档「不跨用户泄漏」是 Clawith 侧对多租户的**延伸类比**，非 letta 原文主张。**已修正方案第七节措辞**（标注延伸性质）。
- **Codex todo 对齐**：方案已标「行为级（未源码核实）」，与 R3 v2 评审第 3 条口径一致，不承载关键论证——引用恰当。
- **Anthropic《Building Effective Agents》**：simple/composable 原则支撑「最小 scope 变更」，属方法论级引用，恰当。
- 无对应物声明（三库/OpenHands/基准）逐类说明——合规。

### Q4 会引起其他问题吗？——**三类已查，无实质风险**

- **heartbeat 波及**：heartbeat_runtime 同样存在 actor=None 路径（`actor_user_id=triggered_by_user_id` 可为 None），且 heartbeat run 无 ChatSession 行——修复后回退查询是其**唯一**指针来源。这正是 R3 v2 Q5「无条件（含 heartbeat）」的原文意图，属意图补全而非副作用。（补注已加入方案文档）
- **a2a 会话类型**：全库存在第四种 `session_type=="a2a"`（api/chat_sessions.py:91）。a2a 启动/完成路径全部显式设 actor（a2a_runtime.py:689 `actor_user_id=owner_user_id`、a2a_completion.py:415）——**永远不会落到 user_id=None 分支**，无需纳入回退 scope。（补注已加入方案文档）
- **成本/缓存**：trigger 周期 60min、每步 +400-800 tokens，可忽略；前缀缓存布局变化仅影响低频后台 run。

### Q5 会把其他逻辑搞坏吗？——**不会**

- 新查询是 `_recent_sessions_open_items_statement` 的同模块**兄弟语句**，既有语句零改动（已测试 SQL 不碰）；
- `retrieve()` 新增 else 分支只在「user_id=None」时触发——该场景此前是**严格 no-op**，行为变化域=此前什么都不发生的路径；
- 调用面封闭：`retrieve()` 全库唯一调用方=context_builder:391，`load_recent_sessions_open_items` 唯一调用方=检索器:222；
- 无迁移、无 schema 变更、无 __all__/契约变更。

### Q6 这是根治的最佳方案？——**是（在四个候选中）**

| 方案 | 评语 |
|---|---|
| ①intake 补 actor_user_id | 语义污染 25 文件消费面；只修 direct 指针场景，纯后台流仍断——**非根治** |
| ②trigger 会话复用 | 波及消息归属/并发/清理面——**代价不对称** |
| ③**检索层回退（采纳）** | 断点全修、调用面 2 文件、语义干净、与 `tool_step_service.py:726` 消费方回退先例一致——**最小根治** |
| ④回退+creator direct 会话 | 零时差但需 creator_id 入参+暴露面扩大——边界 D 成问题时再上，**现为过度** |

### Q7 修复方案是否是多余的？——**不多余，但性质要说清：预防性修复**

诚实口径：**已观察到的 trigger run（268039e4/35338e16）都在无注入情况下正确完成了**——没有已发生的故障。但 ①R3 v2 评审通过的 Q5 决策「无条件（含 heartbeat）」被实施静默架空，方案与实现不一致必须收敛（仓库宪法：非平凡变更代码/文档/契约不得矛盾）；②trigger reason 文本自带数字引用（本次 goal 含「P1 1→4 批次」与步骤编号），5ad111a9 型脑补风险在后台上下文真实存在。修复消除的是**风险类**而非**已损事故**——这不是多余的，但紧迫度定性为 P2 预防。

### Q8 是否已经有可复用的逻辑？——**同模块兄弟语句即最大复用**

- 无任何既有查询返回「agent 级 session_context_states.open_items 投影」：`chat_session_dao.list_sessions(session_type=...)` 是纯 ChatSession 列表（无 SessionContextState join），不可复用；
- 复用点=语句形状/join/排序/limit 全部照抄 `_recent_sessions_open_items_statement`（同 owner 同模块）；
- 备选「参数化既有语句（session_types+可选 user_id）」：复用更彻底但**改动已测试 SQL 的语义**，风险>收益，否决。兄弟语句是正确选择。

### Q9 会破坏 Clawith 的特性吗？——**不会**

- **多租户/多用户隔离**：tenant_id+agent_id 过滤，trigger 会话是该 agent 内部独白（user 恒=creator），零跨用户暴露；
- **卡片模式/飞书/群聊/a2a**：全部携带 actor → 不落新分支，行为零变化；
- **模型可见输入可追溯**（宪法「Model-visible inputs are traceable」）：note 的 `id=cross-session-list:{run_id}` 不变，新 scope 来源写入文档与 docstring；
- **Runtime core 通用性**：改动全部在 R3 capability 服务内，未触及执行模型；
- **回滚安全**：无迁移，镜像级回滚即完整回退。

## 二、评审结论与计划修订

**结论**：方案通过，可进入实施。修订（已回写计划文档）：
1. 第七节 letta 引用措辞：跨 agent 隔离→「类比延伸至多租户跨用户隔离」，标注非原文主张；
2. 决策表 C 行补注：a2a 会话恒带 actor、不落回退分支，无需纳入 scope；
3. 背景节补注：heartbeat run 无 ChatSession 行，回退查询是其唯一指针来源=Q5 意图补全。

**实施不变**：session_context_service 兄弟语句 + retrieve() else 分支 + 4 测试用例 + 文档同步；无迁移；部署照例全量一步+上线后 CH position() 验证。
