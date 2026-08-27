# Clawith Runtime Context

Clawith 多租户企业 Agent 平台的运行时（Runtime）词汇表：LangGraph 图执行、命令队列与工具执行账本的核心术语。本文件只收录本项目独有的概念，不含通用编程术语。

## Language

**Command**:
一条可持久化的运行控制消息（start / resume / cancel），由 daemon 认领并驱动一次图推进；认领、尝试次数、应用状态全部落库，进程死亡后可由其他 daemon 恢复处理。

**Claim（认领）**:
daemon 对 Command 的带 TTL 排他占用；持有期间心跳续期，到期视为持有者死亡、可被重新认领。

**Tool Execution（工具执行账本行）**:
一次精确工具调用的持久化收据：记录调用身份、attempt、状态（started/succeeded/failed/unknown）、lease 与结果。同 call_id 的重复调用被账本识别并拒绝重放。

**Lease（执行租约）**:
Tool Execution 处于 started 时的排他执行窗口；持有者定期续期，到期视为执行者死亡。safe read 的自动重试与孤儿收据的接管都以 lease 为界。

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

_Avoid_: retry（defer 与 attempt 重试是两种机制，勿混用）、recovery、fencing；
Memory Consolidation Gate 勿与 Thread Compact（历史压缩）混用——门禁是收尾注入，压缩是水位触发的历史替换。

## Deployment Coordination

多会话共享同一宿主/仓库/compose project 时的部署协作词汇（机制见 ADR 0003）。

**Deploy Lock（部署锁）**:
仓库内 `.clawith-deploy/deploy.lock` 上的全局排他 fcntl 内核锁，串行化所有部署/回滚（deploy.sh、restart.sh docker 分支）。持有者进程死亡即自动释放；等待超时（默认 600s）或 `--no-wait` 时以固定退出码 9 失败。

**Deploy Registry（部署注册表）**:
`.clawith-deploy/registry.json`：记录当前持锁者（active）与最近 20 次部署（commit、镜像 sha、scope、成败）。部署前以「最近一次部署 commit → 目标 commit」的 git log 区间做 tip 对比，让部署者看见自己在带上/落下哪些提交。

**Deploy Avoidance（部署避让）**:
多会话下防止三类碰撞的协作纪律：A 同时部署（靠锁）、B 部署内容与分支 tip 错位（靠注册表 tip 对比 + `--strict` 阻塞）、C 共享 index 的提交窗口竞态（无法机制化，仅协议：提交前 `git diff --cached --stat` 复核、只用 pathspec 提交本任务文件）。

_Avoid_: 锁≠注册表（一个串行化时机，一个提供信息）；Deploy Lock 勿与 Runtime 的 Lease/Fence 混用（前者在宿主部署层，后者在运行时工具执行层）。
