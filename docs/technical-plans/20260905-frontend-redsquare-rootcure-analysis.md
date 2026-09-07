# 前端「执行中/红色方块」误显——根治方案（最佳方案已选定）

- 日期：2026-09-05
- 关联 run：`14ba5535`（Android 工程师 07，~226 次 read 重读）
- 方法：按 [[plans-compare-reference-materials]] 工作法，对比 reference-projects 完整清单中「流式渲染 / 事件流 / 状态机」域源码后选定
- 现状：最小补丁 `settleRunningTools` 已落地（未提交）。本文件给出**选定后的最佳根治方案**，替代前版「方向清单」。

---

## 一、问题定性（已确认根因）

- 现象：任务已完成，WebUI 仍显示红色方块（`analysis-trace--stopped`，`index.css:5951-5956`）。
- 触发（`AgentDetailPage.tsx:1244-1246`）：

  ```ts
  const hasRunningTool = toolItems.some(tc => tc.status === 'running');
  const stopped = hasRunningTool && chatActive === false; // chatActive = isWaiting || isStreaming
  ```

- 后端数据始终正确（`agent_run_events` 成对 running/done；`chat_messages` 全部 `status='done'`）。
- 前端链路：终态刷新 `refreshSessionMessages` 抓 `limit=20` 规范消息 + 本地流式缓存 `mergeSessionToolMessages` 合并；卡住工具的 done 行落在 20 条窗口外 → running 被当**新 running 工具追加** → 轮询因 `active_run=null` 停止 → 永久残留。

- **深层根因**：前端用「消息里 running 工具的散落状态」去**反推**「是否还在执行」，而这两者之间隔着一个非幂等、有损的合并。参考项目的一致范式恰恰相反：**「是否还在执行」由权威 run 状态驱动，工具视觉态只是它的投影，且数据层单调收敛、幂等**。

---

## 二、参考项目范式（读源码结论）

| 参考项目 | 已读源码 | 范式 |
|---|---|---|
| software-agent-sdk（OpenHands V1） | `event/base.py`、`conversation/state.py`、`event_store.py`、`event_service.py`、`event_router.py` | 事件单一事实源（`Event` 不可变/按 id 幂等）；`ConversationExecutionStatus.is_terminal()` 显式终态；`subscribe_to_events` **订阅即推当前快照**；`/events/search` 游标分页 |
| OpenHands 前端 | `stores/conversation-state-store.ts`、`utils/status.ts`、`contexts/conversation-websocket-context.tsx` | Zustand `executionStatusByConversation` 单一事实源；`isExecutionActive(status)` 全量从单一 status 派生，**从不扫消息**；重连重放靠 `event.id` 去重并跳过非幂等副作用（#1656） |
| agent-chat-ui | `providers/Stream.tsx`、`Thread.tsx`、`lib/ensure-tool-responses.ts` | `useStream` SDK 托管全部流状态机 + `fetchStateHistory:true` 全量重放，前端**零手写合并** |
| deepagents（T0 同栈） | 记忆：`backends/StateBackend` 把状态放 graph state channel 随 checkpoint 持久 | 「后端持状态、前端只读投影」 |

共同范式一句话：**单一事实源 + 显式终态 + 按 id 幂等 + 订阅即推快照；UI 的「是否在跑」从权威状态派生，绝不从有损消息窗口反推。**

---

## 三、最佳方案（选定）：run 权威状态门控 + 数据层单调收敛

### 3.1 核心判断

Clawith 后端**已经**是权威且正确的：`/runtime-state` 的 `active_run`（终态为 null）+ WS `run_completed` 终态包。问题全在前端——它没用这个权威信号去门控工具 running 态，反而用了有损合并后的消息态。

因此最佳方案**不需要任何后端/协议改造**（方向 C「cursor 全量重放」是为历史分页准备的长期优化，不是本 bug 的根治条件，属过度设计，明确降级）。根治 = **把「是否还在执行」的判定源切到权威 run 状态，并让工具数据态单调收敛到它**。

