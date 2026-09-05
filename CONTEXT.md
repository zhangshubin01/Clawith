# Clawith Runtime Context

Clawith 多租户企业 Agent 平台的运行时（Runtime）词汇表：LangGraph 图执行、命令队列与工具执行账本的核心术语。本文件只收录本项目独有的概念，不含通用编程术语。

## Language

**Path grounding（路径接地）**:
模型在未见真实目录结构时，用语言约定（如 Java 包名）脑补文件路径导致工具路径未命中的问题域。三级契约逐层治理：L1 路径契约注入（参数描述声明相对根）、L2 失败诊断（最深祖先/条目/前缀型 Did you mean）、L3 存储侧有界 basename 定位（失败时给出 storage 验证过的真实候选，只建议不代读）。契约与升级判据见 ADR-0013；实证样本：2026-09-01 run 6a1c0eab（mydome1→calculator 包名幻觉）。

**Command**:
一条可持久化的运行控制消息（start / resume / cancel），由 daemon 认领并驱动一次图推进；认领、尝试次数、应用状态全部落库，进程死亡后可由其他 daemon 恢复处理。

**Claim（认领）**:
daemon 对 Command 的带 TTL 排他占用；持有期间心跳续期，到期视为持有者死亡、可被重新认领。

**Tool Execution（工具执行账本行）**:
一次精确工具调用的持久化收据：记录调用身份、attempt、状态（started/succeeded/failed/unknown）、lease 与结果。同 call_id 的重复调用被账本识别并拒绝重放。

**Lease（执行租约）**:
Tool Execution 处于 started 时的排他执行窗口；持有者定期续期，到期视为执行者死亡。safe read 的自动重试与孤儿收据的接管都以 lease 为界。

**Waiting（等待边界）**:
LangGraph interrupt 造成的合法长驻态：Run 停在 waiting_started 事件处等待外部输入（用户回复/审批），收到 resume 命令后继续。驻留期间命令已 applied、无 claim、无工具 lease，也不产生新事件——事件流的 idle-timeout 不得将其误判为死亡；重连附着时若客户端 cursor 已在边界之后，附着直接在边界处以 waiting_user 收尾，不再重放。

**Stream Admission Gate（入流门禁）**:
Direct Chat 消息泵在起流前的车道感知裁决（ADR 0012）。调度车道（scheduling lane）按 thread 串行化执行：持有者最多一个、只有它能执行或停驻 waiting，且只在终态（completed/failed/cancelled）释放——waiting 不算终态，是有意设计（parked run 的 checkpoint 挂在共享 thread 上，放行他 run 会污染 resume 点）。裁决规则：本 Run 持车道 → 立即流；车道被他人持有 → 挂起；车道空且本 Run 是 lane 内最早未终态 run → 立即流（防跨 socket 空等流复挂）；否则挂起。挂起项进独立 deferred 队列，消息优先、2s 超时 peek 重探，defer 落地补发 queued 包。保证泵只流「此刻可能产生活动」的 Run，根治排队 Run 流悬挂（其 start 命令无法认领时流会永久占泵）。

**Fence（收据栅栏）**:
safe read execution 处于 started 且 lease 未到期时，对同 call_id 的后续尝试形成的排他阻挡；撞上 fence 的 Command 走 defer 而不是执行。

**Orphan Receipt（孤儿收据）**:
执行者进程死亡后留下的 started execution——无人 settle、lease 无人续期，只能等 lease 到期后被 reconciliation 接管关闭。

**Defer（延迟重试）**:
撞上 fence 时，Command 释放 claim 并把可认领时间推迟到 fence 的 lease 到期（加抖动），不消耗 business attempt；等待超过上限则判定僵局、消耗一次 attempt。

**Reconciliation（对账接管）**:
lease 到期后对未 settle 的 execution 的兜底处理：探测真实结果、标记不可用，或关闭孤儿收据。

**Memory Consolidation Gate（记忆固化门禁）**:
run 成功收尾路径上的运行时强制检测（机制见 ADR 0005）：本 run 有 workspace 写（write_file/edit_file
落到非 `memory/` 路径）且无 memory 写（`memory/` 前缀）时，拦截 finish 意图、注入一轮条件义务式
记忆固化（至多一轮），仍不写则放行并以 `memory_consolidation_skipped` 事件留痕。与纯提示词义务
（D6 Memory Maintenance）是两层：提示词是语义兜底，门禁是运行时保证。

**Memory Consolidation Run（记忆整合 run，已废止）**:
原设计（ADR 0007）：独立 scheduled 整合 run 治理记忆膨胀。方向错误（新架构而非接通既有
循环），已被 ADR-0008 废止，勿再引用其机制。

