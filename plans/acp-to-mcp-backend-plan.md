# Backend: `/ws/chat` MCP 工具代理改造计划

## 目标

在 `/ws/chat/{agentId}` WebSocket 中新增 MCP 工具代理能力：后端检测到 MCP 工具 URL 为 `localhost/127.0.0.1` 时，不直接 HTTP 调用，改为通过已有 WebSocket 连接向 IDE 插件发送 `mcp_tool_call` 请求，插件本地执行后通过 `mcp_tool_result` 返回结果。

## 现状

### 现有 MCP 工具执行流程

```
LLM 决定调工具
  → execute_builtin_tool_outcome()           # agent_tools.py:3200+
  → _resolve_mcp_execution_target()          # agent_tools.py:6218, 查 DB 获取 mcp_server_url
  → _execute_resolved_mcp_target_outcome()   # agent_tools.py:6322
  → MCPClient.call_tool_result(server_url)   # agent_tools.py:6374, 直接 HTTP 调用
  → ❌ server_url = http://127.0.0.1:38591/mcp 不可达（Docker/远程）
```

### 关键文件

| 文件 | 关键函数/行号 | 作用 |
|------|-------------|------|
| `api/websocket.py` | `WebSocketChatHandler` (L242), `message_loop()` (L506) | `/ws/chat` 消息循环 |
| `services/agent_tools.py` | `_resolve_mcp_execution_target()` (L6218) | 解析 MCP 工具的目标 URL |
| `services/agent_tools.py` | `_execute_resolved_mcp_target_outcome()` (L6322) | 调 MCPClient 执行 |
| `services/agent_tools.py` | `execute_builtin_tool_outcome()` (L3200+) | 工具派发入口 |
| `services/agent_runtime/` | `tool_step_service.py`, `adapter.py` | Runtime 工具执行适配层 |

---

## 目标架构

```
LLM 决定调 IDE 工具
  → execute_builtin_tool_outcome()
  → _resolve_mcp_execution_target()  // 拿到 server_url = http://127.0.0.1:38591/mcp
  → 检测 hostname in ("127.0.0.1", "localhost")
  → _execute_local_mcp_via_ws_proxy()   // 新函数：通过 WebSocket 代理
    → 找到该 agent 的活跃 WS 连接
    → 发送 {"type":"mcp_tool_call","call_id":"...","name":"...","arguments":{...}}
    → await pending future
    → 插件通过 WS 返回 {"type":"mcp_tool_result","call_id":"...","output":"...","is_error":false}
    → 解析结果，返回 ToolExecutionOutcome
```

---

## ⚠️ 架构前置问题：死锁 + 非回归约束

### 问题

当前 `/ws/chat` 的 `message_loop` 是**收消息 + 跑 LLM 合一**的：

```python
async def message_loop(self):
    while True:
        data = await self.websocket.receive_json()  # ← 收消息
        ...
        outcome = await self._run_runtime_and_stream()  # ← await 阻塞，无法收消息
```

`_run_runtime_and_stream()` 内部：LLM 决定调工具 → `execute_builtin_tool_outcome()` → `_execute_local_mcp_via_ws_proxy()` → `handler.send_mcp_tool_call()` → **await future**。

但 future 需要有人从 WebSocket 读到 `mcp_tool_result` 才能 resolve。而唯一的 reader（`message_loop`）正卡在 `_run_runtime_and_stream()` 里。**死锁。**

### 约束

**不能重构 `message_loop` 的执行模型。** 改动必须最小化，确保以下现有行为不受影响：

| 现有行为 | 风险 |
|----------|------|
| 消息顺序处理（一个接一个，不并发） | `create_task` 会导致并发执行 |
| `self.conversation` 在 run 完成后追加 | 跨 task 竞态 |
| `pending_runs` 的 `popleft` + `extend` | 跨 task 竞态 |
| abort 在 loop 顶部处理 | 分离后 abort 时机变化 |
| `_handle_cancel_packet` 取消当前 run | 需要跨 task 通知 |
| `attach_run` → `_run_runtime_and_stream` → 结果追加到 conversation | 同上 |
| welcome message（`not self.history_messages`） | 分离后可能重复发送 |
| setup failure 直接 return（不进入 loop） | 不变 |
| `WebSocketDisconnect` 异常处理 | 分离后可能在 task 中静默丢失 |
| 现有的 `_run_runtime_and_stream` 错误处理 + 日志 | 分离后错误可能不被捕获 |

### 解决方案：Stash Queue（最小改动）

**核心思路**：不改 `message_loop` 的执行模型，而是在 `send_mcp_tool_call` 内部启动**临时 reader task** 读取 WebSocket，把非 MCP 消息暂存到 `asyncio.Queue`，`message_loop` 优先消费暂存队列。

#### 改动范围

```
message_loop:   只加 4 行
send_mcp_tool_call: 新方法（~50 行）
__init__:       加 2 个新字段
cleanup:        加 1 行
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
其他所有代码:    不动
```

