# run 所有权代际（严格单调代际）——生产级修复方案

日期：2026-09-07
状态：**暂缓（降级为观测），不实施**
定位：把立项前研究 `20260905-run-state-generation-study.md` 的结论评估到「是否落地」这一层。**最终裁决：不实施写前强制门控，降级为纯观测。** 保留本文档作为「评估过程 + 为何不实施」的完整记录。

---

## 0. 裁决

**不通过（暂缓，降级为观测）**。经重新权衡收益-风险，判定本方案为**投机式加固**（宪法 II 禁止）：它没有在修一个已发生的故障，且引入的风险耦合到真实文档化事故模式，收益-风险不对称。

1. **收益极薄**：要防的「旧 owner 迟到 checkpoint 写」需要「claim 会话失效 + advisory lock 连接断开 + checkpoint 池仍可写」三种**不同连接**的精确部分失效组合才能成立，实践中极难构造（进程死则三连接全死无迟到写；进程活则通常要么全活要么全受影响）。
2. **风险耦合真实事故**：「语义再平衡」会在 **PG 连接耗尽（53300，文档化事故）** 这类整体连接压力下，额外拒掉「本可凭 advisory lock 完成结算」的合法长图工作，在系统最需要韧性时放大恢复期动荡。

（结论一句话——本方案**曾经**设想：引入 `agent_runs.run_generation` 单调只增，唯一铸币点在 `claim_next_command` 的 re-claim 路径，随 checkpoint 元数据透传，在 checkpoint 写入处严格相等门控。该设计本身技术正确，但不值得其代价，故不实施。§3/§5 保留作完整评估记录。）

### 0.1 观测做法（已落地，2026-09-07）

不加任何字段、不拦任何写。唯一改动：`persistence.py::claim_next_command` 在检测到「过期 claim 被重认领」（改写前 `status=="claimed"`）时，打一条 loguru 日志 `Runtime command re-claimed after claim expiry ...`（含 run_id / command_id / type / previous_claimant / expired_ago_seconds / new_claimant）。

**观测对象（诚实标注）**：这是窗口的**前置条件**（re-claim 事件频率），不是窗口本身（「旧 owner 迟到 checkpoint 写」）。原因：不引入代际字段，checkpoint 写入点无从判断「这个写是否迟到」；re-claim 是唯一零字段、零侵入可观测到的代理信号。该事件此前完全静默，日志本身亦有独立运维价值（暴露 claim 过期频率 → 反映 worker 崩溃 / DB 压力）。

**重新立项判据**：日志显示 re-claim 在观测窗口内频繁发生（如单周期多次），或 Langfuse/PG 台账出现「同 run 同 command 双终态分歧」新事故。长期为零或极低 → 永久搁置。

---

## 0.2 以下为评估过程正文（含已否决的方案细节）

---

## 1. Phase 1 参考对比（≥10，含诚实负结论）

决策点拆成三个：①所有权代际挂哪、何时 +1；②「严格相等」门控怎么落；③恢复边界要不要下限。

