# R3 补丁：后台 trigger/heartbeat run 的清单注入断链修复（2026-09-03）

> 起因：dec47111 上线后 04:30Z 首验实测——trigger run `35338e16` 两次模型调用输入均无注入块（ClickHouse `position()` 铁证，input 全长 27331/8986 chars 无「此前已确认」标记）。
> 流程：两步走——①参考资料对比（见第七节）；②本方案（修复面/回退 scope 决策 + 替代方案评估）待评审后实施。

## 一、问题（代码级根因链，已逐行核实）

1. `trigger_runtime/intake.py` `enqueue_trigger_runtime()` 构造 `StartRunCommand` 时只传 `origin_user_id`，**不传 `actor_user_id`**（默认 None）→ `AgentRunCommand.actor_user_id=None` → `command_worker` 构建的 `RuntimeContext.actor_user_id=None`。
2. `context_builder._retrieve_list_context()`：`user_id = _optional_uuid(context.actor_user_id)` → None。
3. `cross_session_retrieval.retrieve()`：`if user_id is not None:` 才走 `load_recent_sessions_open_items`（该查询要求 `session_type='direct'` + user 过滤）→ **后台 run 的跨会话分支整体跳过**。
4. `_ensure_trigger_session()`：每个 fire 以 `uuid5(execution.id)` 生成**全新 `session_type='trigger'` 会话**，模型步时无指针（指针由本 run 尾 R1 才写入该会话）。

→ 双重断链：后台 run 结构性永不注入。**R3 v2 定稿 Q5 决策「无条件（含 heartbeat）」在实施中被 actor=None 静默架空**——对照 probe 已证检索机制本身健康（同上下文补 user_id=创建者即 sections=2，命中 direct 主会话 17:17Z 指针）。注：heartbeat run 无 ChatSession 行且 actor 可为 None，修复后回退查询是其**唯一**指针来源=Q5 意图补全（评审 Q4）。

## 二、决策（含替代方案评估）

| # | 决策点 | 结论 |
|---|---|---|
| A | 修复面 | **检索层 scope 回退**，不污染命令的 actor 语义 |
| B | 回退 scope | `user_id=None`（后台 run）时，回退到**本 agent 自己的 `session_type='trigger'` 近期会话**（agent 内部工作连续性，≤5 份、updated_at 降序） |
| C | direct 路径 | **不变**：user_id 在场仍 direct-only——避免把 agent 内部独白清单暴露进用户会话。注：`a2a` 会话（第四种类型）恒带 actor（a2a 启动/完成路径显式设 actor_user_id），永不落入回退分支，无需纳入 scope |
| D | 已知边界 | direct 会话新建清单后的**第一次** trigger fire 仍 no-op（尚无先例 trigger 指针）；该 fire 尾 R1 写回指针后自愈。记录在案，出现生产影响再评估零时差增强 |

**替代方案（否决及理由）**：
- 方案一：trigger intake 补 `actor_user_id=origin_user_id`。否决——actor 语义=认证发起人，后台触发是系统动作；`actor_user_id` 消费面 25 个非测试文件（command 幂等比对、审批归属、删除审批 `requested_by`、审计/追踪归属），语义污染风险 > 收益；且 `load_recent_sessions_open_items` 只查 direct，纯后台工作流（指针只在 trigger 会话）依然断链。
- 方案三：trigger 会话按 trigger 复用（改 `_ensure_trigger_session`）。否决——会话复用波及消息归属/并发 lane/清理面，超出单点修复意图。
- 零时差增强（备选，本次不做）：回退范围并入 creator 的 direct 会话（需 `agent.creator_id` 入参）。仅当边界 D 成问题时再评估。

**先例**：`tool_step_service.py:726` 已有同型先例 `actor_user_id = context.actor_user_id or str(agent.creator_id)`——消费方回退而非污染命令，本方案与其一致。

## 三、实施清单（按当前 HEAD 真实代码）

### 1. `backend/app/services/agent_runtime/session_context_service.py`
- 新增模块级 `_recent_trigger_sessions_open_items_statement(tenant_id, agent_id, *, exclude_session_id, limit)`：`select(SessionContextState.session_id, SessionContextState.open_items)` join `ChatSession`（同 tenant+id），where `SessionContextState.tenant_id==tenant_id`、`SessionContextState.agent_id==agent_id`、`ChatSession.session_type=="trigger"`、`ChatSession.deleted_at.is_(None)`、session_id != exclude，`order_by(SessionContextState.updated_at.desc()).limit(limit)`——与既有 `_recent_sessions_open_items_statement` 并列，共享「direct sessions only」对应注释口径。
- 新增 `async def load_recent_agent_trigger_sessions_open_items(db, *, tenant_id, agent_id, exclude_session_id=None, limit=5)`，返回 `(session_id, open_items)` 元组序列；docstring 声明「agent-internal trigger sessions；background run 无 acting user 时的 R3 回退 scope，不跨 user、不跨 agent」。

