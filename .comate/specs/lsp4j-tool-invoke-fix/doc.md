# LSP4J 工具调用协议修复

## 问题场景

LSP4J 连接后存在 3 个运行时问题：
1. **插件不显示思考过程和工具调用过程** — 插件聊天面板看不到工具调用的进度卡片
2. **插件没有走智能体路径** — 代码比对、同意/拒绝 UI 不可见
3. **智能体一直回复"看不到插件本地项目"** — `tool/invoke` 发出后 120s 超时，IDE 无响应

## 根因分析

基于插件源码 `/Users/shubinzhang/Downloads/demo-new` 深度分析，定位到以下根因：

### P0: `tool/invoke` 请求超时（根因 #1 — requestId 不匹配）

**代码位置**: `jsonrpc_router.py:730`

```python
# 当前代码 — 生成了新的 UUID 作为 requestId
request_id = str(uuid.uuid4())
```

**问题**: 插件的 `LanguageClientImpl.toolInvoke()` (line 1449) 通过 `REQUEST_TO_PROJECT[request.getRequestId()]` 查找 Project 对象。该 map 在 `CosyServiceImpl.chatAsk()` (line 197) 中以 `chat/ask` 的 `requestId` 为 key 注册。由于 `invoke_tool_on_ide` 生成了新 UUID，map 查找必然失败，只能走 `ProjectUtils.getActiveProject()` 回退路径。

**影响**: 虽然回退路径理论上可工作，但 120s 超时说明 IDE 未响应 `tool/invoke` 请求。可能与 requestId 不匹配导致的 LSP4J 框架路由问题相关。

**修复**: 使用 `self._current_request_id`（chat/ask 的 requestId），确保 `REQUEST_TO_PROJECT` 查找成功。

### P0: 缺少 `tool/call/sync` 通知（根因 #2 — 工具调用 UI 不可见）

**插件协议**: 插件的 `ChatToolEventProcessor` 是工具调用 UI 的核心。它通过 `toolCallSync()` 方法接收 `ToolCallSyncResult` 事件，渲染工具调用卡片（进度、参数、结果、审批按钮）。

**当前缺陷**: Clawith 从未发送 `tool/call/sync` 通知，导致：
- 工具调用过程不可见（无卡片、无进度）
- 审批 UI 不显示（`BaseToolCallHandler` 在 `toolCallSync` 事件触发后才渲染同意/拒绝按钮）
- `tool/call/approve` 流程无法启动（用户没有审批入口）

**`toolCallSync()` 的关键约束**（`LanguageClientImpl.java:1500-1522`）:
```java
if (null != result && null != result.getProjectPath()) {
    Project project = ProjectUtils.getProjectByPath(result.getProjectPath());
    if (project == null) {
        log.warn("toolCallSync project is null, projectPath=" + result.getProjectPath());
    } else {
        ChatToolEventProcessor.INSTANCE.offerEvent(result);
    }
} else {
    log.warn("Invalid toolCallSync: " + result);
}
```

**`projectPath` 必须非空**，否则事件被静默丢弃。需要从 `initialize` 请求的 `rootUri` 中提取。

### P1: `chat/answer`/`chat/think`/`chat/finish` 以通知方式发送

**插件定义**: `LanguageClient.java` 中这些方法标注为 `@JsonRequest`（期望收到带 `id` 的请求并返回响应）。但 Clawith 使用 `_send_notification` 发送（无 `id`，不期望响应）。

**当前状态**: 功能正常 — LSP4J 框架对通知仍会调用对应方法（只是不等待 CompletableFuture 完成）。保持现状，暂不修改（改为请求需处理大量响应匹配，收益不大）。

## 修复方案

### Fix 1: 修复 `invoke_tool_on_ide` 的 requestId 传播

**文件**: `backend/app/plugins/clawith_lsp4j/jsonrpc_router.py`

**修改位置**: `invoke_tool_on_ide()` 方法 (line 729-730)

```python
# 修改前
request_id = str(uuid.uuid4())

# 修改后 — 优先使用 chat/ask 的 requestId
request_id = self._current_request_id or str(uuid.uuid4())
```

**效果**: `REQUEST_TO_PROJECT[requestId]` 查找成功，IDE 直接获得正确的 Project 对象，走 `CompletableFuture.supplyAsync()` 异步路径（而非 `invokeLater` EDT 回退路径），减少执行延迟。

### Fix 2: 从 `initialize` 请求提取并存储 `projectPath`

