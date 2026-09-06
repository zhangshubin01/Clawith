# 「run 状态需要严格单调代际」跨报告合流发现 —— 立项前研究

日期：2026-09-05
状态：**完成**（三份源码均经 `read_file` 核实：LangBot `@ec63978`、orca `@af821260`、Clawith 工作树 `@HEAD`）
定位：**跨报告发现的可信度核验 + 立项可行性评估**，非实现方案。上游两份研究报告：
- `20260905-langbot-study.md` §8（`placement_generation` 单调代际）
- `20260905-orca-study.md` §2/§4（`runtimeFence` 单调计数租约）

---

## 0. 一句话结论

两份独立项目确实指向同一结论，且经源码核实**成立**：**「run 状态需要严格单调代际」——用一个只增不减的整数作为「谁是最新执行体」的比较基准，在 claim/checkpoint/lease 三处用「代际严格相等」门控写入，不相等即拒绝（fail-closed）。** Clawith 当前用 `attempt_count`（次数）+ 时间戳 + 身份 + 随机 token 四件套仲裁，缺的正是这个「严格单调 + 严格相等」的代际；它是本次 26 条可迁移点里**可信度最高、且最该先动**的一条。但「怎么动」有三个关键 nuance 必须先讲清楚，否则会立错项（见 §5）。

---

## 1. 两个外部机制的确切语义（已核源码）

### 1.1 LangBot `placement_generation`：挂在「placement 所有权」上的单调代际

- **代际挂在哪**：`ExecutionContext` 携带 `placement_generation`（`botmgr.py:56`、`pipelinemgr.py:94`），其权威来源是 workspace 的 **execution binding**（`workspace_service.get_execution_binding(workspace_uuid, expected_generation=...)`）——即「哪个 instance/进程当前拥有这个 workspace」这一事实。**代际属于所有权，不属于单条消息/单个 run。**
- **回滚守卫**（`pipelinemgr.py:532-542` `_observe_execution_context`）：
  - `context.placement_generation < previous_generation` → 直接 `raise WorkspaceInvariantError('...rolled back')`（只增不减）；
  - `==` → 幂等返回；`>` → 清掉该 scope 下所有缓存的 pipeline 再前进。
- **fail-closed 重验**（`pipelinemgr.py:152-172` `_assert_execution_active`、`botmgr.py:91-97`）：每两个 stage 之间、每次输出前，都**重新从 store 拉当前 binding 并按 `expected_generation` 严格比对**；`binding.instance_uuid` 或代际不符即 raise。不是只对本地缓存，是对**权威 store** 重验。
- 构造期强约束（`pipelinemgr.py:107`）：`placement_generation <= 0` 直接拒建运行时。

### 1.2 orca `runtimeFence`：挂在「session 租约」上的持久单调计数

- **字段语义**（`agent-session-record.ts:86` 注释原文）：`runtimeFence` = "durable monotonic integer; **only acquisition CAS and proven eviction move it**"——只在「获取成功」与「确证驱逐」两处 +1，**不在每次状态转移 +1**。
- **唯一铸币点**（`agent-session-next-fence.ts:15-17`）：
  ```ts
  return Math.max(lease.runtimeFence + 1, lease.minimumNextFence ?? 0)
  ```
  关键在 `minimumNextFence` **下限**：store 从备份恢复后，「那笔没落地的 commit 可能已经发出过一个 fence」，而严格相等比较会把这个号再发一次、制造双写者——所以恢复时只记下限、不动当前值。
- **严格相等门控**（`agent-session-lease-adjudication.ts:104-106`）：`isAgentSessionFenceCurrent = Number.isSafeInteger(fence) && fence === lease.runtimeFence`。
- **每个 mutation 携带 `expectedRuntimeFence`**（`agent-session-mutation-envelope.ts:84-127`）：写入前固定顺序校验 `fingerprint → ledger(replay) → lease-writer → fence`，`expectedRuntimeFence === null || 不相等` → 拒绝 `agent_session_checkpoint_stale`。
- **全程 fail-closed**（`agent-session-lease-adjudication.ts` 头注释）：**到期绝不单独授予第二个 owner**；无法验证的进程按「可能还活着」处理；无法证明 owner 的 stage 持续追问或转人工恢复——宁可少写，不可双写。

### 1.3 合流点：为什么两份独立项目得出同一结论

它们不是「偶然同名」。两者的共同内核是三条：

