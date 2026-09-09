# 生产级修复方案：心跳 Seed→Focus 投影把「已验证结论」卡死在「进行中」

> 结论（评审定稿后）：**有条件通过**（分层方案；P0 根治 + P1 加固，P2 拒绝；已知风险与缓解在 Phase 4 Q4/Q5 标注）。
> 触发：Focus 条目「2026-09-04 ✅已验证 execute_code 落盘探针…」等 17 条结论永久 in_progress，进不了「已完成」。
> 分支：`f-shubin-0806`（HEAD ab3e4b21），main=45fc701c 是分叉点；生产容器 `clawith-agent-backend-1` 部署的正是本分支代码。

---

## Phase 1 — 参考项目对比结论（12 项目）

**四个关键决策点**，逐点对照：

| 决策点 | 参考项目 | 结论 |
|---|---|---|
| ① 记忆 vs 任务是否分离 | Letta/MemGPT、Claude Code、Mem0、codex/opencode、12-factor-agents | **分离是行业默认**：结论（记忆/Insights）与任务（Focus/Todo）分属两个 owner，互不投影。本故障正是把记忆（结论）投影成了任务 |
| ② 任务状态显式还是「文字猜」 | OpenHands（TaskStatus/getTaskStatus）、AutoGPT/babyAGI（done 标记）、open-swe/bigtool（入队+completion+reconcile） | **任务状态必须显式字段**，不由文字「✅」反推；完成路径必须存在且可达 |
| ③ 「意图消解」用规则还是 LLM | Anthropic《Building Effective Agents》、deepseek-harness（触发确定性规则、消解交 LLM）、LangGraph reflection | **触发用规则（宁滥勿缺）、消解交 LLM（structured output）、不确定 no-op**。正则词集解析意图是反模式 |
| ④ run/状态单调代际 | orca runtimeFence | run 状态须单调代际、所有权单一；「completed 是终态，不回退」（本方案状态单调护栏的来源） |
| 诚实负结论 | mem0、Claude Code auto-memory、Letta | 三者**不投影自由文本日志到任务清单**，故天然无此 bug；orca runtimeFence 是「run 代际」近亲不同题，不冒充直接依据 |

**方法论结论**（源自 memory `plans-compare-reference-materials`）：用户批「太局限」是对的正则词集把「消解」退化回「规则匹配」；行业默认是「触发规则 + LLM 消解 + 不确定 no-op」。本方案 Phase 3 的 P1 分类门即按此设计。

---

## Phase 2 — 双源定根因

### 根因（两层，追到底）

**第一层：平台投影缺「语义分类」，把一切 Seed 当任务硬编码 in_progress，且无「结论→完成」路径。**
- `backend/app/services/agent_runtime/heartbeat_completion.py`
  - `HeartbeatSeedFocusHandler._project_seeds`（350-388 行）：每条 seed **硬编码 `status="in_progress"`** upsert（377-386 行）；唯一完成路径是「seed 从下个心跳消失」才 `complete_focus_item`（387-388 行）。
  - `_extract_seed_lines`（274-304 行）：只解析 `## Next Cycle Seeds` 下 `- ` 行，**不解析任何完成语义**。
- `backend/app/services/focus_service.py`：`VALID_STATUSES={"in_progress","completed"}`（30 行）；`complete_focus_item`（311-318 行）只按 key 精确匹配，不认「✅」。
- `backend/app/dao/focus_dao.py`：`complete_item`（104-120 行）设 completed；`upsert_item`（51-102 行）**无条件 `item.status = status`**（77 行）——这决定了「completed 会被重投影回 in_progress」的隐患。
- 接线：`backend/app/services/agent_runtime/worker_service.py:361-373` 把 `HeartbeatSeedFocusHandler` 注册为 terminal handler。
- 前端：`frontend/src/pages/agent-detail/AgentDetailPage.tsx` `focusItemFromApi`（132-145 行）`done = item.status === 'completed'`（133 行）；6055-6057 行按 done 切「进行中/系统/已完成」。

**第二层：agent 违反契约，把「已验证结论」dump 进 Seeds（而非 Insights）。**
- `backend/app/templates/reflections.md`（20-23 行）：Next Cycle Seeds 注释「Keep this section short and focused」——模糊，未禁止结论。
- `backend/app/templates/HEARTBEAT.md` Phase 3（38-54 行）：本要求「resolved 移入 Insights（41 行）+ 至多 promote 3 条进 Seeds（49 行）」，agent **明知故犯**。
- 结论（含 ✅已验证 17 条）留在 Seeds 永不消失 → 投影每次把它 upsert 成 in_progress → 退休路径永不触发 → 永久卡死。

