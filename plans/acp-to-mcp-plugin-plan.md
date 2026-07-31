# IDE Plugin: ACP → MCP 改造计划

## 目标

将 IDE 工具调用从"本地 HTTP MCP Server"切换为"通过 `/ws/chat` WebSocket 代理"，解决后端无法直接访问 IDE 本地 `127.0.0.1:38591` 的网络断层问题。

## 现状

```
Plugin ──HTTP POST──► 127.0.0.1:38591/mcp (McpServer)
                        ↑
                        │ 后端在 Docker/远程时不可达 ❌

Plugin ──WebSocket──► /ws/chat/{agentId} (聊天流式，已通 ✅)
Plugin ──WebSocket──► {"type":"mcp_register","url":"http://127.0.0.1:38591/mcp"} (已发，后端未处理 ⚠️)
```

## 目标架构

```
Plugin ◄──WebSocket── /ws/chat/{agentId}
  │
  │  后端发: {"type":"mcp_tool_call","call_id":"...","name":"find_references","arguments":{...}}
  │  插件回复: {"type":"mcp_tool_result","call_id":"...","output":"...","is_error":false}
  │
  └── 本地 ToolRegistry.execute() 直接执行（不走 HTTP）
```

---

## 修改清单

### 1. `ChatEvent.kt` — 新增协议消息类型

**文件**: `src/main/kotlin/com/clawith/acp/mcp/chat/ChatEvent.kt`

新增两个独立 data class（不继承 `ChatEvent`，不会进入 `SharedFlow`）：

```kotlin
/** MCP 工具调用 — 后端通过 WebSocket 请求插件执行本地 IDE 工具（仅协议解析用） */
data class McpToolCallPayload(
    val callId: String,
    val name: String,
    val arguments: JsonObject,
    val sessionId: String,
)

/** MCP 工具结果 — 插件返回给后端（仅协议构造用） */
data class McpToolResultPayload(
    val callId: String,
    val output: String,
    val isError: Boolean,
    val sessionId: String,
)
```

### 2. `ChatBackendClient.kt` — 接收并处理 `mcp_tool_call`

**文件**: `src/main/kotlin/com/clawith/acp/mcp/chat/ChatBackendClient.kt`

#### 2.1 新增依赖注入

```kotlin
class ChatBackendClient : ChatBackend {
    // 新增: 持有 McpProjectService 引用以执行工具
    var mcpProjectService: McpProjectService? = null
    // ...
}
```

#### 2.2 `handleMessage()` 新增 `mcp_tool_call` 分支

在 `handleMessage()` 的 `when (type)` 中新增：

```kotlin
"mcp_tool_call" -> {
    val callId = json["call_id"]?.jsonPrimitive?.content ?: ""
    val toolName = json["name"]?.jsonPrimitive?.content ?: ""
    val args = json["arguments"]?.jsonObject ?: buildJsonObject {}
    // 异步执行，不阻塞消息循环
    scope.launch {
        executeMcpToolAndRespond(sid, callId, toolName, args)
    }
    return  // 不 emit ChatEvent，直接回传 result
}
```

#### 2.3 新增 `executeMcpToolAndRespond()` 方法

```kotlin
private suspend fun executeMcpToolAndRespond(
    sessionId: String,
    callId: String,
    toolName: String,
    arguments: JsonObject
) {
    val mcpService = mcpProjectService
    if (mcpService == null) {
        sendMcpToolError(sessionId, callId, "MCP service not available")
        return
    }
    val ctx = buildToolContext(mcpService) // 构建 ToolExecutionContext
    val result = mcpService.toolRegistry.execute(toolName, arguments, ctx)
    sendMcpToolResult(sessionId, callId, result)
}

private fun sendMcpToolResult(sessionId: String, callId: String, result: ToolResult) {
    val state = wsStateFor(sessionId) ?: return
    val msg = buildJsonObject {
        put("type", JsonPrimitive("mcp_tool_result"))
        put("call_id", JsonPrimitive(callId))
        put("output", JsonPrimitive(result.content))
        put("is_error", JsonPrimitive(result.isError))
    }
    state.webSocket?.send(msg.toString())
}

private fun sendMcpToolError(sessionId: String, callId: String, error: String) {
    val state = wsStateFor(sessionId) ?: return
    val msg = buildJsonObject {
        put("type", JsonPrimitive("mcp_tool_result"))
        put("call_id", JsonPrimitive(callId))
        put("output", JsonPrimitive(error))
        put("is_error", JsonPrimitive(true))
    }
    state.webSocket?.send(msg.toString())
}
```