**文件**: `backend/app/plugins/clawith_lsp4j/jsonrpc_router.py`

**修改 1**: `__init__()` 增加实例变量

```python
self._project_path: str = ""  # 从 initialize rootUri 提取
```

**修改 2**: `_handle_initialize()` 提取 rootUri

```python
async def _handle_initialize(self, params: dict, msg_id: Any) -> None:
    # 提取 projectPath
    root_uri = params.get("rootUri", "")
    if root_uri:
        # rootUri 格式: "file:///path/to/project"
        # 提取路径部分作为 projectPath
        if root_uri.startswith("file://"):
            self._project_path = root_uri[7:]  # 去掉 "file://" 前缀
        else:
            self._project_path = root_uri
        logger.info("[LSP4J-LIFE] projectPath from rootUri: {}", self._project_path)
    # ...原有逻辑
```

### Fix 3: 添加 `tool/call/sync` 通知流程

**文件**: `backend/app/plugins/clawith_lsp4j/jsonrpc_router.py`

**修改 1**: 新增 `_send_tool_call_sync()` 方法

```python
async def _send_tool_call_sync(
    self, session_id: str | None, request_id: str,
    tool_call_id: str, tool_name: str, status: str,
    parameters: dict | None = None, results: list | None = None,
    error_code: str = "", error_msg: str = "",
) -> None:
    """发送 tool/call/sync 通知（ToolCallSyncResult 格式）。

    插件的 ChatToolEventProcessor 通过此通知渲染工具调用卡片。
    status 取值：INIT, PENDING, RUNNING, FINISHED, ERROR, REQUEST_FINISHED, CANCELLED
    """
    sync_result = {
        "sessionId": session_id or "",
        "requestId": request_id,
        "projectPath": self._project_path,
        "toolCallId": tool_call_id,
        "toolCallStatus": status,
        "parameters": parameters or {},
        "results": results or [],
        "errorCode": error_code,
        "errorMsg": error_msg,
    }
    logger.debug("[LSP4J-TOOL] toolCallSync: toolCallId={} status={} name={}", tool_call_id, status, tool_name)
    await self._send_notification("tool/call/sync", sync_result)
```

**修改 2**: `invoke_tool_on_ide()` 中添加 sync 通知

在发送 `tool/invoke` 前，发送 `RUNNING` 状态通知：
```python
# 发送工具调用开始通知
await self._send_tool_call_sync(
    self._session_id, request_id, tool_call_id, tool_name,
    status="RUNNING", parameters=arguments,
)
```

在工具执行完成后，发送 `FINISHED`/`ERROR` 状态通知：
```python
try:
    result = await asyncio.wait_for(tool_future, timeout=timeout)
    # 工具执行完成通知
    await self._send_tool_call_sync(
        self._session_id, request_id, tool_call_id, tool_name,
        status="FINISHED", parameters=arguments,
        results=[{"content": result[:500]}] if result else [],
    )
    return result
except asyncio.TimeoutError:
    # 工具执行超时通知
    await self._send_tool_call_sync(
        self._session_id, request_id, tool_call_id, tool_name,
        status="ERROR", parameters=arguments,
        error_code="TIMEOUT", error_msg=f"工具 {tool_name} 执行超时（{timeout}s）",
    )
    return f"[超时] 工具 {tool_name} 执行超时（{timeout}s）"
```

**修改 3**: `_handle_chat_ask()` 结束时发送 `REQUEST_FINISHED`

在 `_handle_chat_ask()` 的 chat/finish 之后，发送 `REQUEST_FINISHED` 通知清理插件端的工具调用事件队列：
```python
# 通知插件工具调用流程结束
if self._project_path:
    await self._send_tool_call_sync(
        session_id, request_id, "", "",
        status="REQUEST_FINISHED",
    )
```

### Fix 4: 文件修改工具的审批流程

**文件**: `backend/app/plugins/clawith_lsp4j/jsonrpc_router.py`

**当前**: `_handle_tool_call_approve()` 自动批准所有工具调用（MVP 策略）。

**修改**: 保留自动批准逻辑，但在 `invoke_tool_on_ide()` 中为文件修改工具发送 `PENDING` 状态的 `toolCallSync` 通知，使插件渲染审批 UI。即使服务端自动批准，插件也需要 `PENDING` → `FINISHED` 状态转换来正确渲染卡片。