**Memory Loop Connections（记忆循环接通，P0）**:
把既有三层记忆循环接通的三个动作（机制见 ADR 0008）：B 门禁措辞扩展（run 内教训写入
reflections）、G 心跳收敛（curiosity 待办条目 promote 进 reflections Next Cycle Seeds）、
A 反思注入（reflections 节过滤 + user_profile 注入每 run 上下文，per-agent 开关
`context_inject_reflections_{agent_id}` 灰度）。

**Reflections Injection（反思注入）**:
`build_agent_context` 对 `memory/reflections.md` 的节过滤注入：只取 Insights & Discoveries
全节 + Hypotheses 的 ✅/❌ 结论行（上限 2000 chars），排除 Open Questions / 🔄 / Next Cycle
Seeds（旧待办与心跳信号）；user_profile 全量注入。注入进 dynamic 段（uncached tail），
每 model step ~1.6K token 纯线性成本，不破坏 static 前缀缓存。

**Heartbeat Curiosity Convergence（心跳探索收敛）**:
heartbeat Phase 3 的收敛步：读 `curiosity_journal.md` 的 Follow-up 与 Active Questions，
值得跟进的 promote 进 reflections 的 Next Cycle Seeds（≤3），原条目行尾标 `→promoted`
（不删除）。堵住「探索结果写入 curiosity 后无读取通道」的黑洞；curiosity 降为纯探索日志。

**Dirty Connection（脏连接）**:
SQLAlchemy 池中客户端与服务器端事务状态分裂的连接：服务器端仍在事务中（`idle in transaction`），
客户端却认为连接干净（`_started=False`）。成因是取消落在 asyncpg 懒开始窗口（2.0 方言在首条语句
执行时才发 BEGIN），checkin rollback 因客户端以为无事务而跳过。此后每次 checkout 都在懒开始处抛
`cannot use Connection.transaction() in a manually started transaction` 且连接不被 invalidate，风暴自持
（机制与防护见 ADR 0006）。

**Checkout Probe（检出探针）**:
`database.py` 在 engine checkout 事件上注册的防御：检查 `driver_connection.is_in_transaction()`（客户端
缓存的服务器端事务状态，零网络往返），为真即 raise `DisconnectionError` 让池丢弃该连接并给调用者换
健康连接。对脏连接的检测与自愈与污染成因无关。

## Workspace Publication

Agent 工作区发布的一致性与冲突保护词汇（机制见 ADR 0011 与 workspace-reconciliation 模块）。

**Run Workspace（run 工作区）**:
后端进程内按 run 只物化一次的临时工作区（`use_run_workspace` / `_run_workspace_tasks`）：storage 中
workspace/memory/skills 等路径的文件副本 + manifest（每文件 base_version_token/base_hash 快照）。
execute_code 在副本中执行，flush 时按 manifest token 做 CAS 回写；写过的路径刷新 token，未写路径
token 停留在物化时刻。

**Direct Storage Write（直写）**:
绕过 Run Workspace 直接写 storage 的工具路径——Mutation 工具族（write_file/edit_file/delete_file/
move_file，经 `_execute_workspace_mutation`）与 per-call 物化 + sync_back 工具族（convert_*、
generate_image_*、publish_page 等）。直写会 bump storage 版本而使 Run Workspace 的 manifest 陈旧；
ADR 0011 起直写成功后同步刷新 Run Workspace（刷新钩子只在「本 run 自己的工具执行」上下文触发，
人类编辑与跨 run 写入不触发、仍受冲突保护）。

**Publication Conflict（发布冲突，workspace_sync_conflict）**:
flush 的 CAS 因 storage 版本 ≠ manifest 基版本而失败、且 `_stable_identical_storage_version` 收敛
（第三方内容 == 候选内容）不成立时的终态：第三方内容既非基版本亦非候选版本。恢复路径
（recover_publication → apply_candidate(require_base_match=True)）保留当前 workspace 并返回失败。
冲突后 manifest 不刷新，同路径在本 run 剩余生命期内每次 flush 必冲突——只有进程重启（重新物化）
自愈；直写刷新（ADR 0011）从源头消除同 run 虚假冲突。

_Avoid_: retry（defer 与 attempt 重试是两种机制，勿混用）、recovery、fencing；
Memory Consolidation Gate 勿与 Thread Compact（历史压缩）混用——门禁是收尾注入，压缩是水位触发的历史替换。
Dirty Connection 勿与断连（disconnect）混用——断连是物理连接失效，脏连接是逻辑状态分裂且物理上完全
健康；`pool_pre_ping` 只测断连，对脏连接无效，勿当防护手段。
Publication Conflict 勿与 Workspace Lock（Redis 短锁）混用——锁串行化写时点，CAS 串行化版本演进；
直写刷新只覆盖「本 run 自己」的写，人类编辑/跨 run 写仍走冲突保护（宁可失败不覆盖）。

## Deployment Coordination

多会话共享同一宿主/仓库/compose project 时的部署协作词汇（机制见 ADR 0003）。

