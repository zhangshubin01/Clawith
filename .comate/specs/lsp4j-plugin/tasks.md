# 通义灵码 LSP4J 插件实现任务

- [x] Task 1: 创建插件脚手架（plugin.json + __init__.py + context.py）
    - 1.1: 创建 `backend/app/plugins/clawith_lsp4j/` 目录和 `plugin.json`（参考 ACP plugin.json 格式）
    - 1.2: 创建 `context.py`（6 个 ContextVar + `_active_routers` 映射表，可变类型 default=None）
    - 1.3: 创建 `__init__.py`（ClawithLsp4jPlugin 子类，register() 中调用 install_lsp4j_tool_hooks + include_router）
    - 1.4: 验证插件能被 `load_plugins()` 正确加载（无导入错误）

- [x] Task 2: 实现 LSP Base Protocol 解析器（lsp_protocol.py）
    - 2.1: 实现 `LSPBaseProtocolParser.__init__`（字节缓冲区 `_buffer_bytes = b""`）
    - 2.2: 实现 `read_message`（按字节查找 `\r\n\r\n`、解析 Content-Length、按字节提取 body、JSON 解析）
    - 2.3: 实现 `format_message`（`@staticmethod`，UTF-8 字节长度计算）
    - 2.4: 实现 `_parse_content_length`（`@staticmethod`，正则匹配）
    - 2.5: 验证：含中文消息的完整解析 + 分帧处理（多条消息粘包）

- [x] Task 3: 实现 JSON-RPC 路由器核心（jsonrpc_router.py — 生命周期 + Chat + 消息发送）
    - 3.1: 定义模块级变量（`_lsp4j_background_tasks: set[asyncio.Task] = set()`）
    - 3.2: 实现 `JSONRPCRouter.__init__`（websocket, user_id, agent_id, _session_id, _request_id 计数器）
    - 3.3: 实现 `route()` 方法（响应判断 → method 路由 → 核心方法分派 → 非核心通用处理）
    - 3.4: 实现 LSP 生命周期（initialize, initialized, shutdown, exit）
    - 3.5: 实现 `_handle_chat_ask`（thinking → 历史回填 → call_llm + on_chunk/on_tool_call/on_thinking → 持久化后台任务 → chat/finish → 响应）
    - 3.6: 实现 `_handle_chat_stop`（设置 cancel_event）
    - 3.7: 实现消息发送方法，**严格使用插件源码验证的字段名**：
        - `_send_chat_answer(session_id, text, request_id)` → `{"requestId", "sessionId", "text", "overwrite": False, "timestamp"}`（对应 ChatAnswerParams，**不是** `content`）
        - `_send_chat_think(session_id, text, step, request_id)` → `{"requestId", "sessionId", "text", "step": "start"/"done"}`（对应 ChatThinkingParams，**不是** `thinking: true`）
        - `_send_chat_finish(session_id, reason, full_answer, request_id)` → `{"requestId", "sessionId", "reason", "statusCode": 0, "fullAnswer"}`（对应 ChatFinishParams）
        - `_send_message`, `_send_response`, `_send_error_response`, `_send_request`, `_send_notification` 通用方法