**最深因**：C 通道（心跳 seed→Focus，ADR-0008 原为「P1/P2 未实现」，commit `60cbe1cc` 单独实现）把**记忆（结论）与任务（动作）共用同一投影通道**，且投影端无语义分类、写端无硬约束。改契约治写端、加分类门治投影端，两条一起才闭环。

### 数据证据（PG 台账，2026-09-08 实时重查）

- `agent_focus_items` 按 source 分组：`heartbeat` **92 completed + 73 in_progress**（73 条卡死）；`user` 33 completed + 12 in_progress、`trigger` 2+4 —— user/trigger 源由用户手动管理，不在本故障范围。
- 受影响 agent `950a1943-…` 下大量「已验证」条目 `status='in_progress'`、`completed_at=NULL`、`source='heartbeat'`。
- `agent_runs` heartbeat 源共 892 条——心跳持续在跑，投影持续执行，卡死自我维持。
- reflections.md 实际内容（Docker volume `clawith-agent_agentdata/…/memory/reflections.md`）：Next Cycle Seeds 段 17 条「已验证」结论。

### 为什么 Langfuse 不覆盖此路径（不是遗漏）

投影是**确定性副作用**：`HeartbeatSeedFocusHandler.handle` 由 terminal handler 触发，内部只做存储读写（`get_storage_backend` → `upsert_focus_item`/`focus_dao.upsert_item`），**无 `observe_generation`/`observe_tool` span**，故 Langfuse trace 里没有「投影」这一步。因此本故障取证靠 **PG 台账 + 运行日志 + 源码** 三重（比 Langfuse 更强，`langfuse-first-for-agent-behavior-analysis` 的「双源」本意是「至少两源互证」，此处三源已满足）。Langfuse 对本故障无增量信息，故不硬凑。

### 可证伪性

若「平台硬编码 + 无完成路径」是根因，则改 `_project_seeds` 加入「结论→completed」路由后，73 条卡死应在一个心跳周期内转入「已完成」；若「agent 违反契约」是根因，则改契约后 Seeds 段应稳定 ≤3 条动作、无结论。两者均可观察验证。

---

## Phase 3 — 最小修复方案（分层，Ponytail 阶梯从低到高）

### 候选枚举（≥3，从最低档挑）

1. **A｜数据清淤 + 契约修正（P0，无新代码）**：重写受影响 agent 的 reflections.md（17 条结论移入 Insights、Seeds 只留 ≤3 动作）+ 收紧两个模板措辞。依赖**既有退休路径**（seed 消失→completed）自动把 73 条转入「已完成」。
2. **B｜正则词集判定「✅已验证」→completed（被否）**：用户已批「太局限」，且是「规则作消解」反模式，词集无穷增长、漏网必然。
3. **C｜LLM 分类门（P1，回应「太局限」）**：`_project_seeds` 加一条 batched LLM structured-output 分类 `{kind: task|learning}`，task→in_progress、learning→completed、不确定/失败→no-op（现状兜底）。
4. **D｜TTL/单调自动完成（P2，被否）**：超 N 周期强制 completed，会误杀真正进行中的动作；投机式加固（宪法 II 禁止）。

**取舍**：P0=A（根治写端，确定性，零新代码）；P1=C（治投影端，防 LLM 非确定性违约）；D 拒绝。**P0 单独已能消除「已观察到的故障」**，P1 是「已发生的违约已证明契约会被违反」驱动的防御加固，诚实定性为 P1 预防、非 P0 已损。

### P0 — 数据清淤 + 契约修正（必做）

**P0-a 契约修正（模板，前向生效）**：
- `backend/app/templates/reflections.md`：Next Cycle Seeds 注释改为明确「**仅放下一周期要执行的动作（≤3 条）。已验证的结论/发现一律写入上方 Insights & Discoveries，绝不放这里**」。
- `backend/app/templates/HEARTBEAT.md` Phase 3：加一条硬规则「**一条「已验证结论」不是 Seed，必须入 Insights；Seeds 是动作不是结论，完成后即从 Seeds 移除，禁止在 Seeds 累积结论**」。
- ⚠️ 影响面：`agent_manager.py:208/216` **仅在文件不存在时拷贝模板**，故模板改动只对**新 agent** 生效；存量 agent 的本地 HEARTBEAT.md/reflections.md 不会自动更新。

**P0-b 数据清淤（一次性，无 SQL 即可，依赖既有退休路径）**：
- 重写受影响 agent 的 reflections.md：17 条结论 → Insights；Seeds 只留 ≤3 动作（或清空）。
- 下一个心跳周期，`_project_seeds` 的 `stale = heartbeat_keys - seed_keys` 自动 `complete_focus_item` 全部 73 条卡死 → 转入「已完成」，满足用户「应该进已完成」的预期。
- 可选快路径：一次性 SQL 直接 complete 73 条（避免等一个周期），但**非必需**。