#### 2.4 需要解决的依赖: `ToolExecutionContext`

`ToolRegistry.execute()` 需要 `ToolExecutionContext`。创建一个轻量实现：

```kotlin
// ChatBackendClient 内部
private fun buildToolContext(mcpService: McpProjectService): ToolExecutionContext {
    return object : ToolExecutionContext {
        override val project: Project = mcpService.project
        override val searchOps: ClawithAcpSearchOps = ClawithAcpSearchOps(project)
        override val hierarchyOps: ClawithAcpHierarchyOps = ClawithAcpHierarchyOps(project)
        override val refactorOps: ClawithAcpRefactorOps = ClawithAcpRefactorOps(project)
        override var buildErrorCache: List<FsBuildMessage> = emptyList()
        override fun notifyFileChanged(path: String, originalContent: String?, newContent: String, isNewFile: Boolean) {}
        override fun resolveFile(path: String, basePath: String?): VirtualFile? =
            IdeToolBase.resolveFileStatic(path, basePath ?: project.basePath)
    }
}
```

> **注意**: 这要求 `ChatBackendClient` 能获取到 `Project` 实例。可选项：
> - A) 构造函数注入 `Project`
> - B) 通过 `McpProjectService` 获取（`mcpProjectService.project`）
> - C) 从当前打开项目推断

### 3. `ChatPage.kt` — 注入 McpProjectService + 确保 MCP 启动

**文件**: `src/main/kotlin/com/clawith/acp/ui/chat/ChatPage.kt`

#### 3.1 `startSessionWithAgent()` 注入 MCP 服务

在 `backend.connect()` **之前**注入 `McpProjectService`，这样 `onOpen` 回调中就能发送 `mcp_register`：

```kotlin
// startSessionWithAgent() 中（替换现有 registerMcp 调用）
val backend = chatBackend ?: return@launch

// 注入 MCP 服务（必须在 connect 之前，供 onOpen 使用）
if (s.mcpEnabled) {
    val mcpService = McpProjectService.getInstance(project)
    if (!mcpService.isRunning) {
        mcpService.start()  // 确保 MCP Server 已启动（ToolRegistry 已注册工具）
    }
    (backend as? ChatBackendClient)?.mcpProjectService = mcpService
} else {
    // MCP 禁用 → 清空引用，onOpen 会发送空 tools 列表清理后端
    (backend as? ChatBackendClient)?.mcpProjectService = null
}

// 1. 创建后端 session
val realSid = backend.createSession(agentId)
// 2. 连接 WebSocket → onOpen 中自动发送 client_info + mcp_register
backend.connect(s.apiHost, s.apiPort, agentId, tok, realSid, "zh", s.apiSsl)
// 3. 启动后台收集器
startBackgroundCollector(tab)
// 4. 持久化选择
s.lastAgentId = agentId; s.lastAgentName = agentName

// 注意：mcp_register 不再在这里调用！
// 改为 ChatBackendClient.onOpen() 自动发送（消除竞态）
```

#### 3.2 `onNewSessionClicked` 中的 temp client

`onNewSessionClicked` 创建的临时 `ChatBackendClient`（第 548 行）用于 WelcomeCard 展示 agent/model 列表，不参与聊天。它的 `mcpProjectService` 为 null → `onOpen` 不发 `mcp_register` → 正确行为（WelcomeCard 还没有 session）。

#### 3.3 移除旧的 HTTP `registerMcp` 调用

删除第 592-595 行：
```kotlin
// ❌ 旧代码（删除）
// if (s.mcpEnabled) {
//     val localIp = java.net.InetAddress.getLocalHost().hostAddress
//     backend.registerMcp(realSid, "http://$localIp:${s.mcpPort}/mcp")
// }
```