| # | 项目 | 机制 | 对本方案的结论 |
|---|---|---|---|
| 1 | **LangBot**（源码级 `@ec63978`） | `placement_generation` 挂在 execution binding 上，`_observe_execution_context` 是唯一前进点，`<` 即 `raise rolled back` | **采纳**：代际属所有权、单点铸币、严格相等 |
| 2 | **orca**（源码级 `@af821260`） | `runtimeFence` 只随 acquisition CAS / proven eviction +1；`nextAgentSessionFence = max(fence+1, minimumNextFence)` | **采纳语义**（单点铸币+严格相等），**不采纳 `minimumNextFence`**（见 §3.4 Q4） |
| 3 | **herdr** | `SNAPSHOT_VERSION` 单调 + 未来版本拒绝 + 原子写 | **采纳**：单调版本 + 拒未来版本（对应 CHECK>=1 + fail-closed） |
| 4 | **letta-code** | `config-lock` 并发写锁 | 佐证「写入前互斥」必要性，但 Clawith 已有 advisory lock |
| 5 | **conductor** | 无代际（靠 worker lease 续约） | **诚实负结论**：同栈但不同问题，无对应机制 |
| 6 | **loopx** | 无代际 | **负结论**：不可读对应机制 |
| 7 | **jcode** | 仅时间戳 | **负结论**：时间戳非严格单调（NTP 回拨），否决作为唯一权威 |
| 8 | **open-swe** | 生命周期外包 | **负结论**：无 run 所有权代际 |
| 9 | **deepagents** | 靠 checkpoint 父链 | **负结论**：父链是内容链，非所有权链，不解决迟到写 |
| 10 | **bisheng** | sidecar 不持久 | **反面教材**：不持久的所有权状态会丢，代际必须落库 |
| 11 | **12-factor-agents** | Factor 5「unify state」单一真相源 | **采纳原则**：run 所有权权威收拢到单一持久字段 |
| 12 | **LangGraph 官方** | checkpoint 父链 + advisory lock 基础语义 | 确认 Clawith 的 mutex 基础正确，缺的是时序所有权 |

**完成标准核验**：12 条 ≥ 10，含 5 条诚实负结论（5/6/7/8/9），未停在本地三库，源码级条目（1/2）均经 `read_file`。

---

## 2. Phase 2 双源定根因

**重要前提（诚实定性）**：本项是**架构预防性改动（P2）**，非「已发生故障」的修复。触发源是两份独立研究报告源码级合流（LangBot + orca），不是一次线上事故。因此「双源」的第二源是**事故台账 + 设计决策注释**，而非一条新鲜的 Langfuse trace——没有可追的活故障。宪法 II（禁投机式加固）的核验见 §5 Q5。

### 2.1 源码源（第一源，均 read_file 核实）

三处仲裁原语互不共享单调整数（行号为「约」，并行会话会漂移，以函数名定位）：

| 处 | 位置（当前工作树） | 原语 | 有无代际 |
|---|---|---|---|
| claim | `persistence.py` `_claim_statement`(585-662) / `claim_next_command`(701-739) / `_require_claimant`(847-853) / `mark_command_applied`(856-893) | `FOR UPDATE SKIP LOCKED` + `claimed_by` + `claim_expires_at` + `status` + `attempt_count` | **无** |
| checkpoint | `thread_lock.py` `run_with_thread_lock`(60-88) + `checkpointer.py` `create_checkpointer`(170-195，durability="exit") + `langgraph_driver.py` `_execute_inner`(507，ainvoke 于 540/572/596) | PG 会话级 advisory lock（acquire-or-raise，专用连接）+ LangGraph 父链 | **无** |
| lease | `services/sandbox/execution_lease.py` `acquire`(98-110) / `_RENEW`(17-22) | Redis value CAS（value=`v1\|host:pid:uuid4`） | **无（但 token CAS 已 fail-closed）** |

关键事实（均已 grep/read_file 确认）：

- 全库**无** `run_generation`/`runtime_fence`/`thread_fence` 字段。`AgentRun`（`models/agent_run.py:27`）无 status/代际列；`AgentRunCommand`（`models/agent_run_command.py:25`）有 `attempt_count`(99，每命令恢复预算)/`claimed_by`(95)/`claim_expires_at`(96)。
- `_require_claimant` 只查 `status=="claimed" AND claimed_by==claimant`，**不查 `claim_expires_at`**（TTL 无关身份）。
- `mark_command_applied` docstring 明言「deliberately does NOT re-require the command claim」，理由是 claim 60s TTL 在长图执行中合法过期，结算改靠 thread advisory lock 兜底——即**「advisory lock 优先」的现状所有权语义**。
- **`claimed_by` 天然唯一**：`worker_service.py:160-163` `runtime_worker_claimant()` = `f"{hostname}:{pid}:{uuid4}"`，进程级 + uuid4，跨 worker 不重复。→ claim 层已有强所有权身份，代际在此层冗余（见 §3.2）。
- **checkpoint 写与 advisory lock 在两条连接上**：`run_with_thread_lock` 用 `engine.connect()` 的专用连接持锁；checkpoint 写走 `get_shared_checkpoint_pool`（`checkpointer.py:210+`）的独立连接池。→ 这是「advisory lock 管并发、代际管时序」并存的技术依据。
- **`SelectiveCheckpointSaver` 未接入生产**：`create_checkpointer` 直接 yield `AsyncPostgresSaver`；`SelectiveCheckpointSaver` 仅测试引用（`tests/test_selective_checkpointer.py`）。→ 门控落点必须加在 `create_checkpointer` 的**新薄包装**，不是 `SelectiveCheckpointSaver`。
- lease 已迁到 `services/sandbox/execution_lease.py`（此前在 agent_runtime 下）。