#### 实现

**Step 1 — `WebSocketChatHandler.__init__` 新增字段：**

```python
# MCP 工具代理
self._mcp_pending_calls: dict[str, asyncio.Future] = {}
self._mcp_stash: asyncio.Queue = asyncio.Queue()  # 临时 reader 暂存的非 MCP 消息
self._client_type: str = "web"
self._client_meta: dict = {}
self._mcp_proxy_enabled: bool = False
```

**Step 2 — `message_loop` 只加 4 行：**

只改消息获取方式，循环体其余代码**完全不动**：

```python
async def message_loop(self):
    """Core message processing loop."""
    # Send welcome message on new session (no history)
    if self.welcome_message and not self.history_messages:
        await self.websocket.send_json(
            {"type": "done", "role": "assistant", "content": self.welcome_message}
        )

    pending_runs: deque[AcceptedWebChatMessage] = deque()
    while True:
        if pending_runs:
            accepted = pending_runs.popleft()
        else:
            # ── 改动：优先消费 stash queue ──
            try:
                data = self._mcp_stash.get_nowait()
            except asyncio.QueueEmpty:
                data = await self.websocket.receive_json()
            # ── 改动结束 ──

            # ═══ 以下所有代码完全不动 ═══
            if data.get("type") == "abort":
                if self.agent_type == "openclaw":
                    continue
                await self._handle_cancel_packet(data)
                continue

            if data.get("type") == "attach_run":
                attached = await self._attach_runtime_run(data)
                if attached is None:
                    continue
                outcome, queued_messages = await self._run_runtime_and_stream(
                    attached, user_content="",
                )
                pending_runs.extend(queued_messages)
                if outcome is not None:
                    self.conversation.append(
                        {"role": "assistant", "content": outcome.content}
                    )
                continue

            # MCP 工具结果（stash queue 中可能被带过来，显式处理）
            if data.get("type") == "mcp_tool_result":
                call_id = data.get("call_id", "")
                future = self._mcp_pending_calls.pop(call_id, None)
                if future and not future.done():
                    future.set_result({
                        "output": data.get("output", ""),
                        "is_error": data.get("is_error", False),
                    })
                continue

            # MCP 注册
            if data.get("type") == "mcp_register":
                asyncio.create_task(self._register_local_mcp_tools(
                    data.get("url", ""),
                    data.get("project_path", ""),
                    data.get("tools", []),
                ))
                continue

            # 客户端类型标识
            if data.get("type") == "client_info":
                self._client_type = data.get("client_type", "web")
                self._client_meta = data.get("meta", {})
                continue

            accepted = await self._accept_client_message(data)
            if accepted is None:
                continue

        outcome, queued_messages = await self._run_runtime_and_stream(
            accepted.runtime.run,
            user_content=accepted.user_content,
        )
        pending_runs.extend(queued_messages)
        if outcome is not None:
            if not accepted.is_onboarding_trigger:
                self.conversation.append(
                    {"role": "user", "content": accepted.user_content}
                )
            self.conversation.append(
                {"role": "assistant", "content": outcome.content}
            )
            if (
                outcome.status == "completed"
                and accepted.runtime.onboarding_target_phase is not None
            ):
                await self._mark_onboarding_runtime_phase(
                    accepted.runtime.onboarding_target_phase
                )
        continue
```

> **关键**：`_run_runtime_and_stream` 仍在同一个 task 中 await，顺序处理、conversation 追加、pending_runs、abort、attach_run、onboarding — **全部不变**。只改变了"从哪取消息"这一个点。

**Step 3 — `send_mcp_tool_call`（新方法）：**

```python
async def send_mcp_tool_call(
    self, tool_name: str, arguments: dict, timeout: float = 30.0
) -> dict:
    """通过 WebSocket 向 IDE 插件发送 MCP 工具调用请求。

    启动临时 reader task 读取 WebSocket 响应，
    非 MCP 消息暂存到 _mcp_stash 队列供 message_loop 消费。
    """
    call_id = uuid.uuid4().hex[:12]
    future: asyncio.Future = asyncio.get_event_loop().create_future()
    self._mcp_pending_calls[call_id] = future

    # 临时 reader：只消费 mcp_tool_result，其他消息 stash
    async def _mcp_result_reader():
        while not future.done():
            try:
                raw = await asyncio.wait_for(
                    self.websocket.receive_json(), timeout=1.0
                )
            except asyncio.TimeoutError:
                continue
            except Exception:
                if not future.done():
                    future.set_exception(
                        ConnectionError("WebSocket read failed during MCP tool call")
                    )
                return

            msg_type = raw.get("type", "")
            if msg_type == "mcp_tool_result":
                cid = raw.get("call_id", "")
                f = self._mcp_pending_calls.pop(cid, None)
                if f and not f.done():
                    f.set_result({
                        "output": raw.get("output", ""),
                        "is_error": raw.get("is_error", False),
                    })
                if cid == call_id:
                    return  # 拿到目标结果，退出
            else:
                # 非 MCP 消息 → stash 给 message_loop
                await self._mcp_stash.put(raw)

    reader_task = asyncio.create_task(_mcp_result_reader())

    try:
        await self.websocket.send_json({
            "type": "mcp_tool_call",
            "call_id": call_id,
            "name": tool_name,
            "arguments": arguments,
        })
        result = await asyncio.wait_for(future, timeout=timeout)
        return result
    except asyncio.TimeoutError:
        self._mcp_pending_calls.pop(call_id, None)
        raise
    except Exception:
        self._mcp_pending_calls.pop(call_id, None)
        raise
    finally:
        reader_task.cancel()
        # Drain: reader 可能已经 stash 了额外消息，这些会在 message_loop
        # 下次循环时被消费（通过 get_nowait 优先读取 stash queue）
```