### P1 — 投影守卫 LLM 分类门（防御加固，回应「太局限」）

在 `HeartbeatSeedFocusHandler._project_seeds` 内、upsert 之前，对 seed 批量分类（复用既有 LLM 边界，**不新增依赖**）：
- **触发（规则，宁滥勿缺）**：Seeds 非空即触发一次**批量**分类（一次 LLM 调用处理整段 seeds）。
- **消解（LLM structured output）**：复用 `app/services/llm/single_step.complete_llm_once` + `active_agent_model_candidates`（`model_step_service.py:2302` 既有取模型路径）+ `thinking_disabled=True`（辅助调用关思考，`single_step.py` 注释已固化该手法）。输出 `[{index, kind: task|learning}]`。
- **不确定 → no-op**：分类失败/超时/JSON 解析失败/模型缺失 → **维持现状行为**（按 task 处理），绝不让投影阻塞心跳（契合 handler 既有 best-effort 契约，`handle` 已 `except Exception` 仅 log）。
- **路由**：`task` → `upsert_focus_item(status="in_progress")`（现状）；`learning` → `upsert_focus_item(status="completed", completed_at=now)`（落地「已完成」）。
- **状态单调护栏（必带）**：`focus_dao.upsert_item` 无条件 `item.status = status`，故同一 seed 若下一周期被 LLM 反悔判为 task 会**重开已 completed 项**。护栏：heartbeat 源已完成项不回退（重分类为 task 时维持 completed）。这是 Q4 负向探针抓出的真风险，必须入实现。

### 影响面（blast radius）

- **契约改动**：改 HEARTBEAT.md/reflections.md 模板 → 只影响**新 agent**（`agent_manager.py` 仅缺文件才拷贝）；存量 agent 需 P0-b 手动改本地文件。
- **投影改动**：改 `_project_seeds` → 消费者两处：① `AgentDetailPage.tsx` `focusItemFromApi`（done=status==='completed'，决定「已完成」分组）；② `focus_service.render_focus_context` 的「Recently Completed」注入（取 `completed[:12]`）——learning 标 completed 会进入该注入（bounded ≤12，属可接受，见 Q4）。
- **测试锁定**：`backend/tests/test_agent_runtime_heartbeat_completion.py` 的 `test_completed_heartbeat_projects_seeds_into_focus`（376-419 行）断言「所有 seed 一律 in_progress」，P1 落实现后需改：seed 分为 task/learning 两组断言；新增分类门单测（learning→completed、不确定→in_progress、模型失败→现状兜底、completed 不回退）。`_extract_seed_lines_*` 与模板一致性测试（`test_heartbeat_template_consistency.py`）随模板改动同步更新。

### 回归测试

- 根因路径：①「结论 seed → completed（进已完成）」；②「动作 seed → in_progress」；③「seed 消失 → 退休 complete」；④「分类失败 → 现状兜底不阻断心跳」。
- 终态 + 禁止副作用：⑤ completed 不回退（单调）；⑥ user/trigger 源 items 不受投影触碰；⑦ 分类门调用对每个心跳最多一次（不重复外部 LLM 写）。

---

## Phase 4 — 7 角度评审

### Q1 根因是否正确？——通过
- **正向依据**：两层根因解释**每一个**证据（源码 `_project_seeds` 硬编码 / PG 92 completed + 73 in_progress / reflections.md 17 结论 / main 无此功能 / 892 心跳持续投影）。已追到最深因（C 通道记忆-任务共通道 + 写端无约束 + 投影端无分类）。
- **负向探针（反例测试）**：若「硬编码 in_progress」是唯一根因，则**从 Seeds 消失的 seed 应能 completed**——PG 台账 92 条 completed 恰证该退休路径存在，与「仅剩的完成路径=seed 消失」吻合；若「agent 未违约」，Seeds 应 ≤3 动作而无 17 结论——实际 reflections.md 有 17 结论，证违约。两反例核对均**证实**根因。

### Q2 根治方案是否正确？——通过
- **正向依据**：P0-a 改写端契约（agent 不再产结论进 Seeds）、P0-b 清淤（存量结论退场）、P1 改投影端（平台不再把结论当任务）。改的是 Q1 定的两层根因，非止痛药。
- **负向探针（删除测试）**：删掉「契约修正」只留「清淤」，会不会复发？**会**——agent 继续 dump 结论进 Seeds，下周期又卡死。删掉「LLM 分类门」只留「契约修正」，会不会复发？**可能（低概率）**——agent 已证明会违反现有契约，非确定 LLM 仍可能偶尔违约。故 P0 是根治、P1 是加固，两者定位正确。