### 2.2 事故台账源（第二源）

- **`6f43d25b`（`incident-lessons-compendium.md`）**：部署杀在途 run → `durability="exit"` 中间零 checkpoint + DeepSeek 前缀缓存前几步逐字节复现、后几步分叉 → 同 id 不同内容。**已有修复**：幂等账本行已存在时复用终态 + 审计兜底。
- **claim 租约过期窗口**（`mark_command_applied` docstring 的隐含场景）：长图执行中 claim TTL 过期，若期间被 re-claim，旧 worker 的迟到结算靠 advisory lock 兜底而非重查 claim。

### 2.3 根因合成

**不是**「Clawith 缺并发控制」（SKIP LOCKED + advisory lock + Lua CAS 三层很扎实），而是：

> 三处仲裁各自是「时间/身份/token」语义，**没有一处「写入前严格相等门控」，也没有一个跨三处的单调计数器**把「同一 owner」串成一条链。最直接的缺口在 **checkpoint 写入处**：它是唯一「无任何所有权校验」的写入路径（claim 有 `_require_claimant`，lease 有 token CAS，checkpoint 只有一把瞬态互斥锁）。「旧 owner 的迟到 checkpoint 写」无法被拦下。

**根因可证伪性**：若「缺写入前代际门控」是根因，则「在 checkpoint 写前加严格相等代际校验」后，claim 已过期且被 re-claim 的僵尸 worker 的迟到 checkpoint 写应被拒绝（而非如现状被 advisory lock 兜底放行）。§5 Q1 独立复审此证伪。

**最深层因**：Clawith 已版本化「代码」（`graph_version`）和「输入」（`session_context_version`），唯独没版本化「所有权」——这才是两份参考项目合流所指的同一空洞。

---

## 3. Phase 3 方案（最小、可回退）

### 3.1 数据模型（迁移 `f078`）

```sql
-- agent_runs 新增
ALTER TABLE agent_runs
  ADD COLUMN run_generation INTEGER NOT NULL DEFAULT 1,
  ADD CONSTRAINT ck_agent_runs_run_generation CHECK (run_generation >= 1);

-- agent_run_commands 新增
ALTER TABLE agent_run_commands
  ADD COLUMN claimed_generation INTEGER NULL;
```

- `run_generation`：该 run 的 thread 被（重新）获取所有权的次数，单调只增，默认 1（存量行自动 1，正确）。
- `claimed_generation`：认领时快照。存量 pending/claimed 命令为 NULL，下次认领时回填；门控对 NULL（旧在途）跳过。

### 3.2 三处仲裁的处置（关键收窄）

| 处 | 处置 | 理由（负向依据） |
|---|---|---|
| **checkpoint（核心，唯一新门控）** | 在 `create_checkpointer` 加薄包装，`aput` 写入前比对 `metadata["clawith_generation"]` 与 `agent_runs.run_generation`，不等即 raise | 唯一无所有权校验的写入路径；advisory lock 与写在不同连接，锁管并发不管时序 |
| **claim** | `_require_claimant` **不加**代际 | `claimed_by` 已 uuid4 唯一（§2.1），re-claim 必改 `claimed_by`，`_require_claimant` 已能拒旧 owner；加代际纯冗余 |
| **lease** | **不动** | token CAS（每次 acquire 新随机 value）已 fail-closed 拒旧 grant 的 renew/release；且 lease scope 是 tenant/agent/session，与 run 所有权不同层，硬塞 run_generation 需跨层映射，属投机式加固 |