**Step 4 — `cleanup` 加清理 stash：**

```python
async def cleanup(self):
    # Cancel 所有未完成的 MCP tool calls
    for call_id, future in self._mcp_pending_calls.items():
        if not future.done():
            future.cancel()
    self._mcp_pending_calls.clear()

    # 清理未消费的 stash 消息（可选，断开后无所谓）
    while not self._mcp_stash.empty():
        try:
            self._mcp_stash.get_nowait()
        except asyncio.QueueEmpty:
            break
    # ... 现有清理逻辑 ...
```

### Stash Queue 方式 vs 拆分 message_loop

| 维度 | Stash Queue | 拆分 message_loop |
|------|------------|-------------------|
| `message_loop` 改动 | **4 行** | ~80 行重写 |
| 现有行为保持 | **全部不变** | 需逐一验证 10+ 条 |
| 并发执行风险 | **无**（仍然顺序） | 需要锁/semaphore |
| conversation 竞态 | **无** | 需要 `asyncio.Lock` |
| abort 处理 | **不变** | 需要跨 task 通知 |
| 错误传播 | **不变** | 需要额外捕获 |
| 回归测试范围 | 只测 MCP 代理路径 | 需全量重测 websocket.py |

---

## 非回归验证清单

以下所有场景必须在改造前后行为一致（stash queue 方式只改消息来源，不改变处理逻辑）：

### 核心聊天流程

| # | 场景 | 验证方式 |
|---|------|---------|
| 1 | 新会话 → welcome message 正确发送 | 已有测试 |
| 2 | 用户发消息 → LLM 回复 → chunk 流式推送 | 已有测试 |
| 3 | 连续多轮对话 → conversation 累积正确 | 已有测试 |
| 4 | `session_id` 参数 → 恢复已有会话 | 已有测试 |
| 5 | 无 `session_id` → 自动创建/选择 primary session | 已有测试 |

### 错误处理

| # | 场景 | 验证方式 |
|---|------|---------|
| 6 | 无效 token → 4001 关闭 | 已有测试 |
| 7 | Agent 过期 → 4003 关闭 + agent_expired 消息 | 已有测试 |
| 8 | Model 不可用 → runtime_error 消息 | 已有测试 |
| 9 | WebSocket 中途断开 → manager.disconnect 正确调用 | 已有测试 |
| 10 | `_run_runtime_and_stream` 异常 → 不崩溃，正确日志 | 已有测试 |

### Abort / Attach / Onboarding

| # | 场景 | 验证方式 |
|---|------|---------|
| 11 | 发送 `abort` → `_handle_cancel_packet` 正确取消 | 已有测试 |
| 12 | `attach_run` → 重连到正在运行的 run → 恢复流式 | 已有测试 |
| 13 | onboarding → `_mark_onboarding_runtime_phase` 调用 | 已有测试 |
| 14 | openclaw agent → abort 被跳过（continue） | 已有测试 |

### 新增 MCP 功能（仅此部分新增）

| # | 场景 | 验证方式 |
|---|------|---------|
| 15 | IDE 插件发 `mcp_register` → DB 写入 Tool + AgentTool | **新增测试** |
| 16 | IDE 插件断开 → DB 清理工具记录 | **新增测试** |
| 17 | LLM 调 IDE MCP 工具 → `send_mcp_tool_call` → 收到 `mcp_tool_result` | **新增测试** |
| 18 | LLM 调 IDE MCP 工具 → 无活跃 IDE 连接 → `mcp_ws_connection_unavailable` | **新增测试** |
| 19 | LLM 调 IDE MCP 工具 → 超时 30s → `mcp_ws_tool_timeout` | **新增测试** |
| 20 | 前端 Web 收到 IDE MCP 工具调用 → 正确报错（不应代理） | **新增测试** |
| 21 | `mcp_tool_call` 并发两个 → call_id 隔离不串 | **新增测试** |
| 22 | stash queue 中有消息时 message_loop 优先消费 | **新增测试** |

### 不受影响的功能（覆盖确认）

