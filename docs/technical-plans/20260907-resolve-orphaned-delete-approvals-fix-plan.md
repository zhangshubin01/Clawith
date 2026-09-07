# 生产级修复方案：S2 —— 3 条孤儿 pending delete_files 审批的根治

日期：2026-09-07
状态：implemented + `--apply` 已执行（代码 + 测试 + 双轴 code-review 落地；2026-09-07 `--apply` 生产数据已对账——3 条孤儿全 rejected、0 pending，逐 run 实测见 §5.1）
前置：`20260905-maintainer-gate-g3-g4-production-plan.md`（§8.2 雷 1 / §9.2 S2 / §9.3 验收红线「3 条 pending 已 resolve」）；门控已落地 `aa539999`。
裁决：**评审通过**（7 角度逐条通过；Q2/Q5 附「已知风险 + 缓解」标注）。

---

## 0. 结论先行

- **故障**：Maintainer 门控（`aa539999`）用维护者名单替换了 agent-scoped `delete_files` 的 L3 审批流，但存量 3 条 pending `ApprovalRequest(action_type=delete_files)` 无人消费 → 对应 3 个 run 停在 `waiting_user`（2026-08-20 起，已 16 天），成为僵尸态。
- **根治**：一次性 out-of-band 脚本 `backend/app/scripts/resolve_orphaned_delete_approvals.py`，对这 3 条按 creator 以 `rejected` resolve，并 resume 原 run、告知「门控已接管」。不新增审批流，不改 enum，不建运行时回收器。
- **最小生产代码改动**：给 `autonomy_service.resolve_approval` 增加一个可选参数 `resume_message`（默认 `None` 保持现行为），使脚本复用其「状态迁移 + 审计 + 通知 + 幂等 resume」全套逻辑，仅替换 resume 话术。
- **防复发**：本故障是一次性迁移缺口（门控替换审批流时把数据对账外置了），非周期性运行时 bug；防复发 = 脚本自身可参数化复用 + 部署 runbook 固化「替换 L3 审批流必须对账存量 pending」这一不变式，**不建** speculative 的「陈旧审批回收器」（宪法 II）。

---

## 1. 参考资料对比（Phase 1，≥10 项目，含诚实负结论）

决策点：①孤儿 pending 审批该 reject / approve / supersede 哪个语义；②如何 resume 被 interrupt 的 run 并带回决策；③resume 的幂等/exactly-once；④数据迁移 vs 运行时对账（Alembic DDL-only）。