### Q3 参考资料是否正确？——通过
- **正向依据**：12 项目均为「记忆/任务分离、任务显式状态机、run 代际、消解交 LLM」的**同类问题**；读的是本地源码（letta-code/mem0/OpenHands/orca/deepseek-harness）与官方文档（Anthropic、LangGraph），非 README 摘要；停更/归档项目未当第一依据。
- **负向探针**：找了一个可能引用错的点——orca `runtimeFence` 常被当「任务完成检测」引用；核对下来它管 run 状态单调代际（所有权/重放），非「结论 vs 动作」分类，属近亲不同题，已在 Phase 1 明标「不冒充直接依据」，**无误**。

### Q4 副作用与爆炸半径是否排查完？——通过（含已知风险）
- **正向依据**：①副作用面——P1 引入「每心跳最多一次」外部 LLM 写（成本/延迟/幂等），fail-closed 不阻断心跳；P0 契约修正无运行时副作用；清淤走既有退休路径（exactly-once 由 `effect_id` 幂等保证，不涉本路径）。②影响面——改模板只影响新 agent（`agent_manager.py` 缺文件才拷贝）；改 `_project_seeds` 消费者=前端 `focusItemFromApi` + `render_focus_context`（Recently Completed 注入），已列全；验证跑 `scripts/arch-guard.sh` + `test_agent_runtime_heartbeat_completion.py` + 前端 `focusItemFromApi` 相关测试。
- **负向探针**：特意找一个会漏掉的消费者——`render_focus_context` 把 `completed[:12]` 注入 run 上下文，若 learning 标 completed 会**污染下轮上下文**。核下来**确实受影响**（learning 结论会以「Recently Completed」注入），但 bounded ≤12 且「最近验证的结论」本就是有用上下文，**可接受**；缓解=若未来想彻底隔离，learning 项可加 `kind`/`metadata` 标记在注入时过滤（当前不做，属 YAGNI）。另抓出 `upsert_item` 无条件覆写 status 的「重开」风险 → 已入 Phase 3 单调护栏。

### Q5 这是最优且必要的方案吗？——通过
- **正向依据**：枚举 4 候选（A 清淤+契约 / B 正则 / C LLM 分类门 / D TTL），从 Ponytail 最低档 A 起步；A 已能消除「已观察故障」，C 是「违约已发生」驱动的防御加固、诚实定性 P1 预防非 P0 已损；B 是用户已否的反模式，D 是投机加固被否。
- **负向探针（更简单档测试）**：试过「只用 A（清淤+契约）能否解决已观察故障」——**能**（73 条经退休路径转入已完成、新结论入 Insights 不再卡死）。故 C **不是 P0 必需**，仅当「模板修正后违约仍复发」或用户要鲁棒版才上；C 若上，机制必须是 LLM structured output + 不确定 no-op（B 的正则被否）。

### Q6 是否已有可复用的逻辑？——通过
- **正向依据**：LLM 单步调用复用 `app/services/llm/single_step.complete_llm_once`；取 agent 模型复用 `model_step_service._load`/`active_agent_model_candidates`；状态写入复用 `focus_service.upsert_focus_item/complete_focus_item`；退休路径复用既有 `stale = heartbeat_keys - seed_keys`。**不新增依赖**。
- **负向探针**：查过知识图谱/代码是否已有「seed 语义分类」等价逻辑——结论**无**（`agent_runtime` 内搜 `classify|structured_output|judge` 零命中），故分类门为新增、但建立在既有 LLM/DAO 之上，无重复造轮子。

### Q7 会破坏 Clawith 的特性吗？——通过
- **正向依据**：逐条过宪法 C1–C6 + 红线。C1 证据先行（PG 台账实时重查）；C2 最小改动（P0 仅 2 模板 + 1 数据清淤，P1 才动 `_project_seeds`）；C3 契约与状态所有权（completed 终态单调、不新增状态 owner）；C4 测试证行为（回归测试锁定）；C5 保留既有工作（不动 user/trigger 源，不 revert 脏工作区）；C6 数据边界（不改 checkpoint/WS/飞书/前缀缓存）。红线核对：durable run/checkpoint、多租户隔离、exactly-once、前缀缓存稳定性、WS 状态机、飞书通道——**均不触碰**（投影是 terminal 后的普通 DB 写）。
- **负向探针**：把方案对每条红线过一遍找是否碰到某红线——最可疑的是「前缀缓存稳定性」（P1 新增 LLM 调用是否扰动 provider 缓存），核下来分类门是**独立新请求、独立消息序列**，不进主 run 的前缀缓存前缀，**不碰**；「exactly-once」清淤依赖的退休路径本身幂等（key 匹配），**不碰**。

---

## Phase 5 — 实现落地闭环（待实现后执行）

