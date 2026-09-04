# Agent Inbox HITL 审批 UX 源码研究报告

日期：2026-09-03
状态：**完成**（分析基于本地仓库 `/Users/shubinzhang/Documents/UGit/agent-inbox` HEAD `aa46c77b32be321519e7b162110b3b7e5e77e4d7`，`git rev-parse origin/main` 相等，已同步；工作区干净）
定位：参考资料研究，非实现方案。对照 Clawith `approval_requests` 审批流（`ApprovalRequest` 表 + `AutonomyService` + 前端 `ApprovalsTab` + 飞书卡片三终态）。

## 0. 项目概览

- **是什么**：LangChain 官方开源的「human-in-the-loop 审批 inbox」前端（`langchain-ai/agent-inbox`，作者 Brace Sproul）。它是 LangGraph `interrupt()` 中断的**通用审批 UI**，通过 `@langchain/langgraph-sdk` 直连任意 LangGraph 部署（本地 `langgraph dev` 或 LangGraph Platform），把「被 interrupt 挂起的 thread」列成列表，人工批准/拒绝/编辑/回复后 resume 原 run。
- **技术栈**：Next.js 16.3.0（App Router）+ React 19 + TypeScript，无自建后端。UI 用 shadcn/ui（Radix）+ Tailwind + framer-motion；数据层只有 `@langchain/langgraph-sdk@1.9.25`（`package.json`）。核心代码约 30 个文件，集中在 `src/components/agent-inbox/`。
- **入口**：`src/app/page.tsx:6-12` 渲染 `<AgentInbox />`；`src/app/layout.tsx:33-51` 用 `ThreadsProvider` 包裹全局。所有配置（graph id / deployment URL / LangSmith API key）存浏览器 `localStorage`，**无用户体系、无服务端鉴权、无审计**——这是它作为「参考 UI」的定位与最大局限（见 §8）。
- **与 Clawith 的对标关系**：Clawith 的审批是「表模型 + 后端 service + 前端 tab + 飞书卡片」的完整闭环（已自研）；agent-inbox 是「零后端、SDK 直连、靠中断 schema 契约驱动」的极简通用参考。只看**交互契约与前端状态建模**，不做实现移植。

## 1. 中断协议：`HumanInterrupt` / `HumanResponse` 契约（全库核心）

这是 agent-inbox 最值得借鉴的部分——把「一个审批能做什么」编码进中断对象本身，而不是写在 UI 里。

`src/components/agent-inbox/types.ts`：
- `HumanInterruptConfig`（8-13）：`allow_ignore` / `allow_respond` / `allow_edit` / `allow_accept` 四个布尔开关，**由 agent 侧在调用 `interrupt()` 时声明**，决定 UI 渲染哪些动作。
- `ActionRequest`（19-22）：`{action: string, args: Record<string, any>}`——`action` 是中断标题（如工具名），`args` 是待审的参数。
- `HumanInterrupt`（29-33）：`{action_request, config, description}`，`description` 可含 markdown，作为审批上下文渲染（`README.md:131-132`）。
- `HumanResponse`（39-42）：`type: "accept" | "ignore" | "response" | "edit"` + `args: null | string | ActionRequest`。

四动作语义（`README.md:110-118`）：
- `accept`：原样回传 `action_request`（args 全部字符串化），即「照原参数执行」。
- `edit`：回传**用户改过的** `action_request`（args 字符串化 + 用户编辑）。
- `response`：回传一个字符串，供 agent 继续对话。
- `ignore`：回传 `null`，什么都不做。

响应始终是 `HumanResponse[]`（列表），当前只回单个对象，agent 侧 `interrupt(request)[0]` 取回（`README.md:119,171`）。

**对 Clawith 的启示**：Clawith 的 `ApprovalRequest`（`backend/app/models/audit.py:31-47`）只有 `action_type` + `status(pending/approved/rejected)`，是二元批准/拒绝；agent-inbox 把「编辑后批准 / 回复文本 / 忽略」作为**中断实例级可选项**下沉到数据里，UI 纯配置驱动。

## 2. 完整数据流：interrupt → 列表 → 审批 → resume

### 2.1 建 client（`src/lib/client.ts:3-16`）
`new Client({ apiUrl: deploymentUrl, defaultHeaders: { "x-api-key": langchainApiKey } })`——一个 inbox 一个 SDK client，直连部署。

