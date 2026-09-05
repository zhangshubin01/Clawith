# Agent 循环根治跨项目研究（17 项目）

> 2026-09-05 · 问题源头：Clawith 现有熔断器全是「签名级」，抓不住「换了姿势做同一件事」（git status/branch/checkout/fetch + read_file 混杂，每次参数不同 → 签名哈希每次不同 → `_trailing_identical_calls`/`detect_loop` 不触发）。本文回答：其他项目怎么**根治**，而不只是打签名补丁。
>
> 方法：逐仓库读真实代码（非 README 猜测），含诚实负结论。共 17 项目。

## 一、结论先行

**根治「换姿势做同一件事」的唯一共识：判定信号从「动作是否相同」（签名匹配）移到「任务/世界状态是否前进」（证据增益 / 材料变迁 / 语义进展）。**

17 项目里只有 5 个做到了根治级，它们全部放弃签名、改问「状态前进没有」；其余 12 个是签名级/硬上限级，被证明抓不住换姿势。Clawith 的现状（`_trailing_identical_calls` 连续 16 条、`detect_loop` 相邻同签名、`build_dup_read_ratio` 仅 read_file 滑窗）属于后者。

根治有两个**正交**维度，缺一不可：

| 维度 | 抓什么 | 信号源 | 代表项目 |
|---|---|---|---|
| 过程无进展 | 绕圈做无效工作（换姿势） | 证据增益计分 / 材料变迁 / LLM 语义 | DeepSeek-Reasonix / loopx / ouroboros / gemini-cli / crewAI |
| 结果未达标 | 自认完成但没做完 | grader 验收 → needs_revision → 强制继续 | deepagents RubricMiddleware |

## 二、根治级机制（信号落在「状态是否前进」）

### 1. 证据增益计分 + 零增益阶梯（DeepSeek-Reasonix，最完整）

`internal/evidence/progress.go` 给每轮工具调用打「增益分」，非签名：

```go
gainNewRead    = 1  // 首次读某 path；重复读同 path → 0
gainNewCommand = 1  // 首次跑某命令；重复 → 0
gainNewAction  = 1  // 首次 (tool,args)
gainNewFailure = 2  // 首次失败定位错误
gainStateChange= 2  // 曾失败的命令现在通过
gainMutation   = 3  // 成功的写/变更
gainRepeatFailure = -2

const explorationRunLimit = 6  // 连续 6 轮「只看不写」，连读新文件都不再计分
```

- **分工**（`progress_guard.go:98-119`）：storm breaker 抓「全失败」，progress guard 专抓「**工具一直成功但零产出**」——正是换姿势绕圈的盲区。
- **`explorationRunLimit=6` 封死「逐个读遍新文件」游走**（注释原话：*a loop that keeps opening files it has never opened scored positive every round*）。
- **阶梯干预**（`progress_guard.go:16-20`）：`streak 2 → nudge`，`4 → pivot`（强制换策略），`6 → stop`（Goal 强制 replan / chat 强制交最终答案 + loopGuardPass）。
- **Receipt**（`receipt.go`）= host-runtime 的每工具调用证据（ToolName/Args/Success/Command/Paths/Read/Write/Mutation/OutputDigest/ExitCode），per-turn 内存态，不进 prompt/checkpoint。

### 2. 计划层 material transition（loopx / ouroboros / crewAI）

- **loopx** `autonomous_replan_obligation.py`：信号 = goal/todo 是否发生 *material transition*（`OUTCOME_GAP/PROGRESS/PRIMARY_GOAL_OUTCOME`），连续 2 轮停滞 → 注入 P1 强制 replan 义务。完全不看工具签名。
- **ouroboros** `watchdog.py`：三时钟（safety/idle/no_progress），`_is_material_progress` 只认「材料事件」，`_workflow_material_fingerprint=(completed,total,terminal,statuses)` 真变了才算。注释原话：*"the canonical 'stuck doing busywork' signal"*。触发 → `task.cancel()` + UNSTUCK。
- **crewAI** `planning_types.py`：`TodoList` 状态机（`pending/running/completed/failed` + `depends_on`）+ `PlannerObserver` 判断 step 真完成 + 失败强制 replan（限 3 次）+ `max_method_calls = max_iter × 10` 兜底。

### 3. LLM 语义复核「forward progress」（gemini-cli）

`loopDetectionService.ts`：专门 diagnostic agent 判「是否 unproductive loop」，标准 = 重复模式 **且** 无 net change/forward progress，显式排除跨文件批量/增量编辑/带变体重试。双模型 double-check、动态检查间隔（置信度越高查越频）、带原始请求 contextualize、30 轮后才启动、阈值 0.9。代价：每次额外 1-2 次模型调用（成本敏感场景慎用）。

### 4. 结果验收语义复核（deepagents RubricMiddleware，另一正交维度）

`middleware/rubric.py`：声明「done 长什么样」，模型要结束时（无 tool call）调独立 grader 子 agent 打分。`satisfied`→结束；`needs_revision`（至少一 criterion 不过）→ 注入带 gap 的 HumanMessage → 强制继续；`max_iterations=3` 兜底。保守原则（`:153`）：*"every criterion you cannot positively confirm should be marked failed"*。grader 可用 verification tools 收集证据。→ 直接命中 dc557d91「自认 P1 未执行」。

### 5. 中间态：命令折叠归一（qwen-code，部分根治）