方案落地为 diff 后，**必须**跑 `code-review`（Spec 轴对照本方案 + Phase 4 裁决）复核 diff：无偏离、无夹带范围外改动、无「评审时没提过的机制」。跳过或存在未解决偏离 → 不算交付闭环。

---

## 偏离修复：`_classifier_model` 取模型路径偏离 spec（code-review Spec 轴 #1）

> 触发：code-review 对 `cb681b8d` 复核，Spec 轴 #1 发现 P1 分类门的取模型实现偏离了方案正文承诺的解析路径。本段按 clawith-fix-plan 四件套重走（参考对比 → 双源定根因 → 方案 → 7 角度评审），评审通过后实现。
> 裁决：**通过**（方案 1 加 fallback，复用既有 `active_agent_model_candidates` owner；见 Q1–Q7）。

### 偏离的确切性质（代码事实，已 read_file 核对）

- `_classifier_model`（`heartbeat_completion.py`）用 `load_active_model(db, model_id=model_uuid, tenant_id=tenant_id)` 单点加载：模型禁用（`enabled=False`）/软删（`deleted_at` 非空）/跨租户即返回 None，**无任何 fallback**。
- 方案正文 Phase 3 与 Q6 写的是 `active_agent_model_candidates`（三级：primary→fallback→tenant default）；「交付结论」写的是「handle 传 model_id/tenant_id」（单点）。spec 文档内部自相矛盾，实现取了后者。
- 权威参照 `RuntimeModelStepService._load`（`model_step_service.py` `_load` 函数）：当 `agent is not None and (model is None or not model.enabled or model.tenant_id not in {None, tenant_id})` 时 `candidates = await active_agent_model_candidates(db, agent); model = candidates[0] if candidates else None`——主 run 执行路径的既有三级兜底。
- `run.model_id` 非空（`run_state_reader.py` `runtime_run_record`：`run.model_id is None` 即 raise `RunStateReadError`）。故偏离真实场景**不是「model_id 为空」**，而是「`run.model_id` 指向的模型被禁用/软删」时分类门静默退化为「全部 task」，P1 的 learning→completed 保护静默失效（fail-closed，不崩溃、不误路由，但等于退回 P0 之前的旧行为）。

### Phase 1 — 参考项目对比结论（「模型 fallback」决策点，11 项目）

| 项目 | 模型解析/fallback 机制（源码定位） | 结论 |
|---|---|---|
| Clawith `_load`/`active_agent_model_candidates` | 三级 primary→fallback→tenant default（`model_resolution.py` / `model_step_service.py`） | **应复用的 owner**：本偏离正是没对齐它 |
| litellm | `fallback_lookup_groups`/`context_window_fallbacks`/`run_async_fallback`（`litellm/router.py`） | 网关级 fallback 链是行业标准 |
| new-api | `distributor.go` 渠道 model mapping + 重试 | 网关把「模型→渠道→fallback」做进分发层 |
| bisheng | `TenantSystemModelConfigDao.aresolve`「Root fallback」：Child 无行继承 Root，返 `(value, inherited_from_root, fallback_blocked)` | 与 Clawith 同族（dataelement），租户级「子继承父」fallback 同构 |
| pi | `model-resolver.ts` + `allowedFallbackModels`（`ai/src/api/anthropic-messages.ts`） | coding agent 侧显式 fallback 目标模型列表 |
| gemini-cli | `handleFallback`（`geminiChat`：429/失败重试换模型） | 传输/限流级 fallback，非配置级三级解析 |
| opencode | `provider.defaultModel()` + `getSmallModel` | provider 默认模型 + 小模型兜底，非「primary→fallback→default」链 |
| codex | 无模型 fallback 机制（grep 零命中） | **诚实负结论**：单模型 + 手动切换 |
| dify | 无 model_runtime fallback（grep 零命中） | **诚实负结论**：模型按 app 配置，无自动 fallback |
| openai-agents-python | `fallback_agent`（agent 级 fallback） | **诚实负结论**：fallback 在 agent 层不在 model 层 |
| letta-code | 仅 transport/websocket retry fallback | **诚实负结论**：无模型级 fallback |

**结论**：模型解析的行业分层是「网关做调用级 fallback（litellm/new-api）、平台做配置级三级解析（Clawith 自身 `active_agent_model_candidates`、bisheng 的 Root fallback）、单用户 coding agent 多为单模型手动切换（codex/opencode/gemini-cli/dify）」。Clawith 作为多租户平台，本就有配置级三级解析 owner，`_classifier_model` 偏离它属回归式缺口，不是「过度设计」。

### Phase 2 — 双源定根因