### 2.2 拉中断列表（`src/components/agent-inbox/contexts/ThreadContext.tsx`）
- `fetchThreads`（156-306）：`client.threads.search({offset, limit, status, metadata})`（208）。按 tab 过滤：`interrupted` 传 `status:"interrupted"`，`all` 不传（194-197）。
- 按 graph/assistant 隔离：`getThreadFilterMetadata`（`contexts/utils.ts:324-339`）——`graphId` 是 UUID 就按 `assistant_id` 过滤，否则按 `graph_id` 过滤。
- **中断解析**是重头戏：`getInterruptFromThread`（`contexts/utils.ts:60-263`）要兼容 LangGraph 多种 interrupt 结构（嵌套数组 `[0][1].value`、HITL middleware `action_requests`+`review_configs`、JSON 字符串），解析失败统一降级为 `IMPROPER_SCHEMA`（`constants.ts:12`）。`normalizeInterrupt`（12-46）专门把 HITL middleware 的 `{action_requests:[{name,arguments}], review_configs:[{allowed_decisions}]}` 映射回标准契约（`approve→allow_accept`、`edit→allow_edit`、`reject→allow_respond`）。
- 排序：按 `thread.created_at` 倒序（291-296）；分页上限 100/页（182-191）。

### 2.3 resume（`ThreadContext.tsx:411-461`）
`sendHumanResponse` 是唯一写路径：非流式 `client.runs.create(threadId, graphId, { command: { resume: response } })`（452-456）；流式 `client.runs.stream(..., { command: { resume: response }, streamMode: "events" })`（445-450）。`command.resume` 直接回填到 agent 侧 `interrupt()` 的返回值，agent 只需读返回值分派 accept/edit/response/ignore。

### 2.4 拒绝并终止（`ThreadContext.tsx:377-409`）
`ignoreThread` 走 `client.threads.updateState(threadId, { values: null, asNode: END })`（387-390）——直接把线程标记到 `END`，**不 resume**。用于「无效 schema 的强制忽略」和「Mark as Resolved」。

## 3. 审批动作编排（`use-interrupted-actions.tsx`）

`useInterruptedActions`（68-419）是审批交互的状态机核心：
- 初始化（97-121）：调 `createDefaultHumanResponse`（`utils.ts:148-252`）按 config 生成默认响应集 + 默认提交类型（优先级 `accept > response > edit`）。
- `handleSubmit`（123-313）：把编辑态扁平化回 `HumanResponse[]`（158-190）后流式提交；边 stream 边从 `on_chain_start` 事件取 `metadata.langgraph_node` 更新 `currentNode`（226-231），`event === "error"` 则 toast + `currentNode="__error__"`（233-255）；成功后 `fetchSingleThread`，仍 `interrupted` 则留详情页（支持**同一 run 多轮中断**），否则回列表（286-300）。
- `handleIgnore`（315-358）：发 `[{type:"ignore",args:null}]` 后刷新；`handleResolve`（360-392）调 `ignoreThread`（updateState 到 END）。

`createDefaultHumanResponse`（`utils.ts:148-252`）是关键决策表：`allow_edit && allow_accept` → 编辑框默认「Accept」；`allow_respond` → 追加文本框；`allow_ignore` → 追加 ignore 项；缺 accept/ignore 时补一个（238-249）。`haveArgsChanged`（254-270）判断用户是否真改了参数，决定提交 `edit` 还是回退 `accept`。

## 4. UI 组件结构与状态管理