具体方案：
- 所有工具先发送 `PENDING` 状态（触发审批 UI）
- 然后自动批准（`_handle_tool_call_approve` 已经实现）
- 再发送 `RUNNING` → `FINISHED`/`ERROR`

### Fix 5: 增强诊断日志

**文件**: `backend/app/plugins/clawith_lsp4j/jsonrpc_router.py`

**修改 1**: `invoke_tool_on_ide()` 中记录完整请求消息（debug 级别）

```python
logger.debug("[LSP4J-TOOL] tool/invoke 完整请求: tool={} rpcId={} requestId={} callId={}",
             tool_name, rpc_id, request_id, tool_call_id[:8])
```

**修改 2**: `_handle_response()` 中记录未匹配响应的详细信息

```python
logger.warning("[LSP4J-TOOL] 收到未匹配的响应: id={} pending_ids={}",
               msg_id, list(self._pending_responses.keys()))
```

## 受影响的文件

| 文件 | 修改类型 | 受影响的函数 |
|------|---------|------------|
| `backend/app/plugins/clawith_lsp4j/jsonrpc_router.py` | 修改 | `__init__`, `_handle_initialize`, `invoke_tool_on_ide`, `_handle_chat_ask`, `_handle_response` |
| `backend/app/plugins/clawith_lsp4j/jsonrpc_router.py` | 新增 | `_send_tool_call_sync` |

## 数据流路径

### 修复后的完整工具调用流程

```
1. IDE → chat/ask(requestId=X)
   → CosyServiceImpl: REQUEST_TO_PROJECT[X] = project
   
2. Clawith → tool/call/sync(status=PENDING, requestId=X, projectPath=...)
   → ChatToolEventProcessor: 注册 ToolPanel，渲染审批 UI
   
3. IDE → tool/call/approve(approval=true)
   → Clawith: _handle_tool_call_approve → 自动批准
   
4. Clawith → tool/call/sync(status=RUNNING, requestId=X, ...)
   → ChatToolEventProcessor: 更新 ToolPanel 状态为"执行中"
   
5. Clawith → tool/invoke(requestId=X, ...)  ← 现在用 chat/ask 的 requestId！
   → LanguageClientImpl: REQUEST_TO_PROJECT[X] → 找到 project → 执行工具
   → ToolInvokeResponse → JSON-RPC 响应
   
6. Clawith → tool/call/sync(status=FINISHED, requestId=X, results=[...])
   → ChatToolEventProcessor: 更新 ToolPanel 状态为"已完成"
   
7. Clawith → chat/finish
8. Clawith → tool/call/sync(status=REQUEST_FINISHED, requestId=X)
   → ChatToolEventProcessor: 清理事件队列
```

## 边界条件和异常处理

1. **`projectPath` 为空**: 如果 `initialize` 请求不包含 `rootUri`，`_project_path` 为空字符串。`toolCallSync` 会被插件丢弃（`projectPath == null` 检查）。此时工具调用仍可执行（`tool/invoke` 不依赖 `projectPath`），但 UI 不可见。应记录 warning 日志。

2. **`_current_request_id` 为 None**: 如果在非 `chat/ask` 上下文中调用 `invoke_tool_on_ide`（理论上不会发生，因为只在 `call_llm` 回调中触发），回退到生成新 UUID。

3. **`tool/invoke` 仍然超时**: 如果修复 requestId 后仍超时，说明问题在 LSP4J 消息路由层。需要检查：
   - IDE 日志中是否有 `toolInvoke` 调用记录
   - LSP4J 框架的 `validateMessages(true)` 是否拒绝了消息
   - 尝试将 `async: False` 改为 `async: True`，走 `tool/invokeResult` 回传路径

4. **并发工具调用**: `call_llm` 可能同时调用多个工具。每个工具有独立的 `toolCallId`，在 `_pending_tools` 和 `_pending_responses` 中分别注册。`_send_tool_call_sync` 使用独立的 `toolCallId`，不会冲突。

## 预期结果

1. `tool/invoke` 使用 chat/ask 的 requestId → `REQUEST_TO_PROJECT` 查找成功 → 走异步路径 → 响应更快
2. 插件收到 `tool/call/sync` 事件 → 渲染工具调用进度卡片和审批 UI
3. 文件修改工具显示"同意/拒绝"按钮（服务端自动批准，但 UI 正确渲染）
4. `REQUEST_FINISHED` 通知清理插件端的事件队列，防止内存泄漏
5. 如果 `tool/invoke` 仍然超时，增强的诊断日志可定位具体原因