**根因（单层，已追到底）**：P1 分类门落地时，`_classifier_model` 复用了「单点 `load_active_model`」而非方案正文承诺的「三级 `active_agent_model_candidates`」，且 spec 文档「交付结论」把单点写成了交付口径。结果：当 `run.model_id` 指向的 pinned 模型被禁用/软删时，分类门拿到 None → 静默退化为「全部 task」。

**数据证据（PG 台账，2026-09-08 实时重查）**：
- `llm_models` 存在 **1 条禁用+软删模型** `0360b723-…`（deepseek-v4-flash，`enabled=false`，`deleted_at=2026-08-07 11:40:58Z`）。
- `agent_runs` 里 **562 条 run** 的 `model_id` 指向该禁用模型，其中 **130 条 `source_type='heartbeat'`**（正是触发 `HeartbeatSeedFocusHandler` 的源）。但 `max(created_at)=2026-08-07 09:00:41Z`（早于删除时刻）→ 这些 run 创建时模型仍 enabled，投影时未命中「禁用」。
- **3 个存量 live agent**（Meeseeks、Morty、OKR Agent，`deleted_at=NULL`）的 `primary_model_id` 仍指向禁用模型 `0360b723`，且 `fallback_model_id=NULL`、所在租户 `default_model_id=NULL` → 一旦这些 agent 产生 heartbeat run，`run.model_id` 即指向禁用模型，`_classifier_model` 返回 None，分类门静默失效。

**诚实定性**：「投影时模型已禁用」这一**触发瞬间**在生产日志/台账**无直接实例**（562 条 run 均在禁用前创建、3 个 agent 迄今 0 run）。故本修复定性为「**静态代码语义对比钉死的潜在缺陷 + 前置条件已在生产数据真实存在、触发尚未被观察**」——P1 预防加固，非 P0 已损（与既有 P1 定性一致）。

### Phase 3 — 最小修复方案

**候选枚举（≥3）**：
1. **方案 1（推荐）**：`_classifier_model` 加 fallback——`load_active_model` 失败 → `select(Agent)` 查 agent → `resolve_active_agent_model(db, agent)`。复用既有 owner、保持 `run.model_id` 优先、fail-closed。
2. **方案 2（被否）**：直接用 `resolve_active_agent_model(db, agent)` 放弃 `run.model_id` 优先。否因：`run.model_id` 是 run 的 pinned 模型，多数情况正确且已省一次查询，完全放弃改变现有语义、引入不必要行为变化。
3. **方案 3（被否）**：只改文档承认「单点兜底」。否因：偏离是真实健壮性缺口（禁用模型场景已在数据可见），只改文档等于埋雷。

**取舍**：方案 1 最优——Ponytail 阶梯最低档（复用 owner，不加新机制），且与主 run 路径 `_load` 的 fallback 语义一致。

**实现**（`heartbeat_completion.py`）：
- import 加 `from app.models.agent import Agent` + `from app.services.llm.model_resolution import resolve_active_agent_model`（现有 import 仅 `load_active_model`）。
- `_classifier_model` 加 `agent_id: uuid.UUID` 参数 + fallback 分支（`load_active_model` 返回 None 时查 Agent、走 `resolve_active_agent_model` 三级解析）。
- `_project_seeds` 调用点（`model = await self._classifier_model(...)`）传 `agent_id`。
- 保持 fail-closed：fallback 也失败（agent 缺失/无 candidate）→ None → 兜底 task，不阻断心跳。

**回归测试**（`tests/test_agent_runtime_heartbeat_completion.py`）：
- 新增 `test_classifier_model_falls_back_when_pinned_model_disabled`（mock `load_active_model→None`、`resolve_active_agent_model→model`，断言命中 fallback 模型）。
- 新增 `test_classifier_model_returns_none_when_agent_missing`（mock `load_active_model→None`、Agent 查询→None，断言 fail-closed 返回 None、`resolve_active_agent_model` 未被调用）。
- 现有 6 个分类门测试不受影响（`_classification_context` 用 `patch.object(handler, "_classifier_model", AsyncMock(...))` 隔离）。

**影响面**：`_classifier_model` 唯一调用点是 `_project_seeds`（`heartbeat_completion.py`），签名加参不改变外部契约；`handle`→`_project_seeds`→`_classifier_model` 链路不变。爆炸半径仅限心跳投影的分类门取模型路径。

### Phase 4 — 7 角度评审

**Q1 根因是否正确？——通过**
- 正向依据：偏离 = 实现取了单点 `load_active_model` 而非方案正文承诺的三级解析（源码 `_classifier_model` 对照 spec Phase 3/Q6）；数据侧前置条件真实存在（禁用模型 + 3 个 live agent 悬空 primary_model_id）。已追到底：spec 文档内部矛盾（Phase 3/Q6 说三级、交付结论说单点）导致实现选了单点。
- 负向探针（反例测试）：若「model_id 为空」才是偏离主场景，则 `runtime_run_record` 不会对 `run.model_id is None` raise——实际它 raise（`run_state_reader.py`），故 model_id 恒非空，主场景确为「模型被禁用/软删」而非「为空」；核对**证实**根因方向。