| 决策点 | 参考项目 | 结论 / 偏离理由 |
|---|---|---|
| 孤儿审批语义（reject vs ignore/supersede） | **agent-inbox**（T2，研究报告 `20260903-agent-inbox-hitl-ux-study.md`，读真实源码） | 其 `HumanResponse.type="ignore"` = 回传 `null` 什么都不做；`ignoreThread` = `updateState({values:null, asNode:END})` 直接把线程标 END、**不 resume**（用于「无效 schema 强制忽略 / Mark as Resolved」）。对照：Clawith 无 `updateState(asNode=END)` 原语，且这 3 个 run 的删除是任务收尾清理、graph 还有后续节点可跑——**选「resume 带拒绝 payload」而非「直接 END」**（研究报告 1 号对比项明确两种实现的取舍：「直接 END 干净但丢后续节点；resume 后分支判断可保留图逻辑」）。语义上「reject + 话术解释『门控已接管』」= agent-inbox 的 `ignore` + 备注，最贴切。 |
| resume 带回决策 | **langgraph**（T0 本地三库，读真实源码 `libs/langgraph/langgraph/types.py`） | `Command(resume=value)` + `interrupt()` 是 Clawith `resume_run` 的母本。核对：`types.py:799-823` `Command.resume` = "Value to resume execution with. To be used together with interrupt()"、`interrupt(value)` 在 `types.py:851`。Clawith `resolve_approval` 的 resume payload（`resume_type:user_input` + `decision`）即此原语的自家实现——**复用，不自造**。LangGraph 对孤儿 interrupt 无自动清理（与 Clawith 同病），故不能从母本找「孤儿回收」答案，只能从 durable-workflow 生态找。 |
| 暂停态收敛（waiting 应终结，不无限悬挂） | **conductor**（T3→十四节，Netflix Conductor 后裔 durable workflow 引擎） | workflow/task 级超时 + retry/compensation 语义，即「任何 wait 态都必须有收敛路径」的工程原则。Clawith 现状：`waiting_user` 无超时、无回收器（本方案 §2 数据证实 16 天不收敛）。**只借鉴其「wait 态必须收敛」原则，不照搬**（conductor 是 Java 独立引擎，Clawith 是 LangGraph checkpoint 暂停，机制不同）。 |
| 陈旧/重复执行对账（防复发方向） | **open-swe**（T2，研究报告 `20260903-open-swe-task-queue-study.md`） | 「reconcile 兜底清扫」= 队列化异步 agent 的兜底对账，与本票「门控替换后对账存量审批」同型。借鉴：**对账应是显式、可 dry-run、幂等的**，而非靠运行时偶然触发。 |
| 陈旧执行体自灭（防复发方向） | **LangBot**（T1，`20260905-langbot-study.md`）`placement_generation` 单调代际 + `assert_execution_active` 自灭；**orca**（T1，`20260905-orca-study.md`）`runtimeFence` 单调计数租约 + 幂等 envelope | 两者的「单调代际让陈旧工作体自我识别并退出」是**最可信的防复发范式**。本票**不引入**（3 条孤儿是一次性迁移缺口，引入代际属投机加固，宪法 II）——但记录为「若未来再出现『审批流替换/废弃』类迁移，应以『单调代际 + 显式对账脚本』为模板」。 |
| 数据迁移纪律（Alembic DDL-only） | **herdr**（T1，`20260905-herdr-study.md`）版本迁移 + 未来版本拒绝；Clawith `backend/alembic/AGENTS.md` §2 | herdr「结构迁移进版本、数据对账外置脚本」与 Clawith f077 迁移 docstring「autonomy_policy 键清理 + 孤儿 pending delete 审批 resolve 是 out-of-band 数据操作，不进迁移」**完全同构**。本票正是该纪律的产物：**迁移只建表（f077 纯 DDL），数据对账走脚本**。 |
| HITL 审批流的「谁能批」权限边界 | **bisheng**（T1 上游同 org，`20260905-bisheng-study.md`）`interrupt_before` + `continue_run` 人机协同 | 诚实负结论：bisheng 的 LangGraph 是「拓扑调度器 + 自研 GraphState sidecar」，**sidecar 状态不跨进程持久**（进程死即丢）→ 其 interrupt 恢复在进程重启后不可靠，**不能学**（Clawith 原生 checkpoint + AsyncPostgresSaver 更强）。它的 `continue_run` 亦无孤儿审批对账。 |
| HITL 同步阻塞 vs 异步持久 | **codex / gemini-cli / cline**（T1/T2，本地源码） | 诚实负结论：三者权限/审批是**同步阻塞式**（弹窗确认，不落库、不跨进程）→ 根本不存在「孤儿 pending 审批」这个故障类。**无对应机制可抄**，反证 Clawith 的「异步持久审批 + 事后门控替换」才是本故障的形态来源。 |
| 多租户/审计 | **dify**（T1）租户隔离 + **bisheng** 审计日志 | 诚实负结论：二者均无「审批流废弃后的存量 pending 对账」先例可查。Clawith 的 `resolved_by` + `audit_logs` 已满足对账审计要求（脚本复用 `resolve_approval` 即自动落审计），无需从外部抄。 |
| 官方方法论 | **12-factor-agents**（T3，humanlayer）durability 原则 | 「持久状态的所有权与生命周期必须显式」——支撑「wait 态要收敛、数据对账要显式脚本」的结论，非直接机制。 |
| 端到端可观测 | **langfuse**（T0 在用） | run_id↔trace 关联已备；本票无需新增埋点（resolve 走既有 `approval_{id}:{status}` 审计 + notification）。 |