### 3.3 铸币点（唯一）+ 载体 + 门控

**铸币（唯一，`persistence.py` `claim_next_command`）**：

```python
# command.status 此刻是「改写前」状态：pending（首次）或 claimed（过期 re-claim）
was_reclaimed = command.status == "claimed"
...
if was_reclaimed:
    new_gen = await _bump_run_generation(db, command.run_id)  # 原子 UPDATE ... SET run_generation = run_generation + 1 RETURNING
    command.claimed_generation = new_gen
else:
    command.claimed_generation = await _read_run_generation(db, command.run_id)  # 快照当前，不 +1
command.claimed_by = claimant
command.status = "claimed"
command.claim_expires_at = now + timedelta(seconds=claim_ttl_seconds)
```

- 与 claim 同一事务（`_claim` 的 `db.begin()`）原子提交；`_bump_run_generation` 用单条 `UPDATE ... RETURNING` 拿行锁，避免并发 re-claim 丢失更新。
- **明确不 +1 的事件**：`begin_command_attempt`（attempt_count++）、resume、`renew_command_claim`（心跳续期）、`_acquire_start_lane`（lane 非 run 所有权）、sandbox lease。若在这些地方 +1，代际即退化成 `attempt_count`（研究 §5 明确警告）。

**载体（`checkpointer.py` `runtime_command_config`）**：metadata 增 `clawith_generation`（值 = 该 command 的 `claimed_generation`）。沿现有 `clawith_run_id`/`clawith_command_id` 同路透传，最小侵入。

**门控（`checkpointer.py` `create_checkpointer` 新薄包装 `RunGenerationGateSaver`）**：在 `aput(config, checkpoint, metadata, new_versions)` 里、委托 `inner.aput` 之前：

```python
run_id = metadata.get("clawith_run_id")
gen = metadata.get("clawith_generation")
if run_id and gen is not None:
    current = await self._read_run_generation(uuid.UUID(run_id))  # 注入的 reader，走主库 session
    if current != gen:
        raise RunGenerationStaleError(run_id=run_id, expected=gen, current=current)
return await self._inner.aput(config, checkpoint, metadata, new_versions)
```

- **不吞写、不跳过 checkpoint**：gen 匹配则原样透传（不破坏 `create_checkpointer` docstring 警告的父链）；gen 不符则 raise（fail-closed），不存在「swallow writes 断链」。
- `run_id` 或 `gen` 缺失（部署前启动的旧在途调用）→ **跳过门控**，保证不回滚在途工作。
- `_read_run_generation` 以 callable 注入包装器构造点，读取 `agent_runs`（`search_path=langgraph_checkpoint,public` 下 public 可见）。

**worker 侧错误映射（`command_worker.py`）**：`RunGenerationStaleError` 沿 `ainvoke` 抛出，被 `run_once` 现有 `except Exception`（1235）承接 → `_release_for_retry` → command 回到可认领，下一个 worker 以新代际重认领重跑。**run 状态完好**（写被拒发生在持久化前），工具副作用靠产品表幂等，代价仅是 LLM 重跑（与现有「crash→重跑」成本模型一致）。

### 3.4 回答 §6 四个开放问题