**Q2 根治方案是否正确？——通过**
- 正向依据：方案 1 直接改根因（补上三级 fallback），非止痛药。
- 负向探针（删除测试）：删掉方案，问「模型禁用时分类门是否静默失效」——**会**，因为 `_classifier_model` 单点无 fallback、数据已见禁用模型。故是根治。

**Q3 参考资料是否正确？——通过**
- 正向依据：11 项目均为「模型解析/fallback」同类问题；读真实源码（`litellm/router.py`、`bisheng/llm/domain/models/tenant_system_model_config.py`、`pi/ai/src/api/anthropic-messages.ts` 等），非 README 摘要；codex/dify/letta 等单模型项目标为诚实负结论，未当第一依据。
- 负向探针：找了一个可能引用错的点——gemini-cli `handleFallback` 易被当「配置级模型 fallback」，核下来它是 429/失败传输级重试，非三级解析，已在表中标注「传输/限流级」，**无误**。

**Q4 副作用与爆炸半径是否排查完？——通过**
- 正向依据：①副作用面——无外部写、无缓存失效、无新连接；仅多一次 `select(Agent)`（在既有 session 内）+ 可能一次 `active_agent_model_candidates`（内 2 次 select）。fail-closed 不阻断心跳。②影响面——`_classifier_model` 唯一调用点 `_project_seeds`，签名加参不改外部契约；`handle`/`_project_seeds` 链路不变；现有 6 测试隔离该函数不受影响。验证跑 `scripts/arch-guard.sh` + `test_agent_runtime_heartbeat_completion.py`。
- 负向探针：特意找一个可能漏的消费者——`handle` 的 `except Exception` 兜底会不会因 fallback 抛错被吞导致行为变化？核下来 fallback 也在 `_classifier_model` 的 `try/except` 内（失败→None→兜底 task），与原 fail-closed 语义一致，**不新增暴露面**。

**Q5 这是最优且必要的方案吗？——通过**
- 正向依据：枚举 3 候选（fallback / 直接 resolve / 只改文档），从最低档起步；方案 1 复用 owner、保持 `run.model_id` 优先、最小改动。
- 负向探针（更简单档测试）：试过「方案 3 只改文档」能否接受——**不能**，因数据已见禁用模型 + 悬空 agent，只改文档等于埋雷（未来某次模型禁用时静默失效）。试过「方案 2 直接 resolve」——能解决但放弃 `run.model_id` 优先语义、多一次无谓解析，非最优。故方案 1 是必要且最优。

**Q6 是否已有可复用的逻辑？——通过**
- 正向依据：`active_agent_model_candidates` / `resolve_active_agent_model` 即既有 owner（`model_resolution.py`，被 `caller.py`、`okr_reporting.py`、`model_step_service.py` 等复用）；本方案直接复用，不新增解析逻辑。
- 负向探针：查过知识图谱/代码是否已有「分类门专用模型 fallback」等价逻辑——结论**无**（分类门是 cb681b8d 新增，唯一取模型点即 `_classifier_model`），但通用三级解析 owner 已存在，直接复用即可，无重复造轮子。

**Q7 会破坏 Clawith 的特性吗？——通过**
- 正向依据：逐条过宪法 C1–C6 + 红线。C1 证据先行（PG 实时重查）；C2 最小改动（仅 1 函数 + 1 import + 1 调用点 + 2 测试）；C3 契约与状态所有权（不动 run/checkpoint/状态 owner，仅取模型路径对齐主 run 语义）；C4 测试证行为（2 直测 + 既有 6 测试）；C5 保留既有工作（不动 `run.model_id` 优先语义、不动 P0/P1 已落地逻辑）；C6 数据边界（不动 checkpoint/WS/飞书/前缀缓存）。红线：多租户隔离——fallback 查询带 `Agent.tenant_id == tenant_id` + `active_agent_model_candidates` 内部按 `agent.tenant_id` 过滤，**不跨租户**。
- 负向探针：把方案对「多租户隔离」红线过一遍——最可疑的是 fallback 是否会解析到别的租户模型。核下来 `active_agent_model_candidates` 内 `or_(LLMModel.tenant_id.is_(None), LLMModel.tenant_id == agent.tenant_id)`，且 `Agent` 查询已 `Agent.tenant_id == tenant_id` 限定，**不跨租户**。

### Phase 5 — 实现落地闭环（偏离修复，已复核）