### 4. `ChatBackendClient.kt` — `onOpen` 统一发送 `client_info` + `mcp_register`

将 MCP 注册从 `ChatPage.startSessionWithAgent` 移到 `ChatBackendClient.onOpen`，消除竞态：

```kotlin
override fun onOpen(ws: WebSocket, response: Response) {
    if (sid in reconnectDisabled) { ... }
    _state.value = ConnectionState.Connected
    log.info("[CHAT-WS] connected session=$sid")

    // 1. 发送客户端类型标识
    sendClientInfo(ws, sid)

    // 2. 发送 MCP 工具注册（初始连接 + 重连都走这里）
    sendMcpRegister(ws, sid)

    // 3. Flush pending message（现有逻辑不变）
    val pending = state.pendingMsg
    if (pending != null) { ... }

    // 4. Fetch runtime state + attach_run（现有逻辑不变）
    scope.launch { fetchRuntimeState(sid)?.let { ... } }
}

private fun sendClientInfo(ws: WebSocket, sessionId: String) {
    val meta = buildJsonObject {
        put("project_path", JsonPrimitive(mcpProjectService?.getProjectPath() ?: ""))
    }
    val msg = buildJsonObject {
        put("type", JsonPrimitive("client_info"))
        put("client_type", JsonPrimitive("ide_plugin"))
        put("meta", meta)
    }
    ws.send(msg.toString())
}

private fun sendMcpRegister(ws: WebSocket, sessionId: String) {
    val state = wsStateFor(sessionId) ?: return
    val mcpService = mcpProjectService
    val toolsJson = if (mcpService?.isRunning == true) {
        buildJsonArray {
            mcpService.toolRegistry.getAllTools().forEach { tool ->
                add(buildJsonObject {
                    put("name", JsonPrimitive(tool.name))
                    put("description", JsonPrimitive(tool.description))
                    put("inputSchema", tool.inputSchema ?: buildJsonObject {
                        put("type", JsonPrimitive("object"))
                        putJsonObject("properties") {}
                    })
                })
            }
        }
    } else {
        buildJsonArray {} // 空列表 → 后端清理工具
    }
    val msg = buildJsonObject {
        put("type", JsonPrimitive("mcp_register"))
        put("url", JsonPrimitive("ws-proxy://${state.agentId}"))
        put("project_path", JsonPrimitive(mcpService?.getProjectPath() ?: ""))
        put("tools", toolsJson)
    }
    ws.send(msg.toString())
    log.info("[CHAT-WS] >>> mcp_register ${toolsJson.size} tools session=$sessionId")
}
```

> **关键改进**：`mcp_register` 在 `onOpen` 中发送，保证 WebSocket 已连接。消除了 `ChatPage.startSessionWithAgent()` 中 `connect()` 后立即 `registerMcp()` 的竞态（WebSocket 可能还未 open）。

---

### 5. `ChatBackendClient.kt` — `registerMcp` 实现

`registerMcp` 是 `ChatBackend` 接口中已有的方法。保留签名不变，但内部委托给 `sendMcpRegister`（在 `onOpen` 中已自动调用，也支持外部手动触发）：

```kotlin
override fun registerMcp(sessionId: String, url: String) {
    val state = wsStateFor(sessionId) ?: return
    val ws = state.webSocket ?: return  // WebSocket 未就绪时静默跳过（onOpen 会重新发送）
    sendMcpRegister(ws, sessionId)
}
```

> **正常流程**：`connect()` → `onOpen` → `sendMcpRegister`。`registerMcp` 仅作兜底（如设置变更后手动触发重新注册）。

### 6. IDE 工具线程安全 — 无需额外处理

`IdeToolBase.execute()` 已是 `suspend` 函数，内部通过 `edtAction()` → `suspendCancellableCoroutine` + `invokeAndWait` 自动处理 EDT 切换。**无需引入 `requiresWriteAction` 标记**。从 `Dispatchers.IO` 协程调用 `tool.execute(args, ctx)` 完全正确 — 工具内部会自动挂起协程、切到 EDT 执行、然后恢复协程。