- **Q1 铸币事件精确清单**：唯一铸币 = `claim_next_command` 的 re-claim（`status==claimed AND claim_expires_at<now`）。首次认领（pending）不 +1 只快照。明确排除 attempt/resume/renew/lane/lease。一个诚实后果见 §5 Q4。
- **Q2 与 `6f43d25b` 账本关系**：**互补不替代**。代际 = 写入前拦截（拦「旧 owner 迟到写」）；账本 = 写入后复用终态（处理「新 worker 重放分叉」——而重放的新 worker 代际是对的，代际拦不住它）。账本**不降级**为代际兜底，两者各司其职。
- **Q3 advisory lock 与代际分工**：**并存不可替代**。advisory lock 管「同一时刻唯一写者」（并发互斥、进程死即释放、无持久化）；代际管「跨时刻谁最新」（持久单调、跨进程/重启存活）。代际门控是「读→写」两步有 TOCTOU 窗口，不能替代真正的写互斥。
- **Q4 恢复边界**：**不需要 orca `minimumNextFence`**。orca 需要下限是因 store 从备份恢复后「未落地 commit 可能已发 fence」、严格相等会重发。Clawith 的 `run_generation` 在单 PG 持久层，+1 与 claim 同事务原子提交；PG WAL 保证已提交 +1 持久、未提交 +1 回滚，无独立「备份恢复 + 未落地 commit」窗口。需处理的只有「旧在途调用 metadata 缺代际」→ 门控跳过（§3.3）。

### 3.5 回归测试与影响面

- **回归测试**（根因路径 + 终态各一条）：
  1. 根因路径：claim 过期 → re-claim 使 `run_generation`+1 → 旧 worker 迟到 `aput` 携带旧 `clawith_generation` → 断言 raise `RunGenerationStaleError` 且 checkpoint **未**持久化。
  2. 终态：正常 start→resume 全程（心跳续期、无 re-claim）→ `run_generation` 恒 1 → 所有 `aput` 透传，checkpoint 正常落库，command 正常 applied。
  3. 副作用断言：代际不符被拒后，`agent_run_commands` 无「半 applied」态、产品表无重复写（工具幂等），`run_generation` 仍单调（不因失败回退）。
- **影响面**：改的契约 = `runtime_command_config` 的 metadata 新增一个键（只增不减，旧消费者无感）；`claim_next_command` 新增一次原子 UPDATE（仅 re-claim 路径，影响面=认领热路径的 re-claim 子集）；`create_checkpointer` 增一个薄包装（影响面=所有 checkpoint 写，但门控仅在带代际的 runtime 调用生效）。消费者过一遍：`aget_state_history` 的 filter（`langgraph_driver.py:443-449`）不读代际，不受影响；reconciler/debugger 读 checkpoint 不写，不受影响。

---

## 4. 代码级事实出处清单（函数名定位，行号约）

- `persistence.py::_claim_statement`(585) / `claim_next_command`(701) / `_require_claimant`(847) / `mark_command_applied`(856) / `renew_command_claim`(1062) / `release_command_claim`(1081)
- `thread_lock.py::run_with_thread_lock`(60) / `thread_lock_key`(40)
- `checkpointer.py::runtime_command_config`(59) / `create_checkpointer`(170) / `get_shared_checkpoint_pool`(210)
- `langgraph_driver.py::execute`(468) / `_execute_inner`(507)（ainvoke 540/572/596）
- `sandbox/execution_lease.py::acquire`(98) / `_RENEW`(17)
- `worker_service.py::runtime_worker_claimant`(160)
- `models/agent_run.py::AgentRun`(27)；`models/agent_run_command.py::AgentRunCommand`(25, attempt_count 99 / claimed_by 95 / claim_expires_at 96)
- `selective_checkpointer.py::SelectiveCheckpointSaver`(75) —— 未接入生产（仅测试引用）

---

## 5. Phase 4 七角度评审（每条 裁决 → 正向依据 → 负向探针）