| # | 功能 | 原因 |
|---|------|------|
| `_run_runtime_and_stream` | 仍在同一 task 中 await，顺序不变 |
| `self.conversation` | 仍在同一 task 中修改，无竞态 |
| `pending_runs` | 仍在同一 task 中操作，无竞态 |
| `_accept_client_message` | 逻辑完全不动 |
| `_resolve_chat_session` | 逻辑完全不动 |
| `_load_models` / `_load_history` | 逻辑完全不动 |
| `_build_conversation_context` | 逻辑完全不动 |
| 远程 MCP HTTP 调用 | `_execute_resolved_mcp_target_outcome` 中 `ws-proxy://` 和 `localhost` 都不匹配 → 走原有 HTTP 路径 |

---

## 修改清单

### 1. `api/websocket.py` — 处理 `mcp_tool_result` 和 `mcp_register`

**文件**: `backend/app/api/websocket.py`

#### 1.1 在 `WebSocketChatHandler` 中新增 MCP pending call 管理

```python
class WebSocketChatHandler:
    def __init__(self, ...):
        # ... 现有字段 ...
        # MCP 工具代理: call_id → Future
        self._mcp_pending_calls: dict[str, asyncio.Future] = {}
        # 此 session 注册的本地 MCP 工具名列表
        self._local_mcp_tools: set[str] = set()
```

#### 1.2 `message_loop()` 中新增消息处理

在 `message_loop()` 的 `data = await self.websocket.receive_json()` 之后，`abort` / `attach_run` 检查之前，新增：

```python
# MCP 工具结果: 插件返回工具执行结果
if data.get("type") == "mcp_tool_result":
    call_id = data.get("call_id", "")
    future = self._mcp_pending_calls.pop(call_id, None)
    if future and not future.done():
        output = data.get("output", "")
        is_error = data.get("is_error", False)
        future.set_result({"output": output, "is_error": is_error})
    continue

# MCP 注册: 插件声明本地 IDE 工具可用
if data.get("type") == "mcp_register":
    # 可选: url 为空时走 WS 代理模式
    url = data.get("url", "")
    # 后台异步将本地工具注册到 agent
    asyncio.create_task(
        self._register_local_mcp_tools(url, data.get("tools", []))
    )
    continue
```

#### 1.3 新增 MCP 工具代理方法

```python
async def send_mcp_tool_call(
    self, tool_name: str, arguments: dict, timeout: float = 30.0
) -> dict:
    """通过 WebSocket 向 IDE 插件发送 MCP 工具调用请求，等待结果返回。

    返回: {"output": str, "is_error": bool}
    Raises: asyncio.TimeoutError, ConnectionError
    """
    call_id = uuid.uuid4().hex[:12]
    future: asyncio.Future = asyncio.get_event_loop().create_future()
    self._mcp_pending_calls[call_id] = future

    try:
        await self.websocket.send_json({
            "type": "mcp_tool_call",
            "call_id": call_id,
            "name": tool_name,
            "arguments": arguments,
        })
        result = await asyncio.wait_for(future, timeout=timeout)
        return result
    except asyncio.TimeoutError:
        self._mcp_pending_calls.pop(call_id, None)
        raise
    except Exception:
        self._mcp_pending_calls.pop(call_id, None)
        raise

async def _register_local_mcp_tools(self, url: str, tool_names: list[str]):
    """将插件本地 IDE 工具注册为此 agent 的 MCP 工具。

    如果 url 为空，标记为 WebSocket 代理模式（无需 HTTP URL）。
    """
    if not self.agent_id:
        return
    self._local_mcp_tools.update(tool_names)
    # 将工具写入 AgentTool + Tool 表，server_url 设为空或特殊标记
    # 使得 _resolve_mcp_execution_target 能识别这是 WS 代理工具
    ...
```

#### 1.4 `cleanup()` 中清理 pending calls

```python
async def cleanup(self):
    # 取消所有未完成的 MCP tool calls
    for call_id, future in self._mcp_pending_calls.items():
        if not future.done():
            future.cancel()
    self._mcp_pending_calls.clear()
    # ... 现有清理逻辑 ...
```

### 2. `services/agent_tools.py` — 新增 WS 代理执行路径

**文件**: `backend/app/services/agent_tools.py`

#### 2.1 在 `_execute_resolved_mcp_target_outcome()` 中新增 WS 代理分支

在 L6374（`client = MCPClient(server_url, ...)`）之前插入判断：

```python
async def _execute_resolved_mcp_target_outcome(
    target: dict,
    arguments: dict,
    *,
    agent_id,
) -> ToolExecutionOutcome:
    # ... 现有 error/unavailable 检查保持不变 ...

    from urllib.parse import urlparse

    server_url = str(target["server_url"])
    hostname = (urlparse(server_url).hostname or "").lower()

    # ── 新增: 本地 MCP 工具通过 WebSocket 代理执行 ──
    if hostname in ("127.0.0.1", "localhost", "::1"):
        return await _execute_local_mcp_via_ws_proxy(
            target, arguments, agent_id
        )
    # ── 现有逻辑: 远程 MCP 直接 HTTP 调用 ──

    if hostname.endswith(".run.tools"):
        return await _execute_via_smithery_connect_outcome(...)

    # ... 现有 MCPClient HTTP 调用逻辑保持不变 ...
```