```kotlin
// IdeToolBase.kt:80 — 已有完善的 EDT 调度
protected suspend fun <T> edtAction(action: () -> T): T =
    if (ApplicationManager.getApplication().isDispatchThread) action()
    else suspendCancellableCoroutine { cont ->
        ApplicationManager.getApplication().invokeAndWait({ ... }, ModalityState.nonModal())
    }
```

**结论**：`executeMcpToolAndRespond` 保持简单，直接调 `tool.execute()` 即可：

```kotlin
private suspend fun executeMcpToolAndRespond(
    sessionId: String, callId: String, toolName: String, arguments: JsonObject
) {
    val mcpService = mcpProjectService
    if (mcpService == null) {
        sendMcpToolError(sessionId, callId, "MCP service not available")
        return
    }
    val tool = mcpService.toolRegistry.getTool(toolName)
    if (tool == null) {
        sendMcpToolError(sessionId, callId, "Unknown tool: $toolName")
        return
    }
    val ctx = buildToolContext(mcpService)
    val result = tool.execute(arguments, ctx)  // EDT 调度已内置
    sendMcpToolResult(sessionId, callId, result)
}
```

### 7. `McpProjectService` 未启动 / MCP 禁用

已在 `sendMcpRegister` 中处理（section 4）：`mcpService?.isRunning != true` 时发送空 `tools` 数组，后端收到空列表 → `_unregister_ws_proxy_tools` → 清理工具。无需额外代码。

### 8. 多 IDE 窗口匹配

同一个 user 可能打开多个 IntelliJ 窗口（不同 project），连同一个 agent。`mcp_tool_call` 需要路由到正确的窗口。

**方案**：在 `mcp_register` 和 `mcp_tool_call` 中都带上 `project_path`：

```json
// mcp_register
{ "type": "mcp_register", "project_path": "/Users/zhuanz/IdeaProjects/MyApp", "tools": [...] }

// mcp_tool_call（后端选择匹配的连接）
{ "type": "mcp_tool_call", "call_id": "...", "name": "...", "arguments": {...} }
```

后端根据 agent 调用时的当前上下文（如 `cwd`/最近操作的文件路径）选择最匹配的 IDE 连接。详见后端计划第 9 节。

### 9. WebSocket 断开 + 重连

当前 `handleDisconnect` 会在 2s 后自动重连。重连后 `onOpen` 重新触发，会再次调用 `sendClientInfo` + `sendMcpRegister` — **无需额外处理**（section 4 已覆盖）。

`shutdown()` 中清理：

```kotlin
fun shutdown() {
    // 清理 MCP 引用
    mcpProjectService = null
    // ... 现有 disconnect / cancel 逻辑 ...
}
```

### 10. `McpProjectService` — 暴露 `project` 和 `projectPath`

**文件**: `src/main/kotlin/com/clawith/acp/mcp/server/McpProjectService.kt`

`project` 当前是 `private val`，外部（`ChatBackendClient.buildToolContext` 和 `sendMcpRegister`）无法访问。改为 `val`（public），并新增便捷方法：

```kotlin
class McpProjectService(val project: Project) {  // private → public
    // ... 现有字段 ...

    val toolRegistry = ToolRegistry()
    
    /** 获取 project base path（供 mcp_register 的 project_path 字段使用） */
    fun getProjectPath(): String = project.basePath ?: ""
}
```

> **注意**：`private val` → `val` 是破坏性最小的方法。如果担心耦合，可以只加 `getProjectPath()` 和 `fun getProject(): Project = project`。

### 11. `buildToolContext` 缓存优化

**文件**: `src/main/kotlin/com/clawith/acp/mcp/chat/ChatBackendClient.kt`

每次 MCP 工具调用都创建新的 `ToolExecutionContext` 实例（含 `ClawithAcpSearchOps`、`ClawithAcpHierarchyOps`、`ClawithAcpRefactorOps`）有开销。改为惰性单例：