**Q1 根因找的是否正确？** 裁决：**通过**。
- 正向依据：根因「三处仲裁无共享单调计数器、且 checkpoint 写是唯一无所有权校验的路径」能解释双源全部证据——①源码：claim 有 `_require_claimant`、lease 有 token CAS、checkpoint 只有瞬态锁（§2.1）；②事故：6f43d25b 是「新 worker 重放分叉」（代际拦不住、账本拦），claim TTL 过期窗口是「旧 owner 迟到写」（代际正拦这个）。两条症状分别由不同机制解释，根因精确到「checkpoint 写缺写入前所有权门控」这一层。
- 负向探针（反例测试）：若根因是「缺代际门控」，则 claim 层应**没有**所有权校验才对——但事实相反，`_require_claimant` 有强校验（`claimed_by` uuid4 唯一）。这说明根因不能泛化为「三处都缺」，必须收窄到「仅 checkpoint 写缺」。我据此把方案从研究的「三处门控」收窄为「一处门控」（§3.2），根因与方案严格对齐。**证实**（收窄后成立）。

**Q2 根治方案是否正确？** 裁决：**通过**。
- 正向依据：方案改的正是 Q1 定出的「checkpoint 写缺写入前所有权门控」——加单调计数器（铸币）+ 严格相等门控（`aput`），而非给 `attempt_count` 换名字（铸币点排除 resume/attempt）。
- 负向探针（删除测试）：删掉方案，问「根因会不会再发作」——**会**：checkpoint 写仍无所有权校验，claim 过期的僵尸 worker 迟到写仍靠 advisory lock 兜底放行。方案删不得 → 根治。

**Q3 参考的资料是否正确？** 裁决：**通过**。
- 正向依据：两条源码级依据（LangBot/orca）确为**同类问题**（所有权代际 / 租约 fence），非同名 false friend；已 `read_file` 其源码（研究 §1.1/§1.2）而非 README；bisheng 是作反面教材用（明确标注），未当第一依据；停更项目（herdr/letta-code）仅作机制佐证，非第一依据。
- 负向探针：我找了一个可能引用错的点——orca 的 `minimumNextFence` 若照抄会在 Clawith 制造「恢复后双发 fence」的**不适用**机制。核下来：Clawith 单 PG 持久层、+1 与 claim 同事务，无 orca 的「备份恢复 + 未落地 commit」窗口，照抄是错的；我据此**明确不采纳**该下限（§3.4 Q4）。**无误，且发现一处若照抄会错的点已标注不采纳**。

**Q4 副作用与爆炸半径是否排查完？** 裁决：**通过（附 1 条已知风险）**。
- 正向依据：①副作用面——门控 raise 不写 checkpoint（无半写态）、工具副作用靠产品表幂等（exactly-once 保持）、不吞写不断父链（不破坏 `create_checkpointer` docstring 警告）、不新增连接/资源；②影响面——metadata 只增键（旧消费者无感）、`aget_state_history` filter 与 reconciler/debugger 均只读不受影响、`claim_next_command` 仅 re-claim 子路径多一次原子 UPDATE。
- 负向探针：我特意找了一个方案可能漏的消费者——`mark_command_applied` 的「长图 claim 过期仍结算」路径（§2.1 的「advisory lock 优先」）。核下来：**确实受影响**——若长图执行中 claim 被 re-claim，旧 worker 的结算会因 checkpoint 写被拒而连带失败。这是**有意为之的语义再平衡**（见 §0 风险 1），且 fail-closed、无数据损坏、自愈（下一 worker 重跑）。**标注为已知风险 + 缓解**：缓解 = 心跳续期保证正常长图不触发（re-claim 需 claim 真过期）、失败自愈、产品表幂等兜底。

**Q5 这是最优且必要的方案吗？** 裁决：**通过（附 1 条已知风险）**。
- 正向依据：①枚举 ≥3 候选——（a）更简单：只加 `_require_claimant` 到结算（否决：claimed_by 已唯一，冗余且破坏长图 TTL 过期结算）；（b）更彻底：三处全加代际 + minimumNextFence（否决：claim/lease 已有等价保护，属投机加固，违反宪法 II）；（c）本方案：一处门控 + 唯一铸币 + 载体（Ponytail 阶梯最低有效档）。②修的是「已发生故障」还是「臆想风险」——诚实定性为 **P2 预防**（触发源是研究合流，非线上事故），非 P0 已损；删除方案后观察到的故障（6f43d25b 类）**仍在**（那是账本管，代际不管），故本项是「补上缺失的所有权版本化」的预防性加固，优先级 P2、不冒充 P0。
- 负向探针：我试过用**更简单一档**（a：只把 claim 校验加到结算路径）能否解决——**不能**：它不动 checkpoint 写（真正无校验的路径），且会因长图 TTL 过期误杀合法结算。故当前「一处门控」是必要且最小档。**证实当前档正确**。