#### 2.2 新增 `_execute_local_mcp_via_ws_proxy()` 函数

```python
async def _execute_local_mcp_via_ws_proxy(
    target: dict,
    arguments: dict,
    agent_id,
) -> ToolExecutionOutcome:
    """通过 /ws/chat WebSocket 代理执行本地 IDE MCP 工具。

    适用场景: 后端在 Docker/远程，插件 MCP Server 只在本机可达。
    """
    from app.api.websocket import manager as ws_manager

    full_name = str(target["full_name"])
    raw_name = str(target["raw_name"])
    agent_id_str = str(agent_id)

    # 查找该 agent 的活跃 WebSocket 连接
    connections = ws_manager.get_connections(agent_id_str)
    if not connections:
        return _typed_failure(
            f"No active IDE connection for agent {agent_id_str}. "
            "Please open the Clawith plugin in IntelliJ and connect.",
            "mcp_ws_connection_unavailable",
        )

    # 选第一个活跃连接（通常一个 agent 只有一个 IDE session）
    ws_handler = None
    for conn_info in connections:
        handler = getattr(conn_info, 'handler', None)
        if handler and hasattr(handler, 'send_mcp_tool_call'):
            ws_handler = handler
            break

    if ws_handler is None:
        return _typed_failure(
            "IDE connection does not support MCP tool proxy.",
            "mcp_ws_proxy_unsupported",
        )

    try:
        result = await ws_handler.send_mcp_tool_call(
            tool_name=raw_name,
            arguments=arguments,
            timeout=30.0,
        )
    except asyncio.TimeoutError:
        return _typed_failure(
            f"IDE tool {raw_name} timed out after 30s.",
            "mcp_ws_tool_timeout",
        )
    except Exception as exc:
        return _typed_unknown(
            f"IDE tool {raw_name} proxy error: {exc}",
            "mcp_ws_proxy_error",
        )

    output = str(result.get("output", ""))
    is_error = bool(result.get("is_error", False))

    if is_error:
        return _typed_failure(output, "mcp_ws_tool_error")

    return ToolExecutionOutcome(
        status="completed",
        result_summary=output,
        result_ref=None,
        error_code=None,
        retryable=False,
        metadata={
            "execution_mode": "ws_proxy",
            "raw_tool_name": raw_name,
        },
    )
```

### 3. `api/websocket.py` — ConnectionManager 增强

#### 3.1 支持按 agent_id 查找连接并获取 handler

```python
# 在 ConnectionManager 中新增
def get_connections(self, agent_id: str) -> list:
    """获取指定 agent 的所有活跃连接信息。"""
    return list(self.active_connections.get(agent_id, {}).values())

def get_handler(self, agent_id: str) -> list:
    """获取指定 agent 的所有 WebSocketChatHandler 实例。"""
    handlers = []
    for conn in self.get_connections(agent_id):
        handler = getattr(conn, '_handler', None)
        if handler:
            handlers.append(handler)
    return handlers
```

#### 3.2 connect() 时关联 handler

在 `WebSocketChatHandler.setup()` 的 `manager.connect()` 调用之后，将 handler 关联到连接：

```python
# websocket.py setup() 中
await manager.connect(agent_id_str, self.websocket, self.conv_id, str(user_id))
# 新增: 将 handler 关联到连接，供 MCP 代理查找
manager.set_handler(agent_id_str, self.websocket, self)
```

### 4. 连接跟踪优化

#### 4.1 服务端连接表

当前 `ConnectionManager` 使用 `dict[str, dict[WebSocket, ...]]` 管理连接。需要扩展以支持 handler 注入：

```python
class ConnectionManager:
    def __init__(self):
        # agent_id → {websocket: {"handler": WebSocketChatHandler, ...}}
        self.active_connections: dict[str, dict] = {}
        self._lock = asyncio.Lock()

    async def connect(self, agent_id: str, ws: WebSocket, conv_id: str, user_id: str):
        async with self._lock:
            if agent_id not in self.active_connections:
                self.active_connections[agent_id] = {}
            self.active_connections[agent_id][ws] = {
                "conv_id": conv_id,
                "user_id": user_id,
                "handler": None,  # 稍后由 setup() 设置
            }

    def set_handler(self, agent_id: str, ws: WebSocket, handler):
        """关联 WebSocketChatHandler 到连接，供 MCP 代理使用。"""
        conn = self.active_connections.get(agent_id, {}).get(ws)
        if conn:
            conn["handler"] = handler

    def find_handler(self, agent_id: str):
        """查找指定 agent 的第一个有 handler 的活跃连接。"""
        for conn in self.active_connections.get(agent_id, {}).values():
            if conn.get("handler"):
                return conn["handler"]
        return None
```