1. **代际是「所有权」的，不是「尝试次数」的**——回答「我这个执行体还是不是最新的」，而非「我重试了几次」。
2. **严格相等 + fail-closed**——不是「我的代际 >= 你的」这种偏序，而是 `==` 精确相等；对不上就拒绝，绝不猜测谁新。
3. **单点铸币 + 恢复下限**——所有 +1 走一个函数（orca 的 `nextAgentSessionFence`；LangBot 的 `_observe_execution_context` 是唯一前进点），并处理「备份恢复 / commit 未落地」这类会让严格相等误发的边界。

---

## 2. Clawith 现状：三处仲裁的原语（已核源码）

| 处 | 文件:行 | 原语 | 性质 |
|---|---|---|---|
| **claim**（命令认领） | `persistence.py:585-662` `_claim_statement`、`:701-739` `claim_next_command`、`:1062-1078` `renew_command_claim`、`:847-853` `_require_claimant` | `FOR UPDATE SKIP LOCKED` + `claim_expires_at=now+60s` + `claimed_by` 身份 + `status` + `attempt_count` | 身份 + 时间 + 次数 |
| **checkpoint**（图状态写入） | `thread_lock.py:60-88` `run_with_thread_lock`（PG session 级 advisory lock，key=`blake2b(runtime_thread_id)`）、`checkpointer.py:181-187`（`durability="exit"`）、`event_stream.py:305-313` `run_is_terminal`（终态由最新事件推导，**无 status 列**） | 二进制互斥锁（acquire-or-raise）+ LangGraph checkpoint 父链 | 互斥，**无代际** |
| **lease**（沙箱执行租约） | `execution_lease.py:17-28`（Lua `_RENEW/_RELEASE`）、`:98-110` `acquire(nx=True, value=v1|host:pid:uuid4)` | Redis **value 匹配 CAS**（每次 acquire 随机新 value） | token 匹配，**无全序代际** |

补充事实（已 grep 全库确认）：

- `AgentRun`（`agent_run.py:27-194`）**没有 status 列、没有代际列**；`attempt_count` 在 `AgentRunCommand`（`agent_run_command.py:99`）上，是**单命令恢复预算**（`begin_command_attempt` 里 `attempt_count += 1`、上限 `max_attempts`），每命令独立、不是「谁最新」。
- 全库**无** `runtimeFence`/`generation`/`epoch` 意义上的所有权代际字段；只有 `time.monotonic()`（计时 TTL）、`graph_version`（**代码构建版本**）、`session_context_version`（**输入快照版本**，`state.py:74`）、`WorkspaceFileRevision`（文件修订）。这些都是「近亲但不同」——Clawith 已经版本化了**代码**和**输入**，唯独没版本化**所有权**。
- 三处仲裁**互不共享**同一个单调整数：命令认领靠时间戳过期重认领，线程锁是纯互斥，沙箱租约是随机 token。一个「旧 worker 的迟到写」无法被一个统一代际在写入前拦下。

---

## 3. 差距的精确刻画

**不是**「Clawith 没有并发控制」（它有，且相当扎实：SKIP LOCKED + advisory lock + Lua CAS 三层），而是：

> 三个并发原语各自独立、**都是「时间/身份/token」语义而非「代际」语义**，且**没有一处「写入前严格相等门控」**。

由此产生两个具体症状，均已在本工作区留下事故记录：

1. **部署杀在途 run → 重放内容分叉**（`6f43d25b` 已闭环，`[[incident-lessons-compendium]]`）：`durability="exit"` 中间零 checkpoint + DeepSeek 前缀缓存前几步逐字节复现、后几步分叉 → 同 id 不同内容。**现有修复是「事后」幂等账本复用终态 + 审计兜底**——而 orca 的 `expectedRuntimeFence` 是「写入前」就拒绝陈旧代际，不必事后审计。这正是两份研究反复点名的差异。
2. **多 run 共 thread / claim 租约过期窗口**（`mark_command_applied` 的 docstring 明言「deliberately does NOT re-require the command claim」——60s TTL 在长图执行中合法过期，结算改靠 thread advisory lock 兜底）：意味着 claim 层和 checkpoint 层是**两把不相干的锁**，没有共享代际把它们串成一条「同一个 owner」链。

---

## 4. 设计空间（候选锚点，非最终方案）

引入一个字段，把它穿到三处。候选：