- [x] Task 4: 实现 JSON-RPC 路由器 — 工具调用 + 持久化 + 响应处理
    - 4.1: 实现 `_handle_tool_call_approve`（权限批准，MVP 返回空响应；ACP 的 `_IDE_TOOLS_REQUIRING_PERMISSION` 在 MVP 阶段自动批准）
    - 4.2: 实现 `_handle_tool_invoke_result`（从 params 提取 toolCallId，resolve pending Future）
    - 4.3: 实现 `_handle_response`（从 JSON-RPC 响应 resolve pending Future；处理同步工具返回路径）
    - 4.4: 实现 `invoke_tool_on_ide`，**严格使用插件 ToolInvokeRequest 字段名**：
        - 发送 `{"requestId", "toolCallId", "name": tool_name, "parameters": arguments, "async": False}`
        - **不是** `{"tool": ..., "arguments": ...}`（插件不识别 `tool` 和 `arguments` 字段）
        - `name` 字段使用插件原生工具名（`read_file` 等），**不是** ACP 的 `ide_read_file`
        - 120s 超时，不重试
    - 4.5: 实现 `_persist_lsp4j_chat_turn`（验证 UUID → 创建/更新 ChatSession + ChatMessage → ws_module 通知 Web UI → log_activity，source_channel="ide_lsp4j"）
        - ⚠️ 边界条件：通义灵码使用 `UUID.randomUUID().toString()` 生成 sessionId（总是 UUID），但某些代码路径传 `null`。`uuid.UUID(session_id)` 抛 `ValueError` 时静默返回
    - 4.6: 实现 `_load_lsp4j_history_from_db`（验证 UUID + 所有权 → 查询 ChatMessage → 返回 [{"role","content"}]）
    - 4.7: 实现 `invoke_lsp4j_tool`（模块级函数，通过 _active_routers 查找路由器 → 调用 invoke_tool_on_ide）

- [x] Task 5: 实现 WebSocket 端点（router.py）
    - 5.1: 实现 `lsp4j_websocket_endpoint`（token 认证 → _resolve_agent_override 判空 → accept + ContextVar 设置 → 注册 _active_routers → 消息循环）
    - 5.2: 实现 finally 清理（ContextVar 重置 + _active_routers 移除 + pending Futures resolve + 清空消息历史）
    - 5.3: 验证：WebSocket 连接/认证/断开流程正常

- [x] Task 6: 实现 LSP4J 工具调用钩子（tool_hooks.py — 执行路径 + 注册路径）
    - 6.1: 定义 `_LSP4J_IDE_TOOL_NAMES` frozenset（**插件原生名称**，非 ACP 的 `ide_` 前缀名称）：
        - `"read_file"`, `"save_file"`, `"run_in_terminal"`, `"get_terminal_output"`
        - `"replace_text_by_path"`, `"create_file_with_text"`, `"delete_file_by_path"`, `"get_problems"`
        - ⚠️ 与 ACP 的 `_IDE_BRIDGE_TOOL_NAMES` 完全不同！插件不识别 `ide_` 前缀，发送 `ide_read_file` 会返回 "tool not support yet"
    - 6.2: 定义 `_LSP4J_IDE_TOOLS` 工具定义列表（使用插件原生名称和描述，不复用 ACP 的 `IDE_TOOLS`）：
        - 8 个工具定义，名称和描述与插件 `ToolInvokeProcessor` 支持的工具完全一致
        - 参数格式需匹配插件 `ToolInvokeRequest.parameters` 的实际字段
        - ⚠️ 不能导入 ACP 的 `IDE_TOOLS`（工具名是 `ide_read_file` 等，插件无法识别）
    - 6.3: 实现 `install_lsp4j_tool_hooks`（`_installed` 防重复 → 获取 ACP `_custom_execute_tool` + `_custom_get_tools` → 定义增强版 → 替换 ACP 引用 + agent_tools 引用）
        - ⚠️ 与 ACP 安装时机差异：ACP 在模块导入时调用（router.py:854），LSP4J 在 `register()` 中调用（保证 ACP 先加载）
    - 6.4: 实现 `_lsp4j_aware_execute_tool`：
        - 若 `current_lsp4j_ws` 活跃 且 tool_name 在 `_LSP4J_IDE_TOOL_NAMES` 中 → 走 LSP4J 路径
        - 否则走 ACP 原路径（降级处理由 ACP 兜底：双 ContextVar 均 None 时返回中文提示）
    - 6.5: 实现 `_lsp4j_aware_get_tools`：
        - 若 `current_lsp4j_ws` 活跃 → 返回 `tools + _LSP4J_IDE_TOOLS`（插件原生名称）
        - 否则走 ACP 原路径（ACP 的 `_custom_get_tools` 会在 `current_acp_ws` 活跃时追加 `IDE_TOOLS`）
        - ⚠️ 致命关键：若只补丁执行路径不补丁注册路径，LLM 看不到 IDE 工具，永远不会调用
    - 6.6: 验证：ACP 和 LSP4J 工具调用互不干扰（执行路径 + 注册路径均正确）