```kotlin
// ChatBackendClient 中
private var _toolContext: ToolExecutionContext? = null

private fun buildToolContext(mcpService: McpProjectService): ToolExecutionContext {
    return _toolContext ?: object : ToolExecutionContext {
        override val project: Project = mcpService.project
        override val searchOps: ClawithAcpSearchOps = ClawithAcpSearchOps(project)
        override val hierarchyOps: ClawithAcpHierarchyOps = ClawithAcpHierarchyOps(project)
        override val refactorOps: ClawithAcpRefactorOps = ClawithAcpRefactorOps(project)
        override var buildErrorCache: List<FsBuildMessage> = emptyList()
        override fun notifyFileChanged(path: String, originalContent: String?, newContent: String, isNewFile: Boolean) {
            // TODO Phase 2: 通过 WebSocket 通知后端文件变更
        }
        override fun resolveFile(path: String, basePath: String?): VirtualFile? =
            IdeToolBase.resolveFileStatic(path, basePath ?: project.basePath)
    }.also { _toolContext = it }
}
```

> **`notifyFileChanged` 暂时 no-op**：Phase 1 跳过。ACP 路径已有 `AcpFileChangeNotifier`，MCP 路径的文件变更通知可后续补上。

---

## 消息协议定义

### Plugin → Backend: 注册本地工具

```json
{
  "type": "mcp_register",
  "url": "ws-proxy://agent-uuid",
  "project_path": "/Users/zhuanz/IdeaProjects/MyApp",
  "tools": [
    {
      "name": "find_references",
      "description": "Find all references to a symbol in the project",
      "inputSchema": {
        "type": "object",
        "properties": {
          "symbol": {"type": "string", "description": "The symbol name"},
          "file": {"type": "string", "description": "File path to search in"}
        },
        "required": ["symbol"]
      }
    }
  ]
}
```

### Backend → Plugin: 请求执行工具

```json
{
  "type": "mcp_tool_call",
  "call_id": "uuid-v4",
  "name": "find_references",
  "arguments": {
    "symbol": "MyClass",
    "file": "src/main/kotlin/MyClass.kt"
  }
}
```

### Plugin → Backend: 返回执行结果

```json
{
  "type": "mcp_tool_result",
  "call_id": "uuid-v4",
  "output": "Found 3 references:\n- Main.kt:15\n- Test.kt:42\n- ...",
  "is_error": false
}
```

---

## 修改文件汇总

| # | 文件 | 改动 |
|---|------|------|
| 1 | `ChatEvent.kt` | 新增 `McpToolCallPayload` / `McpToolResultPayload`（独立 data class，不继承 ChatEvent） |
| 2 | `ChatBackendClient.kt` | `handleMessage` 新增 `mcp_tool_call` 分支；新增 `executeMcpToolAndRespond` / `sendMcpToolResult` / `sendMcpToolError` / `buildToolContext`；`registerMcp` 发送完整 tools 列表 + `project_path`；`onOpen` 发送 `client_info` + 重连后重新注册；`handleDisconnect` / `shutdown` 清理 |
| 3 | `ChatPage.kt` | `startSessionWithAgent` 注入 `McpProjectService`；区分 MCP 启用/未启用 |
| 4 | `McpProjectService.kt` | `project` 改为 `val`（public）；新增 `getProjectPath()` |
| 5 | `ChatBackend.kt` | `registerMcp` 签名不变（通过 url 空值识别 WS 代理模式） |

> **注**：`IdeToolBase.kt` 无需修改。`execute()` 已通过 `edtAction()` → `suspendCancellableCoroutine` 自动处理 EDT 调度，无需 `requiresWriteAction` 属性。

---

## 非回归验证清单

改造必须不影响现有聊天功能 + ACP 路径（过渡期间并行运行）。

### 核心聊天功能（必须不变）