---

## 执行流程对比

### Before（HTTP 直接调用 — 远程不可达）

```
agent_tools.py:_execute_resolved_mcp_target_outcome()
  → MCPClient(server_url="http://127.0.0.1:38591/mcp")
  → httpx.post("http://127.0.0.1:38591/mcp", ...)
  → ❌ Connection refused (Docker 容器里没有这个端口)
```

### After（WS 代理 — 始终可达）

```
agent_tools.py:_execute_resolved_mcp_target_outcome()
  → hostname == "127.0.0.1" → _execute_local_mcp_via_ws_proxy()
  → ws_manager.find_handler(agent_id) → WebSocketChatHandler
  → handler.send_mcp_tool_call("find_references", {...})
  → WebSocket → {"type":"mcp_tool_call","call_id":"abc","name":"find_references",...}
  → IDE Plugin 收到 → ToolRegistry.execute() → 本地执行
  → WebSocket ← {"type":"mcp_tool_result","call_id":"abc","output":"Found 3 refs...","is_error":false}
  → future.set_result(...) → 返回 ToolExecutionOutcome
  → ✅ LLM 收到工具结果
```

---

## 关键设计决策

| 决策 | 选择 | 理由 |
|------|------|------|
| 如何识别"本地" MCP 工具 | `server_url` scheme 为 `ws-proxy://` 或 hostname 为 `127.0.0.1`/`localhost`/`::1` | 双重判断：`ws-proxy://` 是插件注册时写入的明确标记，hostname 判断作为兜底 |
| WS 代理超时 | 30s | IDE 工具通常 < 10s，30s 留足余量 |
| 多连接选择 | 按 `project_path` 匹配，无匹配时取第一个 | 优先匹配 IDE project 与 agent 上下文的 cwd |
| 工具注册方式 | `mcp_register` 消息 → upsert Tool + AgentTool 表，`mcp_server_url="ws-proxy://{agent_id}"` | 明确标记 WS 代理模式，查 DB 即可识别 |
| 错误处理 | `_typed_failure` + 明确错误码 | 给 LLM 可操作的错误信息 |
| 禁用并发 | 同一 session 只允许一个 in-flight prompt | 避免多 LLM run 并发时的 mcp_tool_call/call_id 混乱 |

---

## 补充修改

### 5. 区分 IDE 连接和前端连接

`/ws/chat` 同时服务前端 Web 和 IDE 插件（`ChatBackendClient`）。`_execute_local_mcp_via_ws_proxy` 查找活跃 WS 连接时，**不能**把前端的 WebSocket 当 IDE 连接用。

#### 5.1 插件声明客户端类型

插件连接后第一时间发：

```json
{"type": "client_info", "client_type": "ide_plugin", "meta": {"project_path": "/path/to/project"}}
```

前端不发此消息 → `_client_type` 默认为 `"web"`。

#### 5.2 `WebSocketChatHandler` 新增字段

```python
class WebSocketChatHandler:
    def __init__(self, ...):
        # ...
        self._client_type: str = "web"       # "web" | "ide_plugin"
        self._client_meta: dict = {}         # project_path, ide_version 等
        self._mcp_proxy_enabled: bool = False  # 收到非空 mcp_register 后置 true
```

#### 5.3 `ConnectionManager.find_handler` 只返回 IDE 连接

```python
def find_mcp_proxy_handler(self, agent_id: str, project_path: str = ""):
    """查找可处理 MCP 工具代理的 IDE 连接。

    优先匹配 project_path，无匹配时取第一个 IDE 连接。
    前端 Web 连接的 handler._mcp_proxy_enabled=False，不会被选中。
    """
    candidates = []
    for conn in self.active_connections.get(agent_id, {}).values():
        handler = conn.get("handler")
        if handler and getattr(handler, '_mcp_proxy_enabled', False):
            conn_path = handler._client_meta.get("project_path", "")
            candidates.append((conn_path, handler))

    if not candidates:
        return None

    # 优先匹配 project_path
    if project_path:
        for conn_path, handler in candidates:
            if conn_path == project_path:
                return handler

    # fallback: 返回第一个
    return candidates[0][1]
```

### 6. `_register_local_mcp_tools` DB 写入实现

这是整个方案中最复杂的 DB 操作。需要将插件发来的 40+ 工具写入 `Tool` + `AgentTool` 表。

#### 6.1 方案：先清再写 + upsert