### 3.2 两层改动

**第 1 层 · 渲染投影（根治核心，~5 行）**

`AnalysisCard` 的 `hasRunningTool` 只有在 run 仍 active 时才把 running 计入：

```ts
// AnalysisCard props 增传 runActive（由 selectedSessionActiveRun 派生）
const runActive = selectedSessionActiveRun !== null
    && ['queued', 'running', 'waiting_user'].includes(selectedSessionActiveRun.status);
const hasRunningTool = runActive && toolItems.some(tc => tc.status === 'running');
const stopped = hasRunningTool && chatActive === false;
```

这一行让「run 已终态时 running 工具残留」在**结构上不可能**触发红色方块：即使数据层某条路径漏 settle，`runActive=false` → `hasRunningTool=false` → `stopped=false`。一条 invariant 覆盖所有终态路径，是「打地鼠式 settle」做不到的根治。

**第 2 层 · 数据收敛（保证数据终态一致，已有 `settleRunningTools` 扩到全部终态入口）**

- 终态时 settle 所有 running 工具（`settleRunningTools`），挂在 `refreshSessionMessages` 终态分支内，**两条终态信号源都已汇到这里**：轮询 `runtimeCompletionNeedsMessageRefresh`（active_run→null，2605）与 WS `done` 包（`runtimeTerminalPacketNeedsMessageRefresh` 3674 → 3751 `refreshSessionMessages(true)`，注释「always reload the canonical page after a terminal packet」）。
- done 永不回退 running（`mergeSessionToolMessage` line 87 已有 guard，保留并契约测试锁死）。

**第 3 层 · 加固（可选，WS 断流防线）**

running 工具带 fail-closed 租约：终态或 `chatActive===false` 超时自动降级 done。作为 WS 丢包的兜底，非根治必需。

### 3.3 为什么这是最佳（对比取舍）

| 候选 | 判定 |
|---|---|
| 只 settle（最小补丁） | 症状兜底，每漏一条终态路径就复发一次，不够根治 |
| **run 状态门控（本方案）** | ✅ 结构性消除：一条 invariant 覆盖所有路径，零后端改动，~5 行 |
| cursor 全量重放（方向 C） | 架构终局正确，但为「历史分页」设计，非本 bug 根治条件，过度设计 |
| 保留 `analysis-trace--stopped` | ✅ 本方案不误杀：真正的「WS 断流 + run 仍 active」仍会红方块告警（合法诊断价值保留） |

---

## 四、落地步骤

1. **渲染投影**：`AnalysisCard` 增 `runActive` 入参（由 `selectedSessionActiveRun` 派生，`selectedSessionActiveRun` 在 7195 渲染作用域已可用），`hasRunningTool` 加 `runActive &&` 门控。
2. **数据收敛（已完成）**：`settleRunningTools` 挂在 `refreshSessionMessages` 终态分支，两条终态源（轮询 active_run→null、WS done 包）都汇到这里，无需再扩。（2026-09-06 复核：done handler 本就调用 `refreshSessionMessages(true)`，此前「需补」判断不准确。）
3. **契约测试**：①终态 + 窗口外 running → 全 settle；②done 永不回退 running；③重连重放重复 call_id 幂等无副作用；④`stopped` 只在「run 显式终态」+ 数据层漏 settle 的瞬间出现（投影门控后应为不可能，锁死）。
4. **（可选）** fail-closed 租约。

---

## 五、结论

- 最佳根治 = **run 权威状态门控（渲染投影）+ 数据层单调收敛**，方向 A+B+D 合一，零后端改动。
- 参考项目（software-agent-sdk / OpenHands / agent-chat-ui / deepagents）范式完全一致：单一事实源 + 显式终态 + 幂等，UI 从权威状态派生、不扫有损消息窗口。
- 方向 C（cursor 全量重放）为长期架构优化，本 bug 不需，避免过度设计。
