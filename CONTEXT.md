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

_Avoid_: retry（defer 与 attempt 重试是两种机制，勿混用）、recovery、fencing