**Q6 是否已经有可复用的逻辑？** 裁决：**通过（部分复用）**。
- 正向依据：载体复用现有 `runtime_command_config` metadata 通道（`clawith_run_id`/`clawith_command_id` 同路）；读 `run_generation` 复用主库 session factory（同 `_load_run` 的 `select(AgentRun)` 模式）；铸币复用现有 `_claim_statement` 的 re-claim 判定（`status==claimed`）。无现成的「代际门控」可复用（已确认全库无该字段）。
- 负向探针：我特意查过知识图谱/代码是否已有等价「所有权代际」逻辑（`graph_version`/`session_context_version`/`WorkspaceFileRevision`/`lane_held`）——结论**无**：graph_version 是代码构建版本、session_context_version 是输入快照版本、lane_held 是布尔 lane 占用、WorkspaceFileRevision 是文件修订，四者皆非「run 所有权单调代际」（研究 §5.3 已论证）。**无等价逻辑，需新建**。

**Q7 会破坏 Clawith 的特性吗？** 裁决：**通过**。
- 正向依据：逐条过宪法 C1–C6 + 红线——C1 证据先行（本方案以源码+事故台账双源钉死）；C2 最小改动（一处门控，不动 claim/lease，不重构）；C3 契约与状态所有权（代际即「所有权」的显式化，正是强化 C3）；C4 测试证行为（§3.5 三条回归）；C5 保留既有工作（存量行默认 1、旧在途 metadata 缺代际跳过）；C6 模块化与数据边界（门控薄包装独立，不越界）。红线——checkpoint 语义（不吞写不断父链）、多租户隔离（run_id 级门控，不跨租户）、exactly-once（工具幂等保持）、前缀缓存稳定性（代际不碰模型层）、WS 状态机（不碰）、飞书通道（不碰）。
- 负向探针：我试过把方案对「checkpoint 父链」红线过一遍——门控 raise 会不会断链？核下来**不碰**：gen 匹配时原样透传（父链完整），gen 不符时 raise 在**写之前**（无半写 checkpoint 进链），不会出现「跳过的 checkpoint」导致的断链（`create_checkpointer` docstring 警告的场景是「swallow writes」，本方案不 swallow）。**不碰红线**。

---

## 6. Phase 5 实现闭环（待实施）

方案实现为 diff 后，**必须**跑 `code-review` 对照本方案复核：Spec 轴核对 diff 是否忠实实现 §3 的门控/铸币/载体，无范围外改动、无「评审未提过的机制」。偏离则回改或回 §3/§5 重审。**跳过此步不算交付闭环。**（本条在实施时执行。）

---

## 附：实施清单（顺序）

1. 迁移 `f078`：`agent_runs.run_generation` + `agent_run_commands.claimed_generation`（含 CHECK）。
2. `persistence.py`：`_bump_run_generation` / `_read_run_generation` 两辅助 + `claim_next_command` 铸币（was_reclaimed 判定）。
3. `checkpointer.py`：`runtime_command_config` 增 `clawith_generation`；`create_checkpointer` 加 `RunGenerationGateSaver` 薄包装 + `RunGenerationStaleError`。
4. `langgraph_driver.py`：`_execute_inner` 构造 config 时传入 `claimed_generation`。
5. worker 侧：确认 `RunGenerationStaleError` 走现有 retry 路径（可加专门 error_code `run_generation_stale`）。
6. 回归测试三条（§3.5）+ `scripts/arch-guard.sh`。
7. **实现后跑 `code-review`（Phase 5）闭环。**
