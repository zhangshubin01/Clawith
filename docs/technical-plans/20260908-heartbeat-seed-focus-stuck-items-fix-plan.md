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