```python
async def _register_local_mcp_tools(
    self, url: str, project_path: str, tools: list[dict]
):
    agent_id = self.agent_id
    if not agent_id:
        return

    self._client_meta["project_path"] = project_path

    from app.models.tool import Tool, AgentTool

    proxy_url = f"ws-proxy://{agent_id}"

    async with async_session() as db:
        async with db.begin():
            # 1. 先清空此 agent 所有旧的 ws-proxy 工具（处理工具减少/更名的情况）
            old_result = await db.execute(
                select(Tool.id).where(Tool.mcp_server_url == proxy_url)
            )
            old_ids = [r[0] for r in old_result.fetchall()]
            if old_ids:
                await db.execute(
                    AgentTool.__table__.delete().where(
                        AgentTool.agent_id == agent_id,
                        AgentTool.tool_id.in_(old_ids),
                    )
                )
                await db.execute(
                    Tool.__table__.delete().where(Tool.id.in_(old_ids))
                )

            if not tools:
                self._mcp_proxy_enabled = False
                logger.info(f"[MCP] Cleared all WS-proxy tools for agent={agent_id}")
                return

            self._mcp_proxy_enabled = True

            # 2. 批量 upsert 新工具列表
            for tool_def in tools:
                name = tool_def["name"]
                tool_row = Tool(
                    name=name,
                    type="mcp",
                    source="agent",
                    description=tool_def.get("description", ""),
                    parameters_schema=tool_def.get("inputSchema"),
                    mcp_server_url=proxy_url,
                    mcp_server_name="IDE Plugin",
                    mcp_tool_name=name,
                    enabled=True,
                    tenant_id=self.agent.tenant_id,
                )
                db.add(tool_row)
                await db.flush()  # 获取 tool_row.id

                db.add(AgentTool(
                    agent_id=agent_id,
                    tool_id=tool_row.id,
                    enabled=True,
                ))

    logger.info(
        f"[MCP] Registered {len(tools)} WS-proxy tools for agent={agent_id}"
    )
```

> **关键改进**：先删后写（`DELETE` + `INSERT`）比 upsert 更简单可靠。旧工具列表中的工具会被删除，新列表全部写入。避免工具更名、减少时的残留问题。

#### 6.2 `_resolve_mcp_execution_target` 适配

在原有函数中新增 `ws-proxy://` 识别：

```python
async def _resolve_mcp_execution_target(tool_name, agent_id, ...):
    # ... 现有逻辑 ...
    server_url = str(tool.mcp_server_url or "").strip()
    parsed = urlparse(server_url)

    # WS 代理模式：不检查 HTTP scheme
    if parsed.scheme == "ws-proxy":
        raw_name = str(tool.mcp_tool_name or "").strip()
        return {
            "full_name": str(tool.name),
            "raw_name": raw_name or str(tool.name),
            "server_url": server_url,  # ws-proxy://{agent_id}
            "proxy_mode": True,        # ← 标记
            "server_name": str(tool.mcp_server_name or ""),
            "config": merged_config,
            "async_completion": trusted_async_completion,
        }

    # 现有 HTTP/HTTPS 逻辑不变
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return {...}
    ...
```

#### 6.3 清理函数

```python
async def _unregister_ws_proxy_tools(agent_id: uuid.UUID):
    """删除指定 agent 的所有 WS 代理工具。"""
    from app.models.tool import Tool, AgentTool

    async with async_session() as db:
        async with db.begin():
            proxy_url = f"ws-proxy://{agent_id}"
            # 查找所有 ws-proxy 工具
            result = await db.execute(
                select(Tool.id).where(Tool.mcp_server_url == proxy_url)
            )
            tool_ids = [r[0] for r in result.fetchall()]
            if tool_ids:
                # 删除 AgentTool 关联
                await db.execute(
                    AgentTool.__table__.delete().where(
                        AgentTool.agent_id == agent_id,
                        AgentTool.tool_id.in_(tool_ids),
                    )
                )
                # 删除 Tool 本身
                await db.execute(
                    Tool.__table__.delete().where(Tool.id.in_(tool_ids))
                )
            logger.info(
                f"[MCP] Unregistered {len(tool_ids)} WS-proxy tools for agent={agent_id}"
            )
```

### 7. 多 IDE 窗口匹配

同一 user 可能开多个 IntelliJ 窗口（不同 project），连同一个 agent。需要根据上下文选择正确的连接。

#### 7.1 `_execute_local_mcp_via_ws_proxy` 传入 project_path

```python
async def _execute_local_mcp_via_ws_proxy(target, arguments, agent_id):
    from app.api.websocket import manager as ws_manager

    agent_id_str = str(agent_id)
    # 从工具执行上下文中获取可能的 project_path
    # 可选来源：agent 最近操作的 workspace path、session 的 cwd 等
    project_path = arguments.get("_project_path", "")  # 或从 session metadata 获取

    handler = ws_manager.find_mcp_proxy_handler(agent_id_str, project_path)
    if handler is None:
        return _typed_failure(
            "No active IDE connection. Open the Clawith plugin in IntelliJ.",
            "mcp_ws_connection_unavailable",
        )

    result = await handler.send_mcp_tool_call(
        tool_name=target["raw_name"],
        arguments=arguments,
        timeout=30.0,
    )
    ...
```