**完成标准核对**：上表 ≥10 条目（agent-inbox、langgraph、conductor、open-swe、LangBot、orca、herdr、bisheng、codex/gemini-cli/cline、dify、12-factor-agents、langfuse），含 4 个诚实负结论（langgraph 无孤儿清理、bisheng sidecar 不持久、codex/cline/gemini 同步阻塞无此故障类、dify 无对账先例）。**唯一可迁移的正面机制**是「显式对账脚本 + 幂等 + dry-run」（open-swe/herdr/LangBot/orca 合流），本票方案即按此写。

---

## 2. 根因（Phase 2，双源钉死）

### 2.1 代码源（`read_file` 核实，函数名定位，行号仅作约）

1. **旧流**：`autonomy_service.check_and_enforce` 对 `delete_files` 达 L3 时创建 `ApprovalRequest`（`id=uuid5(run_id, "runtime-approval:{action}:{tool_call_id}")`，默认 `status="pending"`），调 `_request_approval`（飞书卡片 + web 通知），返回 `allowed=False`。`tool_step_service.py` 的 `_delete_autonomy_gate` 据此返回 `waiting_request`（`waiting_type=user`），run 进入 `waiting_started` 暂停（§2.2 数据证实）。
2. **resolve 路径**：`autonomy_service.resolve_approval` 对 runtime-scoped 审批调 `RuntimeCommandIntake(db).resume_run(ResumeRunCommand(...))`，`idempotency_key=f"approval:{approval.id}:{status}"`，payload `resume_type="user_input"` + `decision` + 硬编码话术（approved→"File deletion approved…" / rejected→"File deletion rejected. Do not execute…"）。权限：仅 creator 或 platform_admin 可 resolve。
3. **门控替换（根因触发点）**：`aa539999` 后，`_delete_autonomy_gate` 对**非 group delete 早退返回 `(None, None)`**（注释明示「Agent-scoped delete_file is now gated by the Maintainer gate; only group deletes still flow through the L3 autonomy approval flow」）；`_maintainer_file_gate` 接管 agent-scoped delete/edit/write/move（`GATED_DENIED → error_code="tool_permission_denied"`）。**从此 agent-scoped delete 不再产生 ApprovalRequest，也不再消费任何存量 pending。**
4. **对账缺口（最深层因）**：f077 迁移 docstring 明示「autonomy_policy 键清理 + 孤儿 pending delete 审批 resolve 是 out-of-band 数据操作，按 backend/alembic/AGENTS.md §2 不进迁移」。即：**门控替换审批流时，把「存量 pending 的对账」有意外置为后续手工步骤（S2），但该步骤一直未做** → 3 条 pending 成孤儿。全库 grep 无任何「陈旧/等待态审批回收器」（仅 `checkpoint_retention` 回收 checkpoint，对象不同）。

### 2.2 数据源（PG 只读，2026-09-07 实测）

| 事实 | 证据 |
|---|---|
| 全库 `approval_requests` 共 17 条，**全部 `action_type=delete_files`**（14 approved + 3 pending），无其他 action_type 审批 | `SELECT status,count(*) … GROUP BY status` → pending:3 / approved:14 |
| 3 条 pending 明细 | `75e02ec8`（run `1211274c`，删 `…/pinjaman/ui/apply`，08-20 08:27:32）；`2a41088e`（run `5c8dc096`，删 `jar_download.log`，08-20 08:27:26）；`3ae339cc`（run `8ea0a5a3`，删 `jar_download.log`，08-20 08:27:26） |
| 三者同 agent、同 creator | agent `27d55a64`（Android工程师 4），`creator_id=16820bb9…`，`requested_by=16820bb9…`（即 creator 本人） |
| 3 个 run 真悬挂（waiting_user） | 各自 `waiting_cnt=2, resumed_cnt=1`，末事件 `waiting_started("Runtime Run waiting user")`→`delivery_succeeded`，`last_event_at=08-20 08:27`；`updated_at` 停在 08-19/08-20，此后 16 天零变化 |
| 无资源泄漏（非 lane/锁持有） | 3 个 run `lane_held=False`、`lane_claimed_at=None`；agent `status=idle`、`is_expired=False` |
| resume 可行（checkpoint 未回收） | `langgraph_checkpoint.checkpoints` 两 thread（`f8bfa104`、`10c52b04`）各存 3 个 checkpoint，未被 retention 剪掉 |
| 无需担心「门控后仍会再产生审批」 | 该 agent `autonomy_policy.delete_files=L1`（且非 group delete 现已整体短路到门控），不再触发 L3 |
| 无其它待处理孤儿 | 全库仅这 3 条 pending，且全 agent-scoped（`workspace_scope="agent"`）；group-scoped delete 仍走 L3，当前无 group pending |

