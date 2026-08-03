# Frontend `ws/chat/{agent_id}` 消息处理方式

> 整理 frontend 端 WebSocket chat 的完整消息处理流程：连接建立、状态管理、消息收发、断线重连、Session 切换。
> 核心文件为 `frontend/src/pages/agent-detail/AgentDetailPage.tsx`（~427KB），辅助文件包括 `sessionRuntimeState.ts`、`runtimeError.ts`。

---

## 目录

1. [架构概览](#1-架构概览)
2. [连接建立](#2-连接建立)
3. [状态管理](#3-状态管理)
4. [消息发送 Client → Server](#4-消息发送-client--server)
5. [消息接收 Server → Client](#5-消息接收-server--client)
6. [消息数据结构](#6-消息数据结构)
7. [运行时状态管理](#7-运行时状态管理)
8. [断线重连机制](#8-断线重连机制)
9. [Session 切换与生命周期](#9-session-切换与生命周期)
10. [完整流程图](#10-完整流程图)

---

## 1. 架构概览

```
┌─────────────────────────────────────────────────────────────────┐
│                      AgentDetailPage                             │
│                                                                  │
│  ┌──────────────┐  ┌──────────────────┐  ┌───────────────────┐ │
│  │ chatMessages  │  │    wsMapRef       │  │    activeRun       │ │
│  │  (ChatMsg[])  │  │ Record<key, WS>   │  │ SessionActiveRun   │ │
│  │  React State  │  │    连接池(Ref)     │  │   React State      │ │
│  └──────┬───────┘  └────────┬─────────┘  └─────────┬─────────┘ │
│         │                   │                       │            │
│         │          ┌────────┴─────────┐             │            │
│         │          │  ws.onmessage    │             │            │
│         │          │  按 type 路由     │◄────────────┘            │
│         │          │  thinking/chunk/ │                          │
│         │          │  tool_call/done/ │                          │
│         │          │  error/...       │                          │
│         │          └────────┬─────────┘                          │
│         │                   │                                     │
│         ▼                   ▼                                     │
│  ┌─────────────────────────────────────────────────────────────┐ │
│  │              WebSocket Connection                            │ │
│  │       ws://host/ws/chat/{agent_id}?token=&session_id=&lang= │ │
│  └─────────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────────┘
```

核心文件：

| 文件 | 作用 |
|------|------|
| `frontend/src/pages/agent-detail/AgentDetailPage.tsx` | 聊天主逻辑：连接、收发、状态、重连、UI 渲染 |
| `frontend/src/pages/agent-detail/sessionRuntimeState.ts` | SessionActiveRun 类型定义与解析、merge 工具函数 |
| `frontend/src/services/runtimeError.ts` | Runtime 错误标准化、重连/过期判定 |
| `frontend/src/services/api.ts` | HTTP API 客户端（`agentApi`, `fileApi` 等） |

---

## 2. 连接建立

### 2.1 连接 URL

```typescript
// AgentDetailPage.tsx L3293-3313
const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
const sessionParam = `&session_id=${sessionId}`;
const lang = i18n.language?.startsWith('zh') ? 'zh' : 'en';

const ws = new WebSocket(
    `${protocol}//${window.location.host}/ws/chat/${agentId}?token=${authToken}${sessionParam}&lang=${lang}`
);
```

参数说明：

| 参数 | 位置 | 必填 | 说明 |
|------|------|------|------|
| `agent_id` | Path | ✅ | Agent UUID |
| `token` | Query | ✅ | JWT 令牌（从 `useAuthStore` 获取） |
| `session_id` | Query | ❌ | ChatSession UUID。不传则后端自动创建或取最近 primary session |
| `lang` | Query | ❌ | `zh` / `en`，由 `i18n.language` 决定 |

### 2.2 连接入口：`ensureSessionSocket()`

```typescript
// AgentDetailPage.tsx L3294-3701
const ensureSessionSocket = (sess, agentId, authToken) => {
    const sessionId = String(sess.id);
    const key = buildSessionRuntimeKey(agentId, sessionId); // "{agentId}:{sessionId}"

    // 已有活跃/连接中的 socket → 跳过
    const existing = wsMapRef.current[key];
    if (existing && (existing.readyState === WebSocket.OPEN
                  || existing.readyState === WebSocket.CONNECTING)) return;

    reconnectDisabledRef.current[key] = false;
    const ws = new WebSocket(url);
    wsMapRef.current[key] = ws;

    ws.onopen    = () => { /* 见 2.3 */ };
    ws.onclose   = (e) => { /* 见 8. 断线重连 */ };
    ws.onerror   = () => { /* console.warn */ };
    ws.onmessage = (e) => { /* 见 5. 消息接收 */ };
};
```

### 2.3 `ws.onopen` — 连接成功后的初始化

```typescript
ws.onopen = () => {
    // 1. 若该 key 已被标记禁用重连 → 立即关闭
    if (reconnectDisabledRef.current[key]) { ws.close(); return; }

    // 2. 若为当前活跃 session → 更新 wsRef + wsConnected state
    if (isActiveRuntime) {
        wsRef.current = ws;
        setWsConnected(true);
    }

    // 3. 获取 runtime-state → 若有活跃 Run → 发送 attach_run 续播
    fetchSessionRuntimeState(agentId, sessionId).then((active) => {
        if (!active?.canCancel || ws.readyState !== WebSocket.OPEN) return;
        const cursor = runtimeEventCursorRef.current[`${key}:${active.runId}`];
        ws.send(JSON.stringify({
            type: 'attach_run',
            run_id: active.runId,
            ...(cursor ? { cursor } : {}),
        }));
    });

    // 4. 若有排队中的待发送消息 → 自动补发
    if (pendingChatSendRef.current?.runtimeKey === key) {
        const pending = pendingChatSendRef.current;
        pendingChatSendRef.current = null;
        dispatchChatMessage(ws, key, pending);
    }
};
```

关键行为：
- 自动 **续播 Run 事件流**（通过 `attach_run` + 事件游标 `cursor`）
- 自动 **补发排队消息**（用户发消息时 socket 未就绪的场景）

---

## 3. 状态管理

### 3.1 Ref（非响应式，跨渲染保持）

| Ref | 类型 | 用途 |
|-----|------|------|
| `wsMapRef` | `Record<string, WebSocket>` | 连接池，key=`"{agentId}:{sessionId}"` |
| `reconnectTimerRef` | `Record<string, timeout>` | 重连定时器 |
| `reconnectDisabledRef` | `Record<string, boolean>` | 重连禁用标记（4002/4003 关闭码或手动关闭） |
| `sessionUiStateRef` | `Record<string, {isWaiting, isStreaming}>` | 每个 session 的 UI 状态 |
| `sessionActiveRunRef` | `Record<string, SessionActiveRun \| null>` | 每个 session 的活跃 Run 缓存 |
| `runtimeEventCursorRef` | `Record<string, string>` | 事件游标，格式 `"{key}:{runId}"` → `"iso|eventId"` |
| `pendingChatSendRef` | `PendingChatMessage \| null` | 待发送消息（socket 未就绪时暂存） |
| `wsRef` | `WebSocket \| null` | **当前活跃 session** 的 WebSocket 引用 |
| `activeSessionIdRef` | `string \| null` | 当前活跃 session ID |
| `currentAgentIdRef` | `string \| undefined` | 当前 Agent ID（用于切换检测） |

### 3.2 State（响应式，触发 UI 重渲染）

| State | 类型 | 用途 |
|-------|------|------|
| `chatMessages` | `ChatMsg[]` | **核心**：当前聊天消息列表 |
| `activeRun` | `SessionActiveRun \| null` | 当前 Run 状态（控制取消/恢复按钮） |
| `wsConnected` | `boolean` | 当前连接状态指示灯 |
| `isWaiting` | `boolean` | 等待 LLM 响应（显示 loading 动画） |
| `isStreaming` | `boolean` | 流式接收中（用于流式消息渲染） |
| `chatInfoMsg` | `string \| null` | 信息横幅（如模型回退通知，6秒消失） |
| `activeSession` | `any \| null` | 当前选中的 ChatSession |
| `sessions` / `allSessions` | `any[]` | Session 列表（mine / all） |

### 3.3 连接池设计

同一 Agent 的不同 Session 各自维护独立的 WebSocket 连接：

```
wsMapRef = {
    "agent-uuid-1:session-uuid-a": WebSocket (OPEN),
    "agent-uuid-1:session-uuid-b": WebSocket (CLOSED),
    "agent-uuid-2:session-uuid-c": WebSocket (OPEN),
}
```

切换 Agent 或用户登出时，全部清理。

---

## 4. 消息发送（Client → Server）

### 4.1 发送入口：`sendChatMsg()`

触发条件：用户点击发送按钮 / 按 Enter

```typescript
// AgentDetailPage.tsx L4333-4425
const sendChatMsg = () => {
    // ── 前置校验 ──
    if (!id || !activeSession?.id) return;             // Agent/Session 缺失
    if (showNoModelState) return;                       // 无可用模型
    if (!chatInput.trim() && attachedFiles.length === 0) return; // 空消息

    // waiting_user 但不可恢复 → 提示等待
    if (resumesWaitingRun && (!currentRun?.canResume || !currentRun.correlationId)) {
        toast.warning('上一轮回复还在处理中...');
        return;
    }

    // ── 构建消息内容 ──
    let userMsg = chatInput.trim();
    let contentForLLM = userMsg;

    // 附件处理（支持图片、文件、工作区引用）
    if (attachedFiles.length > 0) {
        // 图片 → [image_data:data:image/...]
        // 普通文件 → [File: name.pdf]\nFile location: path\n
        // 工作区引用 → [Workspace reference: name]\nFile location: path
    }

    // ── 组装 PendingChatMessage ──
    const payload: PendingChatMessage = {
        runtimeKey: activeRuntimeKey,
        contentForLLM,      // 发送给 LLM 的完整内容（含文件标记）
        userMsg,            // UI 展示文本
        fileName: attachedFiles.map(f => f.name).join(', '),
        imageUrl: attachedFiles.length === 1 ? attachedFiles[0].imageUrl : undefined,
        modelId: effectiveChatModelId,                    // 模型覆盖
        resumeRunId: resumesWaitingRun ? currentRun?.runId : undefined,
        resumeCorrelationId: resumesWaitingRun ? currentRun?.correlationId : undefined,
    };

    // ── 清空输入框 ──
    setChatInput('');

    // ── 发送或排队 ──
    if (!activeSocket || activeSocket.readyState !== WebSocket.OPEN) {
        // Socket 未就绪 → 暂存 + 触发重连
        pendingChatSendRef.current = payload;
        if (token) ensureSessionSocket(activeSession, id, token);
        setChatInfoMsg('连接恢复中，消息将自动发送...');  // 4s 后消失
        return;
    }
    dispatchChatMessage(activeSocket, activeRuntimeKey, payload);
};
```

### 4.2 实际发送：`dispatchChatMessage()`

```typescript
// AgentDetailPage.tsx L3703-3737
const dispatchChatMessage = (socket, runtimeKey, payload) => {
    // 1. 设置 UI 状态
    setIsWaiting(true);
    setIsStreaming(false);
    setSessionUiState(runtimeKey, { isWaiting: true, isStreaming: false });

    // 2. 恢复场景：乐观更新 canResume → false
    if (payload.resumeRunId) {
        const current = sessionActiveRunRef.current[runtimeKey];
        if (current?.runId === payload.resumeRunId) {
            sessionActiveRunRef.current[runtimeKey] = { ...current, canResume: false };
            setActiveRun({ ...current, canResume: false });
        }
    }

    // 3. 立即追加用户消息到列表（乐观更新）
    setChatMessages(prev => [...prev, parseChatMsg({
        role: 'user',
        content: payload.userMsg,
        fileName: payload.fileName,
        imageUrl: payload.imageUrl,
        timestamp: new Date().toISOString()
    })]);

    // 4. 通过 WebSocket 发送
    socket.send(JSON.stringify({
        content: payload.contentForLLM,        // LLM 实际接收
        display_content: payload.userMsg,       // 用于生成标题、历史展示
        file_name: payload.fileName,
        model_id: payload.modelId,
        ...(payload.resumeRunId ? { run_id: payload.resumeRunId } : {}),
        ...(payload.resumeCorrelationId ? { correlation_id: payload.resumeCorrelationId } : {}),
    }));

    // 5. 延迟轮询 runtime-state
    setTimeout(() => fetchSessionRuntimeState(agentId, sessionId), 250);
    setTimeout(() => fetchSessionRuntimeState(agentId, sessionId), 1000);
};
```

### 4.3 发送消息类型汇总

| 场景 | WebSocket payload |
|------|------------------|
| **普通聊天** | `{content, display_content, file_name, model_id}` |
| **恢复 waiting_user** | `{content, display_content, file_name, model_id, run_id, correlation_id}` |
| **重连续播** | `{type: "attach_run", run_id, cursor?}` |
| **取消运行** | `{type: "abort", run_id}` |

### 4.4 附件格式转换

```typescript
// 图片（Vision 模型）
"[image_data:data:image/png;base64,...]\n用户消息"

// 图片（非 Vision 模型）
"[图片文件已上传: filename.jpg]\n用户消息"

// 普通文件
"[File: report.pdf]\nFile location: workspace/uploads/report.pdf ...\n\n文件内容...\n\nQuestion: 用户消息"

// 工作区引用（拖拽）
"[Workspace reference: config.json]\nFile location: workspace/config.json\nUse read_file if you need the file contents.\n\n用户消息"
```

---

## 5. 消息接收（Server → Client）

### 5.1 主处理器：`ws.onmessage`

```typescript
// AgentDetailPage.tsx L3364-3700
ws.onmessage = (e) => {
    const d = JSON.parse(e.data);

    // ── 游标持久化（用于重连后续播）──
    if (d.event_cursor && d.run_id) {
        runtimeEventCursorRef.current[`${key}:${d.run_id}`] = d.event_cursor;
    }

    // ── 入职状态（特殊处理）──
    if (d.type === 'onboarded' || d.type === 'onboarding_pending') {
        setIsWaiting(false); setIsStreaming(false);
        fetchSessionRuntimeState(agentId, sessionId);
        if (d.type === 'onboarded') queryClient.invalidateQueries({ queryKey: ['agent', agentId] });
        return;
    }

    // ── runtime_status → 仅触发 HTTP 轮询，不本地修改 ──
    if (d.type === 'runtime_status') {
        fetchSessionRuntimeState(agentId, sessionId);
        return;
    }

    // ── 非活跃 session → 仅刷新列表，不更新 UI ──
    if (!isActiveRuntime) {
        if (['done', 'error', 'quota_exceeded', 'trigger_notification'].includes(d.type)) {
            fetchMySessions(true, agentId);  // 更新未读数
        }
        if (['done', 'error', 'quota_exceeded'].includes(d.type)
            && d.runtime_status !== 'waiting_user') {
            closeSessionSocket(key, true);    // 终态 → 关闭连接
        }
        return;
    }

    // ── 流式状态管理 ──
    if (['thinking', 'chunk', 'workspace_draft', 'tool_call'].includes(d.type)) {
        setIsWaiting(false); setIsStreaming(true);
    }
    if (['done', 'error', 'quota_exceeded'].includes(d.type)) {
        setIsWaiting(false); setIsStreaming(false);
    }

    // ── 按 type 路由处理 ──
    switch (d.type) {
        case 'connected':          handleConnected(d);          break;
        case 'thinking':           handleThinking(d);           break;
        case 'chunk':              handleChunk(d);              break;
        case 'tool_call':          handleToolCall(d);           break;
        case 'workspace_draft':    handleWorkspaceDraft(d);     break;
        case 'done':               handleDone(d);               break;
        case 'error':
        case 'quota_exceeded':     handleError(d);              break;
        case 'agentbay_live':      handleAgentBayLive(d);       break;
        case 'trigger_notification': handleTriggerNotification(d); break;
        case 'info':               handleInfo(d);               break;
        default:                   handleFallback(d);           break;
    }
};
```

### 5.2 各消息类型详解

#### `connected` — 连接确认

```json
{ "type": "connected", "session_id": "uuid" }
```

- 保存 `session_id` 到 `wsSessionId` state
- 触发 `fetchSessionRuntimeState()` 获取最新 Run 状态

#### `thinking` — LLM 思考链（推理过程）

```json
{ "type": "thinking", "content": "让我分析这个问题..." }
```

处理逻辑：**流式追加**到最后一个 streaming assistant 消息的 `thinking` 字段：

```typescript
setChatMessages(prev => {
    const last = prev[prev.length - 1];
    if (last?.role === 'assistant' && last._streaming) {
        // 追加到现有流式消息
        return [...prev.slice(0, -1), {
            ...last,
            thinking: (last.thinking || '') + d.content
        }];
    }
    // 创建新的流式消息
    return [...prev, {
        role: 'assistant', content: '', thinking: d.content, _streaming: true
    }];
});
```

#### `chunk` — LLM 回复（逐 token 流式输出）

```json
{ "type": "chunk", "content": "这段代码" }
```

处理逻辑：与 `thinking` 相同，但追加到 `content` 字段。

#### `tool_call` — 工具调用事件

```json
{
    "type": "tool_call",
    "name": "write_file",
    "call_id": "call_abc",
    "args": { "file_path": "...", "content": "..." },
    "status": "running" | "done",
    "result": "文件写入成功",
    "reasoning_content": "需要创建配置文件...",
    "execution_status": "success",
    "error_code": null,
    "live_preview": { "env": "desktop", "screenshot_url": "..." },
    "workspace_activity": { "path": "...", "action": "write" }
}
```

按工具类别分流处理：

| 工具分类 | 判定 | 处理动作 |
|---------|------|---------|
| **AWARE 工具** | `set_trigger`, `update_trigger`, `cancel_trigger`, `list_triggers`, `list_focus_items`, `upsert_focus_item`, `complete_focus_item` | 打开 Aware 面板、刷新数据 |
| **WORKSPACE 工具** | `write_file`, `edit_file`, `move_file`, `delete_file`, `convert_*` | 更新 workpaceLiveDraft / workspaceActivities、自动切换工作区路径 |
| **AgentBay 文件传输** | `agentbay_file_transfer` | 更新 liveState.transfer |
| **Live Preview** | `d.live_preview` 存在 | 更新桌面/浏览器/代码预览 |
| **所有工具** | — | `upsertToolCallMessage()` 更新消息列表 |

**tool_call 的 upsert 逻辑**（`upsertToolCallMessage`）：

```
1. 若有 toolCallId → 精确匹配替换
2. 否则 → 按 toolName + toolStatus=running 模糊匹配
3. running → done 时不降级（replay 保护）：已是 done 的不回退为 running
```

#### `workspace_draft` — 工具草稿预览

```json
{ "type": "workspace_draft", "name": "write_file", "arguments": "{...}", "id": "draft-1", "index": 0 }
```

- 仅处理 `WORKSPACE_TOOLS` 集合中的工具
- 解析参数创建 `WorkspaceLiveDraft` 草稿（status: `drafting`）
- 自动切换工作区面板（`allowWorkspaceAutoSwitch`）
- 同时插入 `tool_call` 条目（status: `running`）

#### `done` — Run 完成

```json
{
    "type": "done",
    "role": "assistant",
    "content": "完整回复...",
    "message_id": "uuid",
    "run_id": "uuid",
    "runtime_status": "completed" | "failed" | "cancelled" | "waiting_user",
    "correlation_id": "uuid",
    "error": { "code": "...", "message": "..." },
    "delivery_error": "..."
}
```

处理逻辑：
1. **终态管理**：completed/failed/cancelled → 更新 ActiveRun 为不可恢复/不可取消
2. **流式结束**：将最后一个 streaming assistant → terminal message
3. **防重复**：`mergeTerminalAssistantMessage()` 检查是否已由 HTTP 加载（按 `message_id` 或 `content` 匹配）
4. **waiting_user 特殊处理**：设置 `waitingSessionActiveRunHint`
5. **清理**：刷新 session 列表、清除未读

```typescript
// mergeTerminalAssistantMessage 核心逻辑：
// 1. 若有 message_id → 按 id 精确匹配
// 2. 否则 → 匹配最后一个非流式 assistant 的 content
// 3. 找到匹配 → 仅更新 runtimeError（保留 HTTP 加载的 canonical 消息）
// 4. 未找到 → 追加到列表
```

#### `error` / `quota_exceeded` — 错误

```json
{
    "type": "error",
    "code": "model_unavailable",
    "content": "...",
    "message": "...",
    "run_id": "uuid",
    "agent_id": "uuid",
    "stage": "intake",
    "trace_id": "hex12",
    "error": { "code": "...", "message": "...", "stage": "..." }
}
```

处理：
1. `normalizeRuntimeError(d)` 标准化错误结构
2. `runtimeErrorDisablesReconnect(error)` → 可能禁用重连
3. `runtimeErrorMarksAgentExpired(error)` → 可能标记 Agent 过期
4. 追加 warning 消息到 chatMessages（防重复：检查最后一条消息）

#### `agentbay_live` — 实时代码/桌面/浏览器输出

```json
{
    "type": "agentbay_live",
    "env": "code" | "desktop" | "browser",
    "output": "...",
    "stream": "stdout" | "stderr",
    "screenshot_url": "https://...",
    "call_id": "..."
}
```

- `code` → 追加到 `liveState.code.output`（stdout/stderr 区分）
- `desktop` / `browser` → 替换 `liveState[env].screenshotUrl`
- 自动打开 Live Panel（`allowLivePanelAutoFocus`）

#### 其他类型

| type | 处理 |
|------|------|
| `trigger_notification` | 若属于当前 session → 追加内容到 chatMessages |
| `info` | 显示 6 秒横幅（如 fallback 模型切换通知） |
| 未知类型 | 兜底：`parseChatMsg({role: d.role, content: d.content})` 追加 |

---

## 6. 消息数据结构

### 6.1 `ChatMsg` — 聊天消息

```typescript
// AgentDetailPage.tsx L2875
interface ChatMsg {
    id?: string;                              // 持久化消息 ID（后端分配）
    role: 'user' | 'assistant' | 'tool_call'; // 角色
    content: string;                          // 消息正文
    fileName?: string;                        // 附件文件名
    toolName?: string;                        // 工具名（role=tool_call）
    toolCallId?: string;                      // 工具调用 ID
    toolArgs?: any;                           // 工具参数
    toolStatus?: 'running' | 'done';          // 工具执行状态
    toolResult?: string;                      // 工具返回结果
    toolThinking?: string;                    // 工具推理（reasoning_content）
    thinking?: string;                        // LLM 思考链
    imageUrl?: string;                        // 图片 data URL
    timestamp?: string;                       // ISO 时间戳
    runtimeError?: ReturnType<typeof normalizeRuntimeError>;
    _streaming?: boolean;                     // 内部标记：是否在流式接收中
}
```

### 6.2 `PendingChatMessage` — 待发送消息

```typescript
// AgentDetailPage.tsx L2979
type PendingChatMessage = {
    runtimeKey: string;          // "{agentId}:{sessionId}"
    contentForLLM: string;       // LLM 实际接收（含文件标记）
    userMsg: string;             // UI 展示文本
    fileName: string;            // 附件名（逗号分隔）
    imageUrl?: string;           // 单图 data URL
    modelId?: string | null;     // 模型覆盖 ID
    resumeRunId?: string;        // waiting_user 恢复
    resumeCorrelationId?: string;
};
```

### 6.3 `SessionActiveRun` — 运行时状态

```typescript
// sessionRuntimeState.ts L10
type SessionActiveRun = {
    runId: string;                           // Run UUID
    threadId: string;                        // 线程 ID
    sessionId: string;                       // Session UUID
    status: string;                          // "pending"|"running"|"waiting_user"|"completed"|"failed"|"cancelled"
    waitingType?: string | null;             // "user" 等
    waitingReason?: string | null;
    correlationId?: string | null;           // 恢复关联 ID
    modelStepCount: number;                  // LLM 调用步数
    canResume: boolean;                      // status=waiting_user && waitingType && correlationId
    canCancel: boolean;                      // 非终态 && 后端标记可取消
    pendingToolReconciliations: ToolReconciliation[];
};
```

```typescript
type ToolReconciliation = {
    executionId: string;
    toolCallId: string;
    toolName: string;
    resultSummary?: string | null;
    errorCode?: string | null;
    canReconcile: boolean;       // 是否需要用户确认
};
```

### 6.4 `parseChatMsg` — 用户消息解析

```typescript
// AgentDetailPage.tsx L3177
const parseChatMsg = (msg: ChatMsg): ChatMsg => {
    if (msg.role !== 'user') return msg; // 非用户消息原样返回

    // 1. 标准 Web 格式：[file:report.pdf]\ncontent
    //    提取 fileName，剩余为 content

    // 2. 飞书/Slack 格式：[文件已上传: workspace/uploads/report.pdf]
    //    提取文件名（取最后一段），剩余为 content

    // 3. 旧格式：[File: report.pdf]\nFile location:...\nQuestion: ...
    //    提取 fileName，提取 Question 后的内容为 content

    // 4. 多图片：保留逗号分隔的 fileName（由 ChatMessageItem 独立渲染各图）
};
```

---

## 7. 运行时状态管理

### 7.1 HTTP 轮询

`fetchSessionRuntimeState(agentId, sessionId)` 通过 HTTP 获取权威 Run 状态：

```
GET /api/agents/{agentId}/sessions/{sessionId}/runtime-state
```

响应格式：
```json
{
    "active_run": {
        "run_id": "uuid",
        "thread_id": "uuid",
        "session_id": "uuid",
        "status": "running",
        "waiting_type": null,
        "waiting_reason": null,
        "correlation_id": null,
        "model_step_count": 5,
        "can_resume": false,
        "can_cancel": true,
        "pending_tool_reconciliations": []
    }
}
```

### 7.2 状态解析流程

```
sessionActiveRunFromResponse(payload)
    │
    ├─ 提取 active_run
    ├─ 验证必填字段（run_id, thread_id, session_id, status）
    ├─ 解析可选字段（waiting_type, waiting_reason, correlation_id）
    ├─ 计算派生状态：
    │   canResume = status === 'waiting_user' && waitingType !== null && correlationId !== null
    │   canCancel = raw.can_cancel === true && status 非终态
    └─ 解析 pending_tool_reconciliations[]
```

### 7.3 失败兜底

当 HTTP 请求失败或响应格式无效时，调用 `failClosedSessionActiveRun()`：

```typescript
// 所有可操作能力 → false，防止用户操作已不可用的 Run
{ ...current, canResume: false, canCancel: false, pendingToolReconciliations: canReconcile→false }
```

### 7.4 Run 完成检测

```typescript
export const runtimeCompletionNeedsMessageRefresh = (previous, next) =>
    previous !== null && next === null;
// previous 有值 → next 为 null：Run 刚完成
// 触发 refreshSessionMessages() 从 HTTP 加载最终消息
```

---

## 8. 断线重连机制

### 8.1 重连触发

```typescript
ws.onclose = (e) => {
    // 清理当前 session 的 UI 状态
    setSessionUiState(key, { isWaiting: false, isStreaming: false });
    if (isActiveRuntime) {
        wsRef.current = null;
        setWsConnected(false);
        setIsWaiting(false);
        setIsStreaming(false);
    }

    // ── 特殊关闭码：禁用重连 ──
    if (e.code === 4003) {                // Agent 过期
        reconnectDisabledRef.current[key] = true;
        if (isActiveRuntime) setAgentExpired(true);
        return;
    }
    if (e.code === 4002) {                // 鉴权/会话错误
        reconnectDisabledRef.current[key] = true;
        return;
    }

    // ── 普通断开：2 秒后重连 ──
    scheduleReconnect();
};
```

### 8.2 重连策略

```typescript
const scheduleReconnect = () => {
    if (reconnectDisabledRef.current[key]) return;
    clearReconnectTimer(key);
    reconnectTimerRef.current[key] = setTimeout(() => {
        reconnectTimerRef.current[key] = null;
        if (!reconnectDisabledRef.current[key]) {
            ensureSessionSocket(sess, agentId, authToken);
        }
    }, 2000);  // 固定 2 秒延迟
};
```

特点：
- **固定延迟**：2 秒，无指数退避
- **可中断**：切换 Agent/Session 时清理所有定时器
- **可禁用**：4002/4003 关闭码或明确调用 `closeSessionSocket(key, true)`

### 8.3 重连后的恢复

```
ws.onopen
    │
    ├─ fetchSessionRuntimeState()
    │   └─ 若 ActiveRun.canCancel → ws.send({type:"attach_run", run_id, cursor})
    │       └─ 后端从 cursor 位置开始续播事件流（不再重复已处理的事件）
    │
    └─ 若 pendingChatSendRef 有值 → dispatchChatMessage() 补发
```

### 8.4 连接清理

Agent 或用户切换时：

```typescript
useEffect(() => {
    // 禁用所有重连
    Object.keys(reconnectDisabledRef.current).forEach(k => {
        reconnectDisabledRef.current[k] = true;
    });
    // 关闭所有 WebSocket
    Object.keys(wsMapRef.current).forEach(k => {
        const ws = wsMapRef.current[k];
        if (ws && ws.readyState !== WebSocket.CLOSED) ws.close();
    });
    wsMapRef.current = {};
    sessionActiveRunRef.current = {};
    wsRef.current = null;
}, [currentUser?.id, token]);
```

### 8.5 错误驱动的重连禁用

```typescript
// runtimeError.ts
export const runtimeErrorDisablesReconnect = (error) => {
    // 某些错误码（如 auth 相关）标记重连无效
};

export const runtimeErrorMarksAgentExpired = (error) => {
    // agent_expired 等错误码触发 UI 过期提示
};
```

---

## 9. Session 切换与生命周期

### 9.1 可写判定

```typescript
const isWritableSession = (sess, scopeOverride) => {
    if (!sess) return false;
    if (sess.source_channel === 'agent' || sess.participant_type === 'agent') return false; // A2A
    if (sess.is_group) return false;                                                        // 群聊
    if (canViewAllAgentChatSessions && scopeOverride === 'all') return false;               // 管理员查看他人
    if (sessionUserIdStr(sess) !== viewerUserIdStr()) return false;                         // 非自己的 session
    return true;
};
```

- **可写** → 建立 WebSocket + 可发消息
- **只读** → 仅通过 HTTP 加载历史，不建 WebSocket

### 9.2 `selectSession()` 切换流程

```
selectSession(sess)
    │
    ├─ normalizeChatSession(sess)          // 标准化 ID/字段
    ├─ 判断 isWritableSession()
    │
    ├─ 清理上一 Session 状态：
    │   ├─ abort 进行中的消息加载（AbortController）
    │   ├─ chatMessages = []
    │   ├─ 重置分页游标、UI 状态
    │   └─ 递增 loadSeq 防止竞态
    │
    ├─ 设置 activeSessionIdRef = sess.id
    │
    ├─ HTTP GET /sessions/{id}/messages?limit=20   // 加载历史
    │   └─ parseChatMsg() 解析每条消息
    │
    ├─ 若可写：ensureSessionSocket()              // 建立 WebSocket
    └─ 若可写：fetchSessionRuntimeState()          // 获取活跃 Run
```

### 9.3 新建 Session

```typescript
const createNewSession = async () => {
    // POST /api/agents/{id}/sessions  → 返回新 ChatSession
    // 可能包含 greeting（welcome_message）
    // 切换到新 session：selectSession(newSess)
};
```

### 9.4 删除 Session

```typescript
const deleteSession = async (sessionId) => {
    // 若为活跃 session → 清理 WebSocket + 状态
    // DELETE /api/agents/{id}/sessions/{sessionId}
    // 刷新 session 列表
};
```

---

## 10. 完整流程图

### 10.1 发送消息全链路

```
用户点击发送 / Enter
    │
    ▼
sendChatMsg()
    │
    ├─ [校验] Agent/Session 存在、有模型、消息非空
    ├─ [waiting_user 校验] canResume + correlationId 齐全？
    │
    ├─ [构建] PendingChatMessage
    │   ├─ 附件转格式：图片→[image_data:...], 文件→[File:...]
    │   └─ 组装 contentForLLM / userMsg / fileName / modelId / resumeRunId
    │
    ├─ [Socket 未就绪?]──YES──→ pendingChatSendRef = payload
    │   │                       ensureSessionSocket() → ws.onopen 时自动补发
    │   │
    │   └─ [Socket 已就绪] → dispatchChatMessage()
    │                            │
    │                            ├─ setIsWaiting(true)
    │                            ├─ chatMessages.push(user ChatMsg)   // 乐观更新
    │                            ├─ ws.send({content, display_content, file_name, model_id, run_id?, correlation_id?})
    │                            └─ setTimeout → fetchSessionRuntimeState (250ms + 1000ms)
    │
    ▼
后端 WebSocket 接收
    │
    ├─ _accept_client_message() → 校验/持久化
    ├─ enqueue_chat_runtime() → AgentRun + AgentRunCommand
    └─ stream_web_chat_run() → LangGraph LLM ↔ Tool 循环 → 流式事件
         │
         ├─ thinking    → ws.send({type:"thinking", content})
         ├─ chunk       → ws.send({type:"chunk", content})
         ├─ tool_call   → ws.send({type:"tool_call", name, call_id, status, result})
         ├─ done        → ws.send({type:"done", content, runtime_status, ...})
         └─ error       → ws.send({type:"error", code, message})
         │
         ▼
ws.onmessage  → 更新 chatMessages → React 重渲染 → 用户看到回复
```

### 10.2 接收消息路由

```
ws.onmessage(e)
    │
    ├─ JSON.parse
    ├─ 保存 event_cursor
    │
    ├─ d.type === 'runtime_status'?  ──YES──→ fetchSessionRuntimeState() → return
    │
    ├─ d.type === 'onboarded'/'onboarding_pending'? ──YES──→ 刷新入职状态 → return
    │
    ├─ isActiveRuntime? ──NO──→
    │   ├─ done/error/quota_exceeded/trigger_notification → 刷新 session 列表
    │   └─ 终态消息(done/error, 非waiting_user) → closeSessionSocket()
    │
    └─ YES（当前活跃 session）:
        │
        ├─ 设置 isWaiting / isStreaming
        │
        ├─ connected          → 保存 session_id + 获取 runtime
        ├─ thinking           → 流式追加 thinking 到最后一条 assistant
        ├─ chunk              → 流式追加 content 到最后一条 assistant
        ├─ tool_call          → upsertToolCallMessage()
        │   ├─ AWARE 工具     → 刷新面板
        │   ├─ WORKSPACE 工具 → 更新工作区草稿/活动
        │   ├─ AgentBay       → 更新 Live Preview
        │   ├─ Live Preview   → 更新截图/代码输出
        │   └─ workspace_activity → 更新活动列表
        ├─ workspace_draft    → 创建工作区草稿 + tool_call 条目
        ├─ done               → 终止流式 + mergeTerminalAssistantMessage
        │   ├─ waiting_user   → waitingSessionActiveRunHint
        │   └─ 终态           → 清理 ActiveRun
        ├─ error/quota_exceeded → normalizeRuntimeError + 禁用重连/标记过期
        ├─ agentbay_live      → 更新 liveState 面板输出
        ├─ trigger_notification → 追加通知内容
        ├─ info               → 6秒横幅
        └─ 其他               → 兜底追加
        │
        ▼
    setChatMessages() → React 重渲染
```

### 10.3 重连流程

```
ws.onclose(e)
    │
    ├─ 清理 UI 状态（isWaiting=false, isStreaming=false）
    │
    ├─ code=4003 (Agent 过期)? → 禁用重连 + setAgentExpired(true)
    ├─ code=4002 (鉴权错误)?   → 禁用重连
    │
    └─ 其他 → scheduleReconnect()
               │
               └─ 2s → ensureSessionSocket()
                         │
                         ws.onopen()
                         ├─ fetchSessionRuntimeState()
                         │   └─ activeRun.canCancel → attach_run + cursor 续播
                         └─ pendingChatSendRef 有值 → dispatchChatMessage 补发
```

### 10.4 Session 切换流程

```
用户点击 Session / 新建 Session
    │
    ▼
selectSession(sess)
    │
    ├─ normalizeChatSession(sess)
    ├─ 判断 isWritableSession()
    │
    ├─ [清理旧状态]
    │   ├─ abort 消息加载
    │   ├─ chatMessages = []
    │   ├─ 重置分页、UI 状态
    │   └─ loadSeq++
    │
    ├─ activeSessionIdRef = sess.id
    │
    ├─ HTTP GET /sessions/{id}/messages?limit=20
    │   └─ 加载历史 → parseChatMsg → setChatMessages / setHistoryMsgs
    │
    ├─ [可写] ensureSessionSocket()
    └─ [可写] fetchSessionRuntimeState()
```