- [x] Task 7: 通义灵码 Java 端配置修改（5 个文件）
    - 7.1: 修改 `GlobalConfig.java`（当前 11 个字段均无 Clawith 相关；增加 `clawithBackendUrl`, `clawithApiKey`, `clawithAgentId`, `useClawithBackend` 字段 + getter/setter + equals/hashCode/toString）
    - 7.2: 修改 `LingmaUrls.java`（增加 `CLAWITH_SERVICE_URL` 枚举值）
    - 7.3: 修改 `ConfigMainForm.java`（增加 Clawith 配置 UI：复选框 + URL/Key/AgentID 输入框 + 测试连接按钮）
    - 7.4: 修改 `CosyConfig.java`（增加 Clawith 配置的保存/加载持久化逻辑）
    - 7.5: 修改 `LanguageWebSocketService.java`（当前只有 `createService(Project, int)` 和 `createServiceWithPipe()`；增加 `createClawithService(Project, String url, String apiKey, String agentId)` 方法，URL 含 `agent_id` + `token` 查询参数）

- [x] Task 8: 端到端集成验证
    - 8.1: 验证插件自动加载（启动 Clawith 后 `/api/plugins/clawith-lsp4j/ws` 可达）
    - 8.2: 验证 WebSocket 连接（通义灵码 → 认证 → initialize 握手 → chat/ask 流式对话）
    - 8.3: 验证聊天推送格式（chat/answer 使用 `text` 字段、chat/think 使用 `step` 字段、chat/finish 包含 `fullAnswer`）
    - 8.4: 验证工具调用（LLM 调用 `read_file` → tool/invoke 使用 `name`+`parameters` 字段 → 插件执行 → 结果回传）
    - 8.5: 验证对话持久化（chat/ask 后检查 DB 中 ChatSession + ChatMessage，source_channel="ide_lsp4j"）
    - 8.6: 验证 Web UI 可见（持久化后 Web 端能看到 LSP4J 对话）
    - 8.7: 验证历史回填（断开重连后 chat/ask 能恢复之前上下文）
    - 8.8: 验证 ACP 共存（ACP 通道同时正常工作，ACP 使用 `ide_` 前缀工具名，LSP4J 使用插件原生名称）

---

## 实施注意事项（关键细节）

### 1. 插件原生工具名 vs ACP 工具名（Task 6.1/6.2）

**两套完全不同的工具名称体系，绝不能混用**：

| 用途 | 变量名 | 名称示例 | 来源 |
|------|--------|---------|------|
| ACP IDE 工具 | `_IDE_BRIDGE_TOOL_NAMES` | `ide_read_file`, `ide_write_file` | ACP router.py:174 |
| LSP4J IDE 工具 | `_LSP4J_IDE_TOOL_NAMES` | `read_file`, `save_file` | 插件 ToolInvokeProcessor.java |

| 用途 | 变量名 | 工具定义 | 来源 |
|------|--------|---------|------|
| ACP IDE 工具定义 | `IDE_TOOLS` | 名称含 `ide_` 前缀 | ACP router.py:194 |
| LSP4J IDE 工具定义 | `_LSP4J_IDE_TOOLS` | 插件原生名称 | 新定义 |

### 2. 后台任务生命周期模式（Task 4.5）

`_persist_lsp4j_chat_turn` 使用 fire-and-forget 模式，必须加入 `_lsp4j_background_tasks` 集合管理生命周期（防止 GC 回收导致任务静默消失）：
```python
_t = asyncio.create_task(
    _persist_lsp4j_chat_turn(
        agent_id=agent_obj.id,
        session_id=session_id,
        user_text=user_message,
        reply_text=reply,
        user_id=self._user_id,
    )
)
_lsp4j_background_tasks.add(_t)
_t.add_done_callback(_lsp4j_background_tasks.discard)
```