### 2. `backend/app/services/agent_runtime/cross_session_retrieval.py`
- `retrieve()`：`if user_id is not None:` 分支不变；新增 `else:` 分支调 `load_recent_agent_trigger_sessions_open_items`（exclude 当前 session、limit `self._max_sessions`）。候选收集/去重/exact→wildcard/project 过滤/清单解析/`_MAX_PENDING_LIST_NOTES`/`MAX_INJECTED_ITEMS` 全部复用不动。
- 模块与 `retrieve()` docstring 同步：user_id=None 时的 scope 定义与理由（后台 run 的 agent 内部连续性）。

### 3. `backend/app/services/agent_runtime/context_builder.py`
- **零改动**（`user_id=None` 自然落入新分支）。

### 4. 测试 `backend/tests/test_cross_session_retrieval.py`（TDD：先红后绿）
- `_ContextService` fake 增 `load_recent_agent_trigger_sessions_open_items`（记录调用）。
- 新增：①user_id=None + trigger 会话指针 → 注入（run 35338e16 回归）；②user_id=None + 当前会话指针 + trigger 指针 → 当前优先、list_id 去重；③user_id 在场 → 旧路径不变且新方法不被调用（断言调用记录）；④user_id=None + project 过滤 / 无清单文件 → no-op 不变。

### 5. 文档同步（同一 commit）
- 本方案文档；
- `docs/analysis/2026-09-02-opening-loop-number-hallucination.md` 第 1 层补「后台 run 断链已修」条目；
- `docs/technical-plans/20260903-r3-open-list-injection.md` 定稿 v2「范围外」注记本补丁链接（Q5 意图补全）。

## 四、验收

- `uv run --extra dev pytest tests/test_cross_session_retrieval.py -x -q` 全绿；
- 全量 pytest（服务契约跨模块）、`ruff check .`、`pyright app`、`scripts/arch-guard.sh` 通过；
- 上线后验证：等下一次 trigger fire（周期 60min，约 04:53Z 后）→ ClickHouse `position()` 断言该 run 首个模型调用输入含「此前已确认」与 `cross_session_list`（该 fire 起应注入；此前各 fire 尾已把指针写入各自 trigger 会话，回退查询从最近 5 份命中）；容器内 probe 复跑对照（user_id=None 时 sections≥1）。

## 五、部署与回滚

- 照例 `scripts/deploy.sh --commit <new>` 全量一步上线（红线 2026-08-30「不要灰度」=全量+上线后监控）；部署前 `check-inflight-runs.sh` 等终态；
- 无迁移（alembic f072 不变）；新打回滚标签 `clawith-agent-backend:pre-<new>-<sha>`；部署完成即更新 `clawith-workspace-facts` 当前部署与监控项。

## 六、范围外（明确不做）

- 每步→run 内首步注入的成本优化（仍挂 3.1 增量化）；
- `**bold**` 标题 strip polish（展示质量，另行小票）；
- 3.2 模型升级（需用户拍板成本）；
- trigger 会话复用（方案三）。

## 七、引用严谨性声明

- **源码级**：Clawith 本仓库（本方案全部行号级核实）；letta-code 整库研究报告 `docs/technical-plans/20260903-letta-code-study.md`——deferred 索引发现（`notes/archive` 不注入、靠索引发现）是本方案回退 scope 的依据；其隔离论述原文为**跨 agent 隔离**（memory-confinement，单用户 harness），本方案「不跨用户泄漏」是 **Clawith 侧向多租户的延伸类比**，非 letta 原文主张（评审 Q3 修正）。
- **行为级**：Codex todo 对齐（R3 v2 已引用，本补丁延续其「会话级常驻对齐材料」scope 语义）；Anthropic《Building Effective Agents》——simple/composable 原则，选最小 scope 变更、不引入新抽象。
- **无对应物声明**：langgraph/langchain/deepagents 无跨 run 注入对应物；OpenHands conversation 按用户会话隔离但无 background 回退先例；评估基准（SWE-bench/Terminal-Bench/RE-Bench）无上下文注入验收类别——以上类别均不承载本方案论证，逐类说明为无。