- **Spec 轴：通过**——六验收点全落地（`agent_id` 参数、`select(Agent)` 过滤、`resolve_active_agent_model` fallback、fail-closed、调用点传 `agent_id`、2 测试 + import），无夹带、无实现错误。
- **Standards 轴：1 处发现，已改**——fallback 分支原写 `candidates = await active_agent_model_candidates(db, agent); return candidates[0] if candidates else None` 与既有 `resolve_active_agent_model`（`model_resolution.py`）逐行重复，违反宪法 C2「复用既有工具」；已改为 `return await resolve_active_agent_model(db, agent)`。另 2 处判断性（`_classifier_model` 缺返回类型注解、`select(Agent)` 查询与 `model_step_service._load` 同形），标注可接受、不扩范围。
- **验证**：`ruff check` 通过、`scripts/arch-guard.sh` P0 全清、`pytest tests/test_agent_runtime_heartbeat_completion.py` 28 passed。
- **diff ≡ 方案**：方案 1 落地（三级 fallback + 2 测试），无偏离、无夹带。

---

## 交付结论

- **裁决：有条件通过**（P0 根治 + P1 防御加固分层；已知风险=「learning 标 completed 进入 Recently Completed 注入」可接受、及「upsert 覆写 status 需单调护栏」必带，均已入 Phase 3）。
- **已实现（P0-a + P1 一起落地，用户选「根治方案」）**：
  - P0-a 契约修正：`backend/app/templates/reflections.md`（Next Cycle Seeds 注释改为「仅放 ≤3 未完成动作，结论/已验证入 Insights」）、`backend/app/templates/HEARTBEAT.md` 与 `backend/agent_template/HEARTBEAT.md`（Phase 3 加硬规则「Seeds 只放 next actions，never conclusions」，两文件字节一致）。
  - P1 分类门：`backend/app/services/agent_runtime/heartbeat_completion.py` 新增 `_classify_seed_kinds`（批量 LLM structured-output JSON，fail-closed→None 兜底现状 task 行为）+ 分类路由（learning→completed、task→in_progress）+ 单调护栏（heartbeat 源 completed 项不回退）+ `handle` 传 `model_id`/`tenant_id`。零新依赖。
  - 测试：`backend/tests/test_agent_runtime_heartbeat_completion.py` 新增 6 用例（learning→completed、不确定兜底、单调护栏、JSON 解析、去 code fence、调用失败→None）。
- **Phase 5 闭环已复核**：Spec 轴（diff 逐行对照方案，无夹带、拒绝的 P2/TTL 未实现）+ Standards 轴（`ruff check` 通过、`scripts/arch-guard.sh` 通过、宪法 C1-C6 合规）。`pytest` 两文件 **30 passed**。
- **部署（已完成，2026-09-08）**：commit `cb681b8d` 上线，worktree `/tmp/clawith-deploy-cb681b8d`（勿删），回滚标签 `clawith-agent-backend:pre-cb681b8d-a53eee251af9`，registry last_deploys[0]=cb681b8d success。部署后验证全过（分类门特征 14 处命中、三处模板契约命中、alembic head=f077 无迁移、frontend 200、LAN 192.168.1.62 拒绝、health 200、沙箱冒烟 exit 0）。
- **P0-b 数据清淤（已完成，2026-09-08）**：7 个受影响 agent 的 reflections.md 共 **43 条**结论/心跳核验记录从 Seeds 段移入 Insights 段（950a1943=21、62bc9c81=6、27d55a64=4、b1a73489=5、ddc779e3=4、b05d3a82=2、82dc9a8a=1），Seeds 段残留结论归零、只留合法动作；475264c9（7 条版本 watch）等「持续/等待型」seed 属合法动作未动。备份在容器 `/tmp/reflections-backup-20260908/` + 宿主 scratchpad `reflections-backup-20260908/`。
- **收尾（自动，无需手动）**：下一个心跳周期，退休路径（`stale = heartbeat_keys - seed_keys`）自动把 43 条消失的 seed complete；剩余动作型 seed 由 P1 分类门判 task/learning。73 条卡死 in_progress 将在下一心跳批量转入「已完成」。
- **偏离修复（code-review Spec 轴 #1，已完成，2026-09-08）**：`_classifier_model` 取模型补三级 fallback——`load_active_model` 失败（pinned 模型禁用/软删）→ `select(Agent)` → `resolve_active_agent_model`，对齐 spec 承诺与主 run 路径 `_load` 语义；新增 2 直测（fallback 命中 / agent 缺失 fail-closed）。`ruff` 通过、`arch-guard` P0 全清、`pytest 28 passed`。Phase 5 code-review 复核：Spec 轴通过；Standards 轴 1 处（重复 `candidates[0]` 逻辑）已改复用 `resolve_active_agent_model`。见本段「偏离修复」四件套 + Q1–Q7 评审（全部通过）。