### 3. 历史回填时机（Task 3.5）

`_handle_chat_ask` 中的调用顺序：
1. 发送 `chat/think(step="start")`（思考状态）
2. 调用 `_load_lsp4j_history_from_db`（历史回填）
3. 调用 `call_llm`（带 on_chunk/on_tool_call/on_thinking 回调）
4. 创建持久化后台任务
5. 发送 `chat/finish(reason="success", fullAnswer=reply)`（完成信号）
6. 返回 JSON-RPC 响应

### 4. finally 清理完整性（Task 5.2）

必须包含以下步骤（按顺序）：
1. 重置 ContextVar（`current_lsp4j_ws.set(None)` 等）
2. 从 `_active_routers` 移除路由器实例
3. **resolve 所有 pending Futures**（防止协程挂起）
4. 清空消息历史（`current_lsp4j_message_history.set([])`）
5. 关闭 WebSocket 连接

### 5. 工具注册路径与执行路径缺一不可（Task 6.5）

LSP4J 必须同时补丁两条路径：

| 路径 | ACP 函数 | LSP4J 增强版 | 作用 |
|------|----------|-------------|------|
| 注册路径 | `_custom_get_tools`（router.py:584-588） | `_lsp4j_aware_get_tools` | LLM 看到 IDE 工具定义 |
| 执行路径 | `_custom_execute_tool`（router.py:590-851） | `_lsp4j_aware_execute_tool` | IDE 工具调用路由到 LSP4J |

### 6. 钩子安装时机与 ACP 的差异（Task 6.3）

- **ACP**：`install_acp_tool_hooks()` 在模块导入时调用（router.py:854 裸调用），先于 `register()`
- **LSP4J**：`install_lsp4j_tool_hooks()` 在 `register()` 中调用，晚于 ACP 的安装
- **原因**：LSP4J 需要获取 ACP 已安装的 `_custom_execute_tool` 和 `_custom_get_tools` 引用来包裹增强

### 7. 插件协议字段名严格匹配要求（Task 3.7 / Task 4.4）

基于插件源码验证，以下字段名必须严格匹配，**任何不匹配都会导致插件静默丢弃消息或解析失败**：

| 场景 | 错误字段 | 正确字段 | 插件数据类 |
|------|---------|---------|-----------|
| chat/answer 内容 | `content` | `text` | ChatAnswerParams |
| chat/think 状态 | `thinking: true` | `step: "start"/"done"` | ChatThinkingParams |
| tool/invoke 工具名 | `tool` | `name` | ToolInvokeRequest |
| tool/invoke 参数 | `arguments` | `parameters` | ToolInvokeRequest |
| tool/invoke 请求 ID | 缺失 | `requestId` | ToolInvokeRequest |
| chat/answer 请求 ID | 缺失 | `requestId` | ChatAnswerParams |
| chat/think 请求 ID | 缺失 | `requestId` | ChatThinkingParams |
| chat/finish 请求 ID | 缺失 | `requestId` | ChatFinishParams |
| chat/finish 完整回答 | 缺失 | `fullAnswer` | ChatFinishParams |

### 8. 工具执行同步/异步双路径（Task 4.3/4.4）

插件的 `ToolInvokeProcessor` 支持 `async` 模式：
- **同步**（`async=false`）：工具结果通过 `tool/invoke` 的 JSON-RPC 响应直接返回 → 走 `_handle_response` 路径
- **异步**（`async=true`）：插件先返回空成功响应，实际结果通过 `tool/invokeResult` 请求异步回传 → 走 `_handle_tool_invoke_result` 路径
- 两条路径互斥（LSP4J 框架只会走一条），当前 LSP4J 发送 `async: false` 走同步路径