### 8. WebSocket 断开时的工具清理

插件 WebSocket 断开时，需要清理两部分：
- 注册的动态 MCP 工具从 DB 删除
- 未完成的 `_mcp_pending_calls` cancel

#### 8.1 `WebSocketChatHandler.cleanup()` 增强

```python
async def cleanup(self):
    # 1. Cancel 所有未完成的 MCP tool calls
    for call_id, future in self._mcp_pending_calls.items():
        if not future.done():
            future.cancel()
    self._mcp_pending_calls.clear()

    # 2. 如果此连接注册了 MCP 工具，删除 DB 记录
    if self._mcp_proxy_enabled and self.agent_id:
        try:
            await _unregister_ws_proxy_tools(self.agent_id)
        except Exception:
            logger.exception("[MCP] Failed to unregister WS-proxy tools on cleanup")

    # 3. 从 ConnectionManager 移除
    await manager.disconnect(str(self.agent_id), self.websocket)
```

### 9. 与现有 ACP 并行共存

切换期间 ACP（`/ws/acp`）和 MCP（`/ws/chat` + WS proxy）可能同时运行。需要避免：

#### 9.1 工具名冲突

ACP 的 `tool_bridge.py` 和 MCP 的 `mcp_register` 会注册同名工具（如 `find_references`、`read_file` 等）。后端通过 `mcp_server_url` 区分：

- ACP 工具：在 agent 创建/配置时注册，`mcp_server_url` 为正常 HTTP URL 或空
- MCP WS 代理工具：`mcp_server_url = "ws-proxy://{agent_id}"`

两者可以共存，LLM 会看到两份工具（功能相同但调用路径不同）。**推荐在验证阶段用 feature flag 只启用一种**：

```python
# 环境变量或 Agent config
if agent.config.get("use_ws_proxy_mcp"):
    # 优先走 WS 代理路径
    ...
else:
    # 走 ACP 路径（现有逻辑不动）
    ...
```

#### 9.2 前端 Web 不受影响

前端 `/ws/chat` 连接 `_client_type="web"`，`_mcp_proxy_enabled=False`，`find_mcp_proxy_handler` 会跳过，IDE MCP 工具调用返回 `mcp_ws_connection_unavailable` 错误给 LLM — 这是合理的，因为前端不能执行 IDE 工具。

---

## 修改文件汇总

| # | 文件 | 改动 |
|---|------|------|
| 1 | `api/websocket.py` | `message_loop`：消息来源改为优先消费 `_mcp_stash` queue（**4 行**）；新增 `mcp_tool_result` / `mcp_register` / `client_info` 处理；`send_mcp_tool_call`（含临时 reader task）；`_register_local_mcp_tools`；`_unregister_ws_proxy_tools`；`WebSocketChatHandler.__init__` 新增 `_mcp_pending_calls` / `_mcp_stash` / `_client_type` / `_client_meta` / `_mcp_proxy_enabled`；`cleanup` 增强 |
| 2 | `services/agent_tools.py` | `_execute_resolved_mcp_target_outcome` 新增 `hostname` 判断 + `ws-proxy://` 检测 → `_execute_local_mcp_via_ws_proxy`；`_resolve_mcp_execution_target` 新增 `ws-proxy://` scheme 处理；`_unregister_ws_proxy_tools` |
| 3 | `api/websocket.py` | `ConnectionManager` 新增 `find_mcp_proxy_handler` / `set_handler`；`connect` 支持 handler 关联 |

> **核心改动量**：`message_loop` 只改 4 行（消息获取方式），其余全是**新增**代码路径。现有功能不受影响。详见[非回归验证清单](#非回归验证清单)。

---

## 测试要点

1. **单元测试**: `_execute_local_mcp_via_ws_proxy` 的 mock WS handler
2. **集成测试**: WebSocket 连接 → mcp_tool_call → mcp_tool_result 完整链路
3. **异常场景**:
   - 无活跃 IDE 连接 → 返回 `mcp_ws_connection_unavailable`
   - WS 断开 mid-call → future.cancel → 返回错误
   - 工具执行超时 → `mcp_ws_tool_timeout`
   - 多个 session 并发调工具 → call_id 隔离
4. **兼容性**: 远程 MCP URL（非 localhost）仍走 HTTP 直接调用，保持不变
5. **性能**: WS 代理比 HTTP 直接调用多一跳（插件 ↔ 后端），延迟增加 < 5ms（同机回环）

---

## 后续清理（Phase 2）

验证通过后可以移除：

1. `plugins/clawith_acp/` 整个目录 — ACP 协议不再需要
2. `main.py` 中 `acp_router` 注册 — 关闭 `/ws/acp` 端点
3. 插件端 `McpServer.kt` HTTP 服务器 — IDE 工具不再通过 HTTP 暴露
4. 插件端 `ClawithAcpClient` 等 ACP 残留代码