| # | 场景 | 状态 |
|---|------|------|
| 1 | 新会话 → WelcomeCard → 选择 Agent → 创建 session | 已有 |
| 2 | 用户发消息 → LLM 流式回复（Markdown 渲染） | 已有 |
| 3 | 连续多轮对话 → 历史加载正确 | 已有 |
| 4 | 切换 session tab → loadSession + connect | 已有 |
| 5 | ModelSelector / AgentSelector 正常工作 | 已有 |
| 6 | Thinking panel 折叠/展开 | 已有 |
| 7 | Tool timeline 展示（tool_call_start/done） | 已有 |
| 8 | abort 取消当前 run | 已有 |
| 9 | 连接错误 → ErrorStateView → 重连 | 已有 |
| 10 | Agent 过期 → agentExpired → 禁止重连 | 已有 |
| 11 | Token 过期 → 401 → 触发 refresh → 重连 | 已有 |

### ACP 路径（过渡期间并行）

| # | 场景 | 状态 |
|---|------|------|
| 12 | `/ws/acp` 连接正常 | 已有，不动 |
| 13 | ACP 工具调用正常 | 已有，不动 |
| 14 | ACP IDE 权限弹窗 | 已有，不动 |

### 新增 MCP 代理功能

| # | 场景 | 需要新增测试 |
|---|------|------------|
| 15 | IDE 连接 → 发送 `client_info` → `mcp_register` | ✅ |
| 16 | 后端发 `mcp_tool_call` → 插件执行 → 返回 `mcp_tool_result` | ✅ |
| 17 | 读工具（find_references）→ IO 线程执行正常 | ✅ |
| 18 | 写工具（reformat_code）→ EDT 自动切换正常 | ✅ |
| 19 | MCP 未启用 → `registerMcp` 发空列表 | ✅ |
| 20 | WebSocket 断开重连 → 重新发送 `mcp_register` | ✅ |
| 21 | shutdown → 清理 `mcpProjectService` 引用 | ✅ |
| 22 | 两个并发 `mcp_tool_call` → call_id 隔离 | ✅ |
| 23 | 未知 tool name → 返回 error | ✅ |
| 24 | 工具超时（12s）→ 返回 timeout error | ✅ |

---

## 风险与注意事项

| 风险 | 缓解措施 |
|------|---------|
| `McpProjectService.project` 是 private | 改为 `val project`（public），改动最小 |
| `buildToolContext` 中 `notifyFileChanged` 暂时 no-op | Phase 1 可接受（ACP 路径仍有 `AcpFileChangeNotifier`）；Phase 2 通过 WebSocket 通知 |
| 工具执行耗时可能阻塞 WebSocket 消息循环 | 已在独立协程中执行（`scope.launch`），12s timeout |
| 多个 session 同时调工具，资源竞争 | `ToolRegistry` 内部 `ConcurrentHashMap` + 线程池，线程安全 |
| `registerMcp` 旧的 HTTP URL 方案与新方案冲突 | 后端检测 `ws-proxy://` scheme 时走 WS 代理路径 |
| EDT 工具在 IO 线程调用 | `IdeToolBase.execute()` 已通过 `edtAction()` + `suspendCancellableCoroutine` 自动处理，无需额外改动 |
| 多 IDE 窗口时工具调用路由错误 | `project_path` 匹配 |
| `McpProjectService` 未启动 | 发空 tools 列表，后端跳过注册 |
| 重连后 MCP 工具丢失 | `onOpen` 中重新发送 `mcp_register` |
| 旧代码依赖 `McpServer` HTTP 端点 | 保留不动，等验证通过后再清理 |
| `client_info` 未发送 → 后端把前端 Web 连接当 IDE | `onOpen` 中立即发送 `{"type":"client_info","client_type":"ide_plugin"}` |


---

## 测试要点

1. `ChatBackendClientTest` — 新增 `mcp_tool_call` 消息解析 + 执行 + 回传测试
2. `ChatBackendClientIntegrationTest` (已有) — 验证端到端流程
3. 确认 ToolRegistry 的 40+ 工具都能通过 WS 代理正常执行
4. 确认超时（12s）、取消、错误场景正常
5. EDT 写操作工具在 `Dispatchers.Main` + `runWriteAction` 下正常执行
6. MCP 未启用时 `registerMcp` 发空列表，后端正确处理
7. WebSocket 断开 → 正在执行的工具调用正常取消
