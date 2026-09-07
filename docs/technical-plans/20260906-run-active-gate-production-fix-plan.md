# 生产级修复方案：run-active 渲染投影门控（production-fix-plan）

- 日期：2026-09-06
- 变更：前端 `AnalysisCard` 的红色方块（`analysis-trace--stopped`）判定源从「消息里 running 工具」改为「权威 run 状态」驱动
- 前置：`settleRunningTools`（84f4b505）已闭环已复现路径；本方案是其之上的结构性加固
- 评审裁决：**通过**（9/9）

---

## Phase 1 · 参考对比（≥10 项目）

决策点：**UI 如何判定「agent 是否还在运行」而不依赖过期的消息态**。

| # | 项目 | 结论 | 来源 |
|---|---|---|---|
| 1 | OpenHands 前端 | `utils/status.ts` `isExecutionActive(status)` 从单一 `execution_status` 派生，**绝不扫消息**；`conversation-state-store` Zustand 单一 store；重连重放靠 `event.id` 去重 + 跳过非幂等副作用(#1656) | 读源码 |
| 2 | software-agent-sdk（OpenHands V1） | `ConversationExecutionStatus.is_terminal()`(FINISHED/ERROR/STUCK) 显式终态；`subscribe_to_events` 订阅即推快照 | 读源码 |
| 3 | agent-chat-ui（langchain-ai） | `useStream` SDK 托管全流状态机 + `fetchStateHistory:true`，前端零手写合并 | 读源码 |
| 4 | **open-agents（vercel）** | `tool-state.ts` `extractRenderState` 显式区分 `running` vs `interrupted`(流停仍 running)；`stream-recovery-policy` 4s stall + 探针式向服务器求证「workflow 是否还在跑」，**不信任本地态** | 读源码（最直接对标） |
| 5 | open-canvas | `streamWorker.ts` 单一 stream + 显式 `done` 事件终结循环 | 读源码 |
| 6 | dify | `workflow/run/status.tsx` run `status` 是一等 prop（来自 run 服务），`StatusDot` 直渲，不从消息反推 | 读源码 |
| 7 | deepagents（T0 同栈） | `backends/StateBackend` 状态入 graph state channel 随 checkpoint 持久：后端持状态、前端只读投影 | 记忆 |
| 8 | langgraph-sdk / langgraphjs | `useStream` 状态机本体（#3 委托对象）；本地 clone 无独立 useStream 文件，机制经 #3 用法体现 | 诚实负结论 |
| 9 | letta-code | `StatusMessage.tsx`；stateful harness 但 CLI 为主 | 诚实负结论（无 WebUI 状态机） |
| 10 | A2A / ACP / agent-protocol | 协议层 TaskState 定义显式终态 | 诚实负结论（协议非 UI） |
| 11 | CLI 系（gemini-cli/codex/SWE-agent/gptme） | 无 WebUI 状态误显问题；工具状态=事件流建模与 #2 一致 | 诚实负结论（无 WebUI） |

**合成**：open-agents 是「吸烟枪」——显式区分 running/interrupted 且用探针向服务器求证。Clawith 红色方块 = open-agents 的 `interrupted`，应由权威状态驱动。Clawith 已有探针等价物（1500ms `fetchSessionRuntimeState`→`active_run`），问题是前端反用本地 running 工具态推断 run 态。

## Phase 2 · 双源定根因

- **代码源**（read_file 核对，2026-09-06）：`AgentDetailPage.tsx:1244-1246` `hasRunningTool = toolItems.some(tc => tc.status === 'running')` + `stopped = hasRunningTool && chatActive === false`；`chatActive = isWaiting || isStreaming`（7203）。
- **日志/DB 源**（前会话取证）：nginx `messages?limit=20`→200、`runtime-state`→200(`active_run:null`)；DB `agent_run_events` 160 行成对 running/done、`chat_messages` 全 `status='done'`。后端始终正确。
- **最深层因**：判定源反了——「是否还在执行」应由权威 run 状态（`active_run`）驱动，工具 running 视觉态只是它的投影；现实现反用有损合并后的消息态反推，故终态残留 running 工具即误显。

## Phase 3 · 修复方案

- `sessionRuntimeState.ts`：新增 `runIsActive(activeRun)`（权威谓词，复用 `SessionActiveRun`）+ 统一 `TERMINAL_RUN_STATUSES`（收编 3 处重复的 `['completed','failed','cancelled']` 字面量，行为不变）。
- `AgentDetailPage.tsx`：`AnalysisCard` 增 `runActive: boolean` 必填 prop，`hasRunningTool = runActive && toolItems.some(...)`；渲染处 `runActive={runIsActive(selectedSessionActiveRun)}`（唯一调用点 7195）。
- 契约测试：①`runIsActive` 谓词（null/终态→false，queued/running/waiting_user→true）；②源码级断言门控已接线。
- **影响面**：仅会话详情页 tool 卡片视觉态；纯前端谓词；无后端/契约变更。爆炸半径 = 单组件渲染分支。

## Phase 4 · 9 角度评审

1. **根因正确** ✅ 判定源反了，解释全部症状（红方块=消息态 stale+chatActive false；后端正确=消息态是前端衍生）。追至最深层（为何 settle 之外还需门控）。
2. **方案正确** ✅ 改的正是判定源。删掉方案→settle 漏路径时红方块复发→非止痛药。
3. **资料正确** ✅ 读真实源码，同类问题非 false friend，无归档项目当第一依据。
4. **无副作用** ✅ 无外部写/缓存/连接影响；`runActive` 只窄化视觉态。
5. **不破坏他逻辑** ✅ 唯一调用点已传参；tsc 0、133 测试过、eslint 0；render-site `isGroupRunning`（7187 原始判定）不受影响。无后端改动，arch-guard 不适用。
6. **最佳方案** ✅ 枚举 ①只 settle（症状兜底，已做）②本方案（结构性 invariant）③cursor 全量重放（过度），取 Ponytail 阶梯第二档。
7. **非多余** ✅ 定性 P2 防御纵深（settle 已闭环 P0）：根因（判定源反）客观存在，门控把「红色方块由 run 状态驱动」变为显式 invariant。
8. **复用既有逻辑** ✅ 统一 3 处终态字面量；`runIsActive` 复用既有类型与 predicate 风格。
9. **不破坏 Clawith 特性** ✅ C1–C6 全过；不动 durable run/checkpoint、多租户、exactly-once、前缀缓存、WS 状态机、飞书通道。

**裁决：通过。**