- **路由即状态**：列表/详情切换、tab、分页全靠 URL query params（`use-query-params.tsx:4-64`）：`agent_inbox`、`inbox`(状态 tab)、`offset`、`limit`、`view_state_thread_id`。`index.tsx:147-156` 用 `view_state_thread_id` 有无在 `<ThreadView>` / `<AgentInboxView>` 间切换——**详情页可深链、可回退、刷新保持**。
- **状态管理**：单一 React Context（`ThreadsProvider`/`useThreadsContext`，`ThreadContext.tsx:113-488`），持有 `threadData[]` + `agentInboxes` + 操作方法，无 Redux/zustand；数据全在内存，每次 URL 变化重取（138-154 的 effect）。
- **列表**：`inbox-view.tsx:21-229` 按 tab 过滤（`All/Interrupted/Idle/Busy/Error`，`inbox-buttons.tsx:58-94`），带滚动位置保存/恢复（`use-scroll-position.tsx`）。
- **列表项**：`inbox-item.tsx:19-111` 按 `ThreadData` 判别联合路由到 `InterruptedInboxItem`（`interrupted-inbox-item.tsx:18-117`：标题=action、描述截 65 字符、时间戳）或 `GenericInboxItem`（只显示 Thread ID + Studio 按钮）。
- **详情**：`thread-view.tsx:11-129` 左 `ThreadActionsView` + 右 `StateView`（description/state 两 tab）。
- **审批表单**：`inbox-item-input.tsx:316-560`，三个子组件：`EditAndOrAcceptComponent`（182-314，args 逐字段 textarea 编辑 + Reset）、`ResponseComponent`（86-152，文本框，Ctrl/Cmd+Enter 提交）、`AcceptComponent`（154-180）。
- **状态徽章**：`statuses.tsx:5-64` `InboxItemStatuses`——config 只有 `allow_ignore` 时灰 "Ignore"，否则绿 "Requires Action"（20-24）。
- **任意 JSON 状态渲染**：`state-view.tsx` 的 `StateViewRecursive`（82-162）递归渲染嵌套 JSON，`MessagesRenderer`（47-80）专门渲染 `BaseMessage[]` + `ToolCallTable`。

## 5. 多 inbox / 多用户协作与部署形态

- **多 inbox**：`use-inboxes.tsx`（31-468）管理 `AgentInbox` 数组（`types.ts:152-178`：`id/graphId/deploymentUrl/name/selected/tenantId/createdAt`），全存 `localStorage`（key `inbox:agent_inboxes`）。一个页面可挂多个 graph/部署，侧栏切换（`app-sidebar/index.tsx:78-127`）。
- **多租户**：靠每个 inbox 的 `deploymentUrl` 直连不同部署 + `graph_id/assistant_id` metadata 过滤隔离（`contexts/utils.ts:324-339`）。
- **部署形态**：`add-agent-inbox-dialog.tsx:69-167`——deployed graph 时请求部署 `/info` 拿 `project_id/tenant_id`，inbox ID 变成 `project_id:graphId`（105-109）；本地 graph 用 UUID。`utils/backfill.ts:86-161` 是一次性 ID 迁移逻辑（老 UUID → `project_id:graphId`）。
- **多用户协作模型（关键局限）**：agent-inbox **无 claim/lease/锁**——多个用户各自浏览器指向同一部署、看到同一批 thread，谁都能 approve/reject，无并发冲突检测、无「谁在处理」标识。这与 Clawith 的 `resolved_by` + creator/platform_admin 权限校验（`autonomy_service.py:182-186`）是最本质差距。

## 6. 错误处理与中断恢复

- **无效 schema 降级**：`IMPROPER_SCHEMA`（`constants.ts:12`）——中断解析失败时 `getInterruptFromThread` 返回 `allow_ignore=true` 的伪中断，详情页显示黄色「required action data is missing」+ 只允许 Ignore（`thread-actions-view.tsx:262-329`）。
- **流式错误**：`use-interrupted-actions.tsx` 内 `chunk.event === "error"` → toast + `currentNode="__error__"`（233-255），`inbox-item-input.tsx:547-552` 渲染红色 "Error occurred"。
- **多轮中断恢复**：提交后 `fetchSingleThread`，仍 `interrupted` 则停留详情（`use-interrupted-actions.tsx:286-300`），支持一个 run 内多次 `interrupt()`。
- **状态兜底**：`threads.search` 拿不到中断时回退 `client.threads.getState()` 再 `processThreadWithoutInterrupts`（`ThreadContext.tsx:240-274`）。
- **自动刷新**：agent-inbox 无轮询，靠 URL 变化触发 refetch；Clawith `ApprovalsTab` 用 15s 轮询（`ApprovalsTab.tsx:14`）——两条路线可对照。

## 7. 可迁移点 → Clawith 映射