### 2.3 根因陈述（可证伪）

> 门控（`aa539999`）以维护者名单替换了 agent-scoped `delete_files` 的 L3 审批流，但存量 3 条 pending 审批因「对账被外置为 out-of-band 步骤且未执行」而成孤儿；对应 run 因 `resolve_approval` 从不被调用而永久停在 `waiting_user`，且全系统无任何 `waiting_user`/pending-approval 回收器兜底。

**可证伪判据**：若此为根因，则 ①3 条审批的 run 应停在 waiting_user（§2.2 证实）；②非 group delete 不应再新增 approval（§2.1-3 代码证实 + §2.2 全库零新增）；③「UI 缺失」假设不成立——`ApprovalsTab.tsx` 仍渲染 pending 列表并可调 `/agents/{id}/approvals/{id}/resolve`（即 creator 理论上仍可手工 resolve，只是 16 天未做）。三者全符合，根因成立且已追到最深（对账缺口），非上一层症状（「审批变孤儿」只是症状）。

---

## 3. 修复方案（Phase 3）

### 3.1 一次性 out-of-band 脚本

新文件 `backend/app/scripts/resolve_orphaned_delete_approvals.py`，范式对齐既有 `prune_runtime_checkpoints.py`（argparse、`--dry-run` 默认、`--apply` 才写库、安全护栏、`python -m app.scripts.…` 在 backend 容器内运行）。

**目标集**：`status='pending' AND action_type='delete_files' AND details->runtime_scope->workspace_scope='agent'`。
（`workspace_scope='agent'` 过滤是安全护栏：group-scoped delete 仍走 L3，**绝不触碰**。§2.2 证实当前 3 条 pending 全为 agent-scoped。）

**每条的处置**（复用 `resolve_approval`，不重写其逻辑）：

1. 幂等预检：目标集 SQL 已过滤 `status='pending'`，已 resolve 的行天然不在结果集内（重跑即空集、无事可做），无需逐条 skip 日志（防重入）。
2. 以 creator 身份 `resolve_approval(db, approval_id, creator_user, action="reject", resume_message=…)`：
   - 状态 `pending→rejected`，`resolved_at=now`，`resolved_by=creator.id`（§2.2 三者 `requested_by==creator`，与 `resolve_approval` 的「仅 creator/platform_admin」权限一致，天然通过）。
   - 落 `audit_logs`（`approval_rejected`，附 `approval_id` + `action_type`）——`resolve_approval` 已内建。
   - 向 creator 发 `approval_resolved` web 通知——`resolve_approval` 已内建。
   - `resume_run`（幂等键 `approval:{id}:rejected`）resume 原 run，payload 内容替换为「门控已接管」话术（见 3.2）。
3. `--dry-run` 打印每条将执行的动作与 run/agent/creator/路径；`--apply` 才提交。

**话术（`resume_message`）**：
> 文件删除的 L3 审批流程已被维护者门控取代，此前的删除申请已关闭且未执行。如仍需删除，请重新发起删除工具调用，系统将按维护者名单自动判定。

（诚实、可执行：既告知「未执行」，又给「重新发起会走门控」的出路。）

**安全性质**：幂等（`approval:{id}:rejected` 键防重复 resume；已 resolved 跳过）；dry-run 默认；只写 `approval_requests` 状态 + 审计 + 通知 + 排队一次 resume，不删行、不碰 checkpoint、不碰 group 审批。

### 3.2 最小生产代码改动（一处可选参数）

`autonomy_service.resolve_approval` 签名加 `resume_message: str | None = None`；resume payload 的 `content` 从硬编码三元表达式改为 `resume_message if resume_message is not None else <现硬编码话术>`（用 `is not None` 而非 `or`，空串也生效、语义更严）。默认 `None` 时行为与现在**逐字节一致**（既有 approve/reject 路径零回归）。仅脚本传入自定义话术。