**Deploy Lock（部署锁）**:
仓库内 `.clawith-deploy/deploy.lock` 上的全局排他 fcntl 内核锁，串行化所有部署/回滚（deploy.sh、restart.sh docker 分支）。持有者进程死亡即自动释放；等待超时（默认 600s）或 `--no-wait` 时以固定退出码 9 失败。

**Deploy Registry（部署注册表）**:
`.clawith-deploy/registry.json`：记录当前持锁者（active）与最近 20 次部署（commit、镜像 sha、scope、成败）。部署前以「最近一次部署 commit → 目标 commit」的 git log 区间做 tip 对比，让部署者看见自己在带上/落下哪些提交。

**Deploy Avoidance（部署避让）**:
多会话下防止三类碰撞的协作纪律：A 同时部署（靠锁）、B 部署内容与分支 tip 错位（靠注册表 tip 对比 + `--strict` 阻塞）、C 共享 index 的提交窗口竞态（无法机制化，仅协议：提交前 `git diff --cached --stat` 复核、只用 pathspec 提交本任务文件）。

_Avoid_: 锁≠注册表（一个串行化时机，一个提供信息）；Deploy Lock 勿与 Runtime 的 Lease/Fence 混用（前者在宿主部署层，后者在运行时工具执行层）。

## Observability

**Native Score（第一方评分）**:
应用在 run 终态自己记录的业务事实评分——结局（succeeded/failed/cancelled）、重试次数、成本快照——挂在 run 根 trace 上；与 evaluator（CODE evaluator / judge）事后推断的评分相对：前者是第一方事实，后者是平台推断，口径分轨不混用。

**Implicit Negative Signal（隐式负反馈信号）**:
可判定的用户不满信号，窄口径三类：显式取消/打断、同目标（goal）重复发起、否定/纠正后重试。中性信号（多 run 共 thread 的继续对话）不算负反馈——宽口径会把继续对话污染成失败率虚高，并污染 judge 校准基准。

**Release Tag（部署版本标签）**:
trace 上标记本次部署的 git commit 版本，供看板、告警、实验按部署对比与指标突变归因。语义版本（如 v2.1）不适用：平台无发布节奏，commit hash 才是部署事实。

_Avoid_: Native Score 勿与 evaluator score 混用（应用写事实、平台做推断，两者同源会失去交叉校验价值）；Implicit Negative Signal 勿把「继续下一步」当负反馈；Release Tag 勿用语义版本。

## Loop Breaker（循环熔断）

运行时兜底「模型原地空转」的熔断词汇（机制见 ADR-0016 与方案 20260905-agent-no-progress-detection-plan）。

**Evidence Gain（证据增益）**:
判定一轮工具执行「是否让任务/世界状态前进」的确定性计分，替代签名匹配。判定键落在客观副作用上：read_file 新 (path, content_hash) +1、execute_code 结果哈希变化 +1 / 失败→成功 +2、write 真实变更 +3、external_write +3；重复读/重复命令/空编辑 = 0。与「签名级熔断」（判定动作是否相同）相对——后者抓不住「换了姿势做同一件事」（git status/branch/checkout/fetch + read_file 混杂，参数每次不同）。

**Material Progress（材料进度）**:
Clawith 特有、比通用证据增益更根本的信号：workspace 实际变更。`workspace_file_revisions` 只在 before≠after 时落行（`record_revision` 空编辑返回 None），所以「写是否真实变更」有客观证据；git 巡检类命令永不产生 revision → 天然零材料进度。

**No-Progress Ladder（零增益阶梯）**:
连续零增益轮数（streak）触发的分级干预 nudge(3)/pivot(5)/stop(8)，对齐 `_SUCCESS_LOOP_THRESHOLD=5` 与 DeepCode 3/5/8 口径。stop 档注入「停止探索、交最终答案」并放行 finish，非硬 terminate。

**Look-only Cap（纯观察上限）**:
连续 N=6 轮「无任何真实写变更」后，即使后续轮读到新文件/命令输出变化也强制该轮增益归零——封死「逐文件读遍」与「无限发明新命令串」两类游走（Reasonix `explorationRunLimit` 的移植，Clawith 靠材料进度信号使其更严）。

**ScoreRound（增益计分器）**:
`no_progress.py` 的纯函数核心：从 ledger（effect/status/sanitized_arguments/result_metadata）+ material_change（接线层把 WorkspaceFileRevision before≠after 解析成布尔）重放计算每轮增益，零新 checkpoint 状态，per-turn 重算。

_Avoid_: Evidence Gain 勿与签名熔断混用（一个问「前进没有」，一个问「动作相同没有」，前者是后者盲区的补集而非替代——`_trailing_identical_calls` 仍兜重复执行本身）；No-Progress Ladder 勿与 Thread Compact（历史压缩）混用——阶梯是零增益干预，压缩是水位触发的历史替换；Material Progress 勿把 external_write 当可验（外部副作用无 workspace 证据，只能假定前进）。