- **锚点**：`agent_runs` 新增 `run_generation`（或更贴切地叫 `thread_fence`），语义 =「该 run 的 thread 被（重新）获取所有权的次数」。**只在 owner 变更时 +1**（对齐 orca「only acquisition/eviction move it」），绝不在每次 resume/attempt +1（否则就退化成 `attempt_count++`）。
- **铸币点（唯一）**：`_acquire_start_lane` / `claim_next_command` 认领成功、以及「lost claim → re-claim」路径——即所有「新 owner 上位」处。一个 `next_run_generation()` 辅助函数集中 +1，并对齐 orca 的 `minimumNextFence` 处理「备份恢复 / commit 未落地」边界。
- **门控点（三处写入前严格相等）**：
  1. **claim**：`_require_claimant` 升级为「`status==claimed AND claimed_by==me AND generation==我看到的`」；
  2. **checkpoint**：在 `run_with_thread_lock` 内、写 checkpoint 前比对 `runtime_command_config` 里带的 `clawith_generation` 与当前 `run_generation`，不符即拒（而非现在 `mark_command_applied` 干脆不查 claim）；
  3. **lease**：`SandboxExecutionLease` 的 value 里加入代际，`_RENEW/_RELEASE` 脚本比对 `value + generation` 而非仅 value，让「旧 grant 的迟到写」被显式拒绝（对齐 LangBot `revocation_revision`）。
- **载体**：代际可随 `runtime_command_config` 的 `metadata`（`checkpointer.py:59-75`，现已有 `clawith_run_id`/`clawith_command_id`）透传，最小侵入。

---

## 5. 三个关键 nuance（防立错项）

1. **代际在「所有权」上，不在「run/尝试次数」上。** 两个参考项目的代际都是 owner/placement 变更才 +1，**不是**每次 resume +1。若把 `run_generation` 做成「每 resume +1」，就只是给 `attempt_count` 换个名字，无法回答「旧 worker 的迟到写 vs 新 worker 的写」这个真问题。
2. **Clawith 已有半数原语，缺的是「共享单调计数器 + 严格相等门控」，不是缺锁。** thread advisory lock（互斥）、sandbox lease（token CAS）、`_require_claimant`（身份+状态）都在位；真正缺的是**一个跨三处的单调整数 + 每处写入前的 `==` 门控**。所以这是一个「补一个字段、穿三处、加一个铸币函数」的中等改动，不是「另起一套租约系统」。
3. **`graph_version` / `session_context_version` 都不是它。** 前者是代码构建版本、后者是输入快照版本——Clawith 已版本化「代码」和「输入」，独缺「所有权」。别把它们误当成现成代际复用。

**风险**（若铸币点选错）：
- 代际在错误事件上 +1 → 合法 resume 被误拒（丢活）；代际在正确事件上漏 +1 → 该拦的迟到写没拦住（等于没做）。orca 的「fail-closed + 单点铸币 + minimumNextFence」三件套正是为压住这两个方向的风险，照抄语义而非只抄字段。

---

## 6. 建议与开放问题

**建议**：立项方向正确，且优先级排第一（本次 26 条里可信度最高）。但**立项边界要收窄为「引入一个 run 所有权代际并穿三处」**，而非泛化的「引入租约/状态机」。落地前需先回答：

1. **铸币事件的精确清单**：哪些事件算「owner 变更」（start lane 认领？lost-claim 重认领？thread 被 re-claim？沙箱 lease 被抢占？）——这是最易出错的一环。
2. **与 `6f43d25b` 幂等账本的关系**：代际是「写入前拦截」、账本是「写入后复用终态」，二者是互补不是替代。要不要让代际成为账本的第一道防线，账本降级为兜底？
3. **thread advisory lock 与代际的分工**：advisory lock 已保证「同一时刻只有一个写者」；代际解决的是「跨时刻的两个写者谁新」。是否需要两者并存，还是代际可替代部分 advisory lock 场景？
4. **恢复边界**：Clawith 的 checkpoint 有 `durability="exit"` 语义，代际是否需要 orca 式 `minimumNextFence` 下限来处理「备份/checkpoint 未落地」？

**下一步**：若确认立项，走 `grill-with-docs`（本方案是写代码级方案前的「设计」阶段，参考 `[[plans-compare-reference-materials]]` 的流程），产出 ADR + 拆分到 `to-tickets`；实施按 constitution C1–C6 + `tdd` + `scripts/arch-guard.sh`。