> 规范权衡（`backend/AGENTS.md` L112「Do not widen a public service for one internal caller」）：本改动确为单一内部调用方（一次性对账脚本）加宽 `resolve_approval`，属有意为之——该参数**有当前消费者 + 本方案为 owning contract**，非投机扩展点；且它只是「注入话术」这一最小 seam，不改变状态机/审计/通知/幂等 resume 的任何既有行为。

> 为何不是「脚本直接改状态 + 自调 `resume_run`」：那会重复 ~15 行 resume 编排（幂等键格式、correlation_id、payload 结构），一旦 `resume_run` 契约演进脚本即漂移。复用 `resolve_approval` 是 Ponytail 阶梯第 2 档（复用既有 owner），改动面 = 一个可选参数 + 一条三元表达式。

### 3.3 回归测试

- **根因路径**：`resolve_approval` 传 `resume_message` 时，resume 命令的 `payload.payload.content == resume_message`（新断言）。
- **终态**：默认不传时 `content` 仍为原硬编码话术（既有断言不变，证明向后兼容）。
- **副作用禁止断言**：脚本对 `workspace_scope='group'` 的 pending **不触碰**（单测：构造 group-scoped pending 输入，断言 skip）。

### 3.4 影响面

- 改的契约：`resolve_approval` 新增可选参数（向后兼容，所有既有调用点不变）。
- 爆炸半径：脚本只影响「pending + agent-scoped delete_files」审批。用 `detect_changes`/`trace_path` 复核 `resolve_approval` 的唯一生产消费者是 `agents.py` 与 `enterprise.py` 的 resolve 端点，二者均不传新参数 → 行为不变。
- 不触碰：checkpoint、run 状态机、门控判定、group 审批流、DEFAULT_AUTONOMY_POLICY（S1 另立）。

### 3.5 防复发（P2，只记录不建）

本故障是一次性迁移缺口。防复发 = ①脚本的过滤条件（`action_type='delete_files'`、`workspace_scope='agent'`）是**显式常量**，未来同类「审批流替换/废弃」迁移时按需复制改造（YAGNI：不预设 CLI 参数化扩展点，避免投机抽象）；②部署 runbook / release note 固化不变式——**替换某 L3 审批流时，必须同批执行「存量 pending 对账」**（脚本或手工，不得再外置为待办）。**不建**「陈旧审批超时回收器」：全库 17 条审批、16 天才出 3 条孤儿，回收器是投机式加固，违宪法 II。

---

## 4. 七角度评审（Phase 4，每条含负向探针）

### Q1 根因是否正确？——**通过**

- **正向依据**：根因能解释 §2.2 全部证据——3 条 pending 对应 3 个 waiting_user run（E1）、非 group delete 零新增（E2）、creator 16 天未手工 resolve（E3）、无回收器兜底（§2.1-4 grep）。且追到最深层「对账被外置」。
- **负向探针（反例测试）**：若根因是「审批 UI 缺失」→ 前端 `ApprovalsTab.tsx` 不应渲染 pending 且不应有 resolve 按钮；实际它仍渲染并可 resolve（§2.3-③）。反例推翻「UI 缺失」假设，证「对账未执行」才是根因。若根因是「checkpoint 被回收导致无法 resume」→ `langgraph_checkpoint.checkpoints` 两 thread 各 3 条未剪（§2.2）；反例推翻。

### Q2 根治方案是否正确？——**通过（附风险标注）**

- **正向依据**：方案直击「对账缺口」——对 3 条孤儿执行 resolve + resume，关闭验收红线「3 条 pending 已 resolve」。
- **负向探针（删除测试）**：删掉脚本，问「run 会不会继续挂」→ **会**（无回收器、creator 16 天未动、门控后无任何路径消费 pending）。故脚本是根治该具体孤儿。
- **已知风险 + 缓解（实现时为「原子提交」，非「解耦」）**：resume 会让 16 天前的模型再推理一次（多花 ~3 次 token，可接受）。落地实现采用**原子提交**——`resolve_approval`（状态迁移 + 审计 + 通知 + enqueue resume）与脚本的 `db.commit()` 在同一事务（`enqueue_resume` docstring 明示「without committing the caller transaction」）；任一步抛错脚本 `rollback()` 整条回滚 → 审批仍 pending、可重试，绝不产生「审批已 resolve 但 run 仍 waiting」的半态，比「对账/恢复解耦」更安全。resume 命令在 resolve 时只是幂等入队（`approval:{id}:rejected` 键），不触碰 checkpoint；即便某线程 checkpoint 后续被 retention 剪掉，入队本身不报错，实际 resume 由 worker 异步执行、走既有 run 状态机收敛。