| # | agent-inbox 机制（文件） | Clawith 对标点 | 可借鉴要点 |
|---|---|---|---|
| 1 | `HumanInterruptConfig` 四开关 + 四动作响应（`types.ts:8-13`、`39-42`） | `ApprovalRequest.action_type`+`status` 二元 approve/reject（`audit.py:31-47`） | 把「是否允许编辑/回复/忽略」下沉为**每个审批实例的声明**，UI 纯配置驱动；审批类型从批准/拒绝扩展到「编辑后批准 / 回复文本 / 忽略」 |
| 2 | 判别联合 `ThreadData` + `invalidSchema` 降级（`types.ts:92-137`、`constants.ts:12`） | 飞书卡片三终态 `NO_REPLY→withdraw / failed→fallback_error / cancelled→abort`（`checkpoint_side_effects.py:260-279`） | 前端显式建模「中断数据解析失败」态（只允许 Ignore + 黄色告警），不崩溃、不给错误按钮——Clawith 网页端审批卡片可复用该降级思路 |
| 3 | resume 走 `client.runs.create/stream` 的 `command.resume`（`ThreadContext.tsx:411-461`） | `AutonomyService.resolve_approval` 走 `RuntimeCommandIntake.resume_run`（`autonomy_service.py:203-234`） | 两者同构「审批→resume 原 run」；agent-inbox 靠 LangGraph 原生 resume，Clawith 用 `idempotency_key=f"approval:{id}:{status}"` 防重，各有可取 |
| 4 | `ignoreThread` = `updateState({values:null, asNode:END})`（`ThreadContext.tsx:377-409`） | reject 后 resume 一个「不执行」payload（`autonomy_service.py:219-229`） | 「拒绝并终止」两种实现：直接 END 干净但丢后续节点；resume 后分支判断可保留图逻辑。按场景选 |
| 5 | 流式提交 + `currentNode` 进度反馈（`use-interrupted-actions.tsx:226-255`） | 飞书流式卡片 `_build_streaming_skeleton`（`card_stream_bridge.tsx:606-663`） | 审批提交后给「图跑到哪个节点」的实时反馈，而非盲等——网页端 ApprovalsTab 目前无此反馈 |
| 6 | 全 URL query params 驱动列表/详情/分页（`use-query-params.tsx`、`index.tsx:147-156`） | `ApprovalsTab` 内嵌 agent-detail tab（`ApprovalsTab.tsx:6-143`） | 审批列表可深链、可浏览器回退、刷新保持；Clawith 若做独立审批中心页可借鉴 |
| 7 | `createDefaultHumanResponse` 决策表 + `haveArgsChanged`（`utils.ts:148-270`） | 无对标 | 「默认提交类型优先级 accept>response>edit」+「是否真改了参数」判定，是编辑型审批的正确性关键，可直接借鉴到 Clawith 审批表单 |
| 8 | 通用任意 JSON 状态渲染 `StateViewRecursive` + `MessagesRenderer`（`state-view.tsx:47-162`） | `ApprovalsTab` 只 `JSON.stringify` details（`ApprovalsTab.tsx:78-82`） | 审批详情 `details` 字段可折叠、可渲染消息/tool_call 的通用组件，提升审批人理解上下文的效率 |

## 8. 局限（诚实记录）

- **零后端、零鉴权**：无用户体系、无审批人身份校验、无审计，任何拿到 deployment URL + API key 的人都能审批；与 Clawith 多租户 + `resolved_by`/creator 权限 + `AuditLog`（`audit.py:13-28`）不可同日而语，agent-inbox 只能当「单机参考 UI」。
- **无并发协作模型**：无 claim/lease/锁，多用户同时操作同一 thread 会互相覆盖；Clawith 的 `approval_requests` 表 + `status != "pending"` 拒绝二次处理（`autonomy_service.py:179-180`）反而更稳。
- **数据全在浏览器 localStorage**：inbox 配置、API key 明文存本地（`constants.ts:2-3`、`settings-popover.tsx`），不适合多租户生产。
- **TS/Next 技术栈不通**，只取设计契约（中断 schema、响应状态机、无效 schema 降级）与边界条件，不做代码移植。
- **中断解析兼容层过重**（`getInterruptFromThread` 200+ 行 case 分支）：LangGraph interrupt 数据结构不稳定的代价；Clawith 自家控制 `interrupt()` 载荷则不需要。
- 本次未深入：`@assistant-ui` 组件库定制、`convert_messages.ts`/`cookies.ts`（Supabase 残留）、CI 仅有 format/lint/spell（`.github/workflows/ci.yml`，无 e2e）。