`loopDetectionService.ts:899-943`：`SHELL_COMMAND_STAGNATION` 把变参 git 巡检命令（status/diff/ls-files）折叠成单一 key `git-inspection`，连续 8 次触发。介于签名级和计划层之间；局限：不含 branch/checkout/fetch、会被 read_file 打断、受 `skipLoopDetection` 门控。

## 三、签名级 / 上限级（负结论，抓不住换姿势）

| 项目 | 机制 | 信号 | 干预 |
|---|---|---|---|
| OpenHands | `StuckDetector`（5 场景重复） | 事件对象相等 + 重复计数（仅忽略 pid/edit 前 3 行） | V0 暂停+人工 / V1 nudge→STUCK |
| DeepCode | `RepeatCallTracker` | (name, 规范化 args) 连续 (3,5,8) | 注入提醒（软）；web_fetch 硬阻断 |
| cline | `LoopDetectionTracker` + `MistakeTracker` | 键排序 JSON（soft3/hard5）+ 连续失败计数 | soft→remind / hard→terminate |
| opencode / kilocode | `doom_loop` | name+input 逐字连续 3 次 | 询问用户 permission.ask |
| SWE-agent | 成本上限 + `ScoreRetryLoop` | 成本、attempt、提交后 LLM 打分 | 环境重置整 attempt 重来 |
| codex | token 预算 + `update_plan` 自报清单 | token 上限、模型自报进度 | 预算超限终止 + 额度提醒 |
| openai-agents-python | `max_turns=10` | 纯轮次计数 | 抛 MaxTurnsExceeded |
| pi | 仅 compaction + 调用方钩子 | token 溢出 | 压缩 |
| Codewhale | turn_budget + goal 熔断 | 步数(200)/墙钟(3600s)/流；`NoProgress` 枚举已声明未接线 | Failed + final-report turn |
| deepagents | `recursion_limit=9_999` + 压缩三件套 | 硬上限、上下文压缩 | 无过程无进展检测 |

## 四、对 Clawith 的迁移结论

**Clawith 是 workspace 型 agent，「世界状态」= workspace 文件 + git 状态，「材料进度」= workspace 实际变更。** 这能把路线 1（证据增益）和路线 2（材料变迁）合并成一个比 DeepSeek-Reasonix 更精确的信号——因为 Clawith 有 Reasonix 没有的客观副作用证据：

- ledger 的 `workspace_file_revisions`（文件真实变更）≈ Receipt 的 `Mutation/Write`，且更强。
- `git status/branch/checkout/fetch` 不改变 workspace 材料状态 → 全是「零材料进度」，天然命中，直接封死 git 绕圈。
- Reasonix 的盲区「无限发明新命令串」在 Clawith 被「workspace 是否实质变化」这条更根本的信号消掉，不需要靠 `command 首次=+1`。

**原料已齐，缺的是 `ScoreRound` 计分器 + 零增益阶梯**：

| 工具 | 增益键 | 增益 |
|---|---|---|
| read_file | (path, content_hash) 首次 | +1（`build_dup_read_ratio` 已是特例） |
| execute_code | command + workspace 是否变更 | 有变更/失败→成功 +2；无变更 0 |
| edit_file | `workspace_file_revisions` 真实变更 | +3；空编辑 0 |

连续 N 轮「零材料进度」→ nudge / pivot / stop 阶梯（Goal 强制 replan，chat 强制最终答案）。

**结果验收闭环**：Clawith judge 平台（`run_outcome`/`attempt_count` 评分、judge evaluator/rule ID，票 03/04 已上线）是 rubric 的离线可观测原料（与运行期门分轨）；而「模型要结束时 → 验收 → needs_revision 强制继续」的运行期闭环**已由既有 `TaskCompletionGate`（LLM 判 pass/repair）+ `verify` 节点（repair → 注入消息 → 继续，`max_verification_repairs=10`）实现**，即 deepagents RubricMiddleware 的迁移，早于本观察已建成——原「缺的是…运行期闭环」不成立，已更正。

## 五、源码出处索引

- DeepSeek-Reasonix：`internal/evidence/progress.go`、`internal/agent/progress_guard.go`、`internal/agent/storm_breaker.go`、`internal/evidence/receipt.go`
- loopx：`loopx/control_plane/work_items/autonomous_replan_obligation.py`、`delivery_outcome.py`、`trajectory_hygiene.py`
- ouroboros：`src/ouroboros/evolution/watchdog.py`、`material_progress.py`、`convergence.py`、`directive_mapping.py`
- gemini-cli：`packages/core/src/services/loopDetectionService.ts`
- crewAI：`lib/crewai/src/crewai/utilities/planning_types.py`、`lib/crewai/src/crewai/experimental/agent_executor.py`
- deepagents：`libs/deepagents/deepagents/middleware/rubric.py`、`graph.py`
- qwen-code：`packages/*/services/loopDetectionService.ts`
- OpenHands：`StuckDetector`（stuck.py）
- DeepCode：`core/agent_runtime/repeat_guard.py`、`runtime.py`、`utils/loop_detector.py`
- cline：`packages/core/src/runtime/safety/loop-detection.ts`、`mistake-tracker.ts`
- opencode/kilocode：`packages/opencode/src/session/processor.ts`
- codex：`codex-rs/core/src/tools/handlers/plan_spec.rs`、`rollout_budget.rs`、`session/turn.rs`
- openai-agents-python：`src/agents/run_config.py`、`run.py`
- Codewhale：`crates/tui/src/core/engine/turn_budget.rs`、`tools/goal.rs`