### Q3 参考资料是否正确？——**通过**

- **正向依据**：agent-inbox（真实源码研究）、langgraph（本地源码 `types.py` 读实）、herdr/orca/LangBot/open-swe（整库研究报告）均读真实源码，非 README 摘要；并诚实地把 codex/cline/gemini（同步阻塞、无此故障类）标为负结论。
- **负向探针（false friend 测试）**：conductor 的「workflow timeout」是否 false friend？核：conductor 是 Java 独立 durable workflow 引擎，Clawith 是 LangGraph checkpoint 暂停，机制不同——§1 已标「只借鉴『wait 态必须收敛』原则，不照搬」，无误。bisheng 的 `continue_run` 是否可抄？核：其 sidecar 状态不跨进程持久（研究报告明示），Clawith 用 AsyncPostgresSaver 更强——已标「不能学」，无误。

### Q4 副作用与爆炸半径是否排查完？——**通过**

- **正向依据（两子检查）**：
  - ①副作用面——resume 触发一次模型推理（3 次，可接受）；通知发 creator；审计落库；**无 exactly-once 外部写**（reject 分支不触发 `_execute_approved_action` 的直执行，仅 group+approved 才直删，本票全 reject）。②影响面——`resolve_approval` 唯一消费者是 `agents.py`/`enterprise.py` resolve 端点，均不传新参数 → 行为不变；脚本过滤 agent-scoped 排除 group 审批。
- **负向探针（漏消费者测试）**：特意找「会被脚本误伤的消费者」——group-scoped delete 审批流（仍走 L3）。核：脚本 `workspace_scope='agent'` 过滤排除，且当前 0 条 group pending（§2.2）。另一漏点：若误用 approve → `_execute_approved_action` 会直删 16 天前意图——本票硬约束 reject，不触发。均已覆盖。

### Q5 是否最优且必要？——**通过**

- **正向依据（两子检查）**：
  - ①候选枚举（4 个）：**A reject+resume（选中）**；B reject+cancel（`cancel_run` 终结 run，不喂模型）；C 只 reject 不清 run（留 waiting_user 僵尸）；D approve+resume（重放 16 天旧删除意图）。A 最诚实（reject=不执行该次删除 + 告知门控接管），复用既有 `resolve_approval`，唯一代价一个可选参数。
  - ②修的是「已发生故障」（3 个 run 真挂 16 天，§2.2 数据），非臆想风险；防复发定性为 P2 记录、不建回收器（宪法 II）。
- **负向探针（更简单档测试）**：试「`resolve_approval(reject)` 原样、零 production 改动」能否解决问题 → **能**（run 会 resume 且不执行删除），但 resume 话术是硬编码 "File deletion rejected. Do not execute…"，非 §8.2 明确要求的「门控已接管」，模型会被误导为「被人类拒绝」。加一个可选参数是满足 §8.2 的最小增量，故当前方案不多余。

### Q6 是否已有可复用逻辑？——**通过**

- **正向依据**：复用 `resolve_approval`（状态迁移+审计+通知+幂等 resume）、`RuntimeCommandIntake.resume_run`、`prune_runtime_checkpoints.py` 的 dry-run/--apply 脚本范式。知识图谱/代码 grep 无「孤儿审批回收」等价实现。
- **负向探针**：查「是否已有等价回收器」→ grep 全库无 stale/pending-approval reaper（仅 `checkpoint_retention`，对象不同）；resume 编排已内建在 `resolve_approval`，复用而非重写。结论「无等价独立实现，应复用 resolve_approval」。

### Q7 是否会破坏 Clawith 特性？——**通过**

- **正向依据**：宪法 C1（证据先行，§2 双源）C2（最小改动：一脚本+一可选参数）C3（契约所有权：只加可选参、不破既有）C4（回归测试 §3.3）C5（保留既有工作：不删行、不碰 checkpoint/group 审批）C6（数据边界：脚本只写 approval 状态+审计+通知）。红线：durable run/checkpoint 不直写（走 `resume_run` 幂等命令）、多租户（按 approval.agent_id 查 creator、resolved_by=creator、复用 resolve_approval 的权限校验）、exactly-once（`approval:{id}:rejected` 幂等键）、前缀缓存/WS 状态机/飞书通道不涉及。
- **负向探针（红线测试）**：对「checkpoint 语义」——脚本是否直写 checkpoint？否，走 `RuntimeCommandIntake.resume_run` 排队，不触碰。对「group 审批流」——`workspace_scope='agent'` 过滤 + 0 group pending。均不碰。

---

## 5. 落地闭环与验收（Phase 5 前置约定）

实现后按 skill `code-review` 双轴复核 diff 对照本方案；验收口径：

1. `approval_requests` 中 pending delete_files 计数 = 0（`--apply` 后实测；happy path——若某条 resolve 抛错，原子提交使其仍 pending、脚本退出码非 0 提示重试，不产生「审批已 resolve 但 run 仍 waiting」的半态）。
2. 3 个 run（`1211274c`/`5c8dc096`/`8ea0a5a3`）不再处于 waiting_user（到达 completed/failed 等终态）。
3. 无新增 delete_files 审批；group-scoped 审批流未受影响。
4. `resolve_approval` 默认路径回归测试全绿（`pytest backend/tests/…resolve_approval…` 相关）。

同时回写 `20260905-maintainer-gate-g3-g4-production-plan.md` §9.3 的「3 条 pending 已 resolve」为 ✅，并在此文档状态行标注 implemented + 实现 commit。

### 5.1 实测结果（2026-09-07 `--apply`，生产数据）

| 验收 | 结果 | 证据 |
|---|---|---|
| 1 pending delete_files = 0 | ✅ | `--apply` 后 3 条全 `status=rejected`（`resolved_at=14:05 UTC`、`resolved_by=creator 16820bb9`）；dry-run 重跑 `0 target` |
| 2 三 run 脱离 waiting_user | ⚠️ 部分（如实） | 见下 |
| 3 无新增审批 / group 未受影响 | ✅ | `SELECT count(*) … pending delete_files` = 0；无 group pending |
| 4 回归测试全绿 | ✅ | resolve_approval 相关 pytest 22 passed（autonomy 集 31 passed） |

**验收 2 逐 run 实测**：

- `1211274c` ✅ **run_completed**（14:08:13）：resume 一次模型推理后直接收敛（wait 节点消费，checkpoint step 619→706，末事件 `run_completed`）。
- `5c8dc096` ⏳ **resume 后重新活跃执行**：从 14:08 起持续改代码 + `android_compile`，至 15:43 仍在工具调用/流式输出（16 天前「优化 android 项目」任务被 resume 续跑，非「一次推理即止」）。
- `8ea0a5a3` ⚠️ **resume 被 worker 拒绝为 `thread_not_started`**：其 graph 从未在共享 thread `10c52b04` 上落 checkpoint（该 thread 全部 `clawith_run_id=5c8dc096`，属「多 run 共 thread」历史遗留）。审批 `3ae339cc` 已 rejected（孤儿已清），但 run 事件仍停 `waiting_started`（虚等待，无 graph interrupt 可 resume）。

**与方案预估的两处偏差（如实记录）**：① Q2 预估 resume 成本「~3 次 token」，实测 `5c8dc096` 触发完整任务续跑（>1.5h，模型继续 Android 任务），成本被低估一个量级；② §2.2 假设「两 thread 各 3 checkpoint」未识别 thread `10c52b04` 被 `5c8dc096`/`8ea0a5a3` 共享、checkpoint 全属前者，故 `8ea0a5a3` 无 graph 可 resume。两者均不影响核心目标（3 孤儿审批已 rejected、无新孤儿、门控接管），但 `8ea0a5a3` 的虚 `waiting_user` 事件残留为「共 thread 幽灵 run」遗留（可选 `cancel_run` 终结，非本票必需）。
