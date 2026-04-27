# 通义灵码 LSP4J 插件接入 Clawith — 规格文档

## 需求概述

将开源通义灵码（Tongyi Lingma）JetBrains IDE 插件适配连接到自部署的 Clawith AI 后端。创建独立的 `clawith_lsp4j` 插件，通过 LSP4J 协议（LSP Base Protocol + JSON-RPC 2.0）提供 WebSocket 端点，与现有 ACP 插件共存。

## 架构方案

新建 `backend/app/plugins/clawith_lsp4j/` 独立插件，不修改坏的 IDE Bridge。插件由以下模块组成：

```
clawith_lsp4j/
├── __init__.py          # ClawithPlugin 子类 + plugin 注册
├── plugin.json          # 插件元数据
├── context.py           # ContextVar 定义（避免循环依赖）
├── router.py            # FastAPI WebSocket 端点 + 认证
├── lsp_protocol.py      # LSP Base Protocol 解析器（字节级操作）
├── jsonrpc_router.py    # JSON-RPC 2.0 路由器 + call_llm 集成 + 对话持久化
└── tool_hooks.py        # ACP 工具钩子扩展（LSP4J ContextVar 判断）
```

### 核心流程

1. 插件通过 `ClawithPlugin.register()` 注册 WebSocket 路由到 `/api/plugins/clawith-lsp4j/ws`
2. 通义灵码 IDE 连接时传入 `agent_id` + `token` 参数
3. 后端复用 `_resolve_agent_override` 解析 Agent/Model
4. `chat/ask` 请求触发 `call_llm`，通过 on_chunk/on_tool_call/on_thinking 回调推送流式内容
5. IDE 工具通过 `tool/invoke`（server→client）调用，结果通过 `tool/invokeResult` 或 JSON-RPC 响应回传
6. `call_llm` 返回后，fire-and-forget 后台任务持久化 ChatSession + ChatMessage 到数据库
7. 首次 `chat/ask` 时从数据库回填历史（支持重连恢复上下文）
8. 持久化后通知 Web UI 实时刷新

### 关键协议约束

- **LSP Base Protocol**：`Content-Length: {UTF-8 字节数}\r\n\r\n{JSON}`，必须按字节操作
- **chat/answer、chat/finish、chat/think** 是 `@JsonRequest`（必须带 `id`），不是 `@JsonNotification`
- **tool/invoke** 是 server→client `@JsonRequest`，插件执行后返回 ToolInvokeResponse
- **tool/invokeResult** 是 client→server `@JsonRequest`（`@JsonSegment("tool") + @JsonRequest("invokeResult")`），需解析 pending Future
- **call_llm** 通过异步回调实现流式，不是 async generator

### 插件协议字段映射（基于源码验证）

以下字段映射来自通义灵码插件源码（`/Users/shubinzhang/Downloads/demo-new`）的实际数据类定义，**必须严格匹配**，否则插件无法解析。

#### chat/answer（LanguageClient `@JsonRequest("chat/answer")`，参数类型 `ChatAnswerParams`）

| 字段 | 类型 | 说明 | 之前方案（错误） |
|------|------|------|-----------------|
| `requestId` | String | 请求 ID（必需） | 缺失 |
| `sessionId` | String | 会话 ID | 正确 |
| `text` | String | 流式文本内容 | 用了 `content` |
| `overwrite` | Boolean | 是否覆盖前一次输出 | 缺失 |
| `isFiltered` | Boolean | 是否被过滤 | 缺失 |
| `timestamp` | Long | 时间戳 | 缺失 |
| `extra` | Map | 扩展字段（含 sessionType） | 缺失 |

#### chat/think（LanguageClient `@JsonRequest("chat/think")`，参数类型 `ChatThinkingParams`）

| 字段 | 类型 | 说明 | 之前方案（错误） |
|------|------|------|-----------------|
| `requestId` | String | 请求 ID（必需） | 缺失 |
| `sessionId` | String | 会话 ID | 正确 |
| `text` | String | 思考内容 | 用了 `content` |
| `step` | String | 步骤标识：`"start"` 或 `"done"` | 用了 `thinking: true` |
| `timestamp` | Long | 时间戳 | 缺失 |
| `extra` | Map | 扩展字段 | 缺失 |

#### chat/finish（LanguageClient `@JsonRequest("chat/finish")`，参数类型 `ChatFinishParams`）

| 字段 | 类型 | 说明 | 之前方案（错误） |
|------|------|------|-----------------|
| `requestId` | String | 请求 ID（必需） | 缺失 |
| `sessionId` | String | 会话 ID | 正确 |
| `reason` | String | 完成原因 | 正确 |
| `statusCode` | Integer | 状态码 | 缺失 |
| `fullAnswer` | String | 完整回答文本 | 缺失 |
| `extra` | Map | 扩展字段 | 缺失 |

#### tool/invoke（LanguageClient `@JsonRequest("tool/invoke")`，参数类型 `ToolInvokeRequest`）

| 字段 | 类型 | 说明 | 之前方案（错误） |
|------|------|------|-----------------|
| `requestId` | String | 请求 ID（必需） | 缺失 |
| `toolCallId` | String | 工具调用 ID | 正确 |
| `name` | String | 工具名称 | 用了 `tool` |
| `parameters` | Map | 工具参数 | 用了 `arguments` |
| `async` | Boolean | 是否异步执行 | 缺失 |

#### tool/invokeResult（ToolService `@JsonSegment("tool") + @JsonRequest("invokeResult")`，参数类型 `ToolInvokeResponse`）

| 字段 | 类型 | 说明 |
|------|------|------|
| `toolCallId` | String | 工具调用 ID |
| `name` | String | 工具名称 |
| `success` | Boolean | 是否成功 |
| `errorMessage` | String | 错误信息 |
| `result` | Map | 执行结果 |
| `startTime` | long | 开始时间 |
| `timeConsuming` | long | 耗时 |

### 插件原生工具名称（基于 ToolInvokeProcessor 源码验证）

插件的 `ToolInvokeProcessor.java`（switch 语句）只处理以下 **8 个原生工具名**，不支持任何 `ide_` 前缀名称。发送 `ide_read_file` 等名称会触发 `default` 分支返回 `"tool not support yet"`。

| 插件原生名称 | ACP 对应名称 | 语义对应 | 参数差异 |
|-------------|-------------|---------|---------|
| `read_file` | `ide_read_file` | 读取文件 | 参数名不同 |
| `save_file` | `ide_write_file` | 保存文件 | 参数名不同 |
| `run_in_terminal` | `ide_execute_command` | 执行终端命令 | 参数名不同 |
| `get_terminal_output` | `ide_terminal_output` | 获取终端输出 | 参数名不同 |
| `replace_text_by_path` | 无直接对应 | 文本替换 | 插件独有 |
| `create_file_with_text` | `ide_mkdir` | 创建文件 | 语义不同 |
| `delete_file_by_path` | `delete_file` | 删除文件 | 名称不同 |
| `get_problems` | 无对应 | 获取代码问题 | 插件独有 |

ACP 中以下工具在插件中**完全不存在**：`ide_list_files`、`ide_move`、`ide_append`、`ide_kill_terminal`、`ide_release_terminal`、`ide_create_terminal`。

**设计决策**：定义独立的 `_LSP4J_IDE_TOOLS` 工具定义列表（使用插件原生名称），不复用 ACP 的 `IDE_TOOLS`。原因：
1. 工具名称不匹配（`ide_` 前缀 vs 无前缀），直接发送会导致插件报 "tool not support yet"
2. 工具能力不完全对等（插件有 `get_problems`、`replace_text_by_path` 等独有工具）
3. 参数格式可能不同（插件用 `parameters`，ACP 用各自的参数格式）

### ACP 共存机制

- LSP4J 扩展 ACP 的 `_custom_execute_tool` 和 `_custom_get_tools`，增加 `current_lsp4j_ws` ContextVar 判断
- **执行路径**：`_lsp4j_aware_execute_tool` — 当 `current_lsp4j_ws` 活跃且工具名在 `_LSP4J_IDE_TOOL_NAMES` 中时走 LSP4J 路径，否则走 ACP 原路径
- **注册路径**：`_lsp4j_aware_get_tools` — 当 `current_lsp4j_ws` 活跃时追加 `_LSP4J_IDE_TOOLS`（插件原生名称），否则走 ACP 原路径（追加 ACP 的 `IDE_TOOLS`）
- 两条路径缺一不可：若只补丁执行路径不补丁注册路径，LLM 看不到 IDE 工具，永远不会调用
- `_installed` 标志防止重复安装钩子

### 对话持久化（参考 ACP）

- `_persist_lsp4j_chat_turn`：fire-and-forget 后台任务，创建 ChatSession（`source_channel="ide_lsp4j"`）+ ChatMessage
- `_load_lsp4j_history_from_db`：验证 session_id UUID + 所有权后返回历史消息
- `ws_module.manager.send_to_session()`：通知 Web UI 实时刷新
- `log_activity`：记录活动日志（`channel="ide_lsp4j"`）
- `_lsp4j_background_tasks`：防止 GC 回收未完成的持久化任务（`add` + `add_done_callback(discard)` 模式）

### 钩子安装时机

- **ACP**：`install_acp_tool_hooks()` 在模块导入时调用（router.py:854 裸调用）
- **LSP4J**：`install_lsp4j_tool_hooks()` 在 `register()` 中调用
- **原因**：LSP4J 需要获取 ACP 已安装的 `_custom_execute_tool` 和 `_custom_get_tools` 引用来包裹增强。在 `register()` 中调用保证 ACP 先加载完成

## 受影响文件

### 新建文件（Python 后端）

| 文件 | 类型 | 说明 |
|------|------|------|
| `backend/app/plugins/clawith_lsp4j/__init__.py` | 新建 | ClawithPlugin 子类 + plugin 注册 |
| `backend/app/plugins/clawith_lsp4j/plugin.json` | 新建 | 插件元数据 |
| `backend/app/plugins/clawith_lsp4j/context.py` | 新建 | ContextVar 定义 |
| `backend/app/plugins/clawith_lsp4j/router.py` | 新建 | WebSocket 端点 + 认证 |
| `backend/app/plugins/clawith_lsp4j/lsp_protocol.py` | 新建 | LSP Base Protocol 解析器 |
| `backend/app/plugins/clawith_lsp4j/jsonrpc_router.py` | 新建 | JSON-RPC 路由器 + 持久化 |
| `backend/app/plugins/clawith_lsp4j/tool_hooks.py` | 新建 | ACP 工具钩子扩展（含 `_LSP4J_IDE_TOOLS` 定义） |

### 修改文件（Java 插件端）

| 文件 | 修改类型 | 说明 |
|------|----------|------|
| `GlobalConfig.java` | 增加字段 | `clawithBackendUrl`, `clawithApiKey`, `clawithAgentId`, `useClawithBackend`（当前 11 个字段均无 Clawith 相关） |
| `LingmaUrls.java` | 增加枚举 | `CLAWITH_SERVICE_URL` |
| `ConfigMainForm.java` | 增加 UI | Clawith 配置面板（URL/Key/AgentID 输入框 + 复选框） |
| `CosyConfig.java` | 增加逻辑 | 配置持久化（保存/加载 Clawith 配置） |
| `LanguageWebSocketService.java` | 增加方法 | `createClawithService()` 方法（当前只有 `createService(Project, int)` 和 `createServiceWithPipe()`，无 Clawith 相关方法） |

### 间接依赖文件（只读复用，不修改）

| 文件 | 复用方式 |
|------|----------|
| `backend/app/plugins/clawith_acp/router.py` | 导入 `_resolve_agent_override`，扩展 `_custom_execute_tool` + `_custom_get_tools` |
| `backend/app/services/llm/caller.py` | 导入 `call_llm` |
| `backend/app/services/agent_tools.py` | 通过 ACP hook 间接调用 |
| `backend/app/core/security.py` | 导入 `verify_api_key_or_token` |
| `backend/app/database.py` | 导入 `async_session` |
| `backend/app/models/chat_session.py` | 导入 `ChatSession` |
| `backend/app/models/audit.py` | 导入 `ChatMessage` |
| `backend/app/api/websocket.py` | 导入 `ws_module.manager` |

## 边界条件与异常处理

1. **session_id 非 UUID 格式**：插件始终使用 `UUID.randomUUID().toString()` 生成 sessionId，后端 `uuid.UUID(session_id)` 抛 `ValueError` 时静默返回
2. **token 为空**：WebSocket 连接前显式 `if not token` 检查，返回 4001
3. **Agent/Model 未找到**：`_resolve_agent_override` 返回 None 时关闭连接（4002）
4. **工具调用超时**：120s 超时，不重试（避免双次执行）
5. **持久化失败**：`_persist_lsp4j_chat_turn` 内部捕获所有异常，不影响 IDE 响应
6. **Web UI 通知失败**：不阻塞持久化流程，仅 debug 日志
7. **连接断开**：finally 块清理 ContextVar + _active_routers + pending Futures
8. **ContextVar 可变默认值**：所有 dict/list 类型 default=None，在 router.py 中 set({})/set([])
9. **工具名不在插件支持列表**：`_lsp4j_aware_execute_tool` 收到插件不支持的 IDE 工具名时，走 ACP 原路径兜底
10. **async 工具执行**：插件 `ToolInvokeRequest.async=true` 时返回空成功响应，实际结果通过 `tool/invokeResult` 异步回传。后端需同时处理 `_handle_response`（同步路径）和 `_handle_tool_invoke_result`（异步路径）

## 数据流路径

```
IDE chat/ask → WebSocket → LSPBaseProtocolParser → JSONRPCRouter._handle_chat_ask
  → chat/think(step="start") → IDE 显示思考状态
  → _load_lsp4j_history_from_db（回填）
  → call_llm(messages, on_chunk, on_tool_call, on_thinking)
    → on_chunk → chat/answer(text=...) → IDE 显示流式文本
    → on_thinking → chat/think(text=..., step=...) → IDE 显示推理过程
    → on_tool_call → execute_tool → _lsp4j_aware_execute_tool
      → LSP4J 活跃? → invoke_lsp4j_tool → tool/invoke(name=..., parameters=...) → IDE 执行
        → 同步: JSON-RPC 响应 → _handle_response → resolve Future
        → 异步: tool/invokeResult 请求 → _handle_tool_invoke_result → resolve Future
      → 否则 → ACP 原路径
  → _persist_lsp4j_chat_turn（后台） → DB + ws_module 通知 Web UI
  → chat/finish(reason="success", fullAnswer=...) → IDE 完成信号
```

## 插件源码参考路径

| 组件 | 源码路径 |
|------|----------|
| LanguageClient（server→client 方法） | `src/main/java/.../core/lsp/model/LanguageClient.java` |
| LanguageServer（client→server 方法） | `src/main/java/.../core/lsp/model/LanguageServer.java` |
| ChatService（chat/* 方法） | `src/main/java/.../core/lsp/model/service/ChatService.java` |
| ToolService（tool/invokeResult） | `src/main/java/.../core/lsp/model/service/ToolService.java` |
| ToolInvokeProcessor（工具执行分发） | `src/main/java/.../chat/processor/ToolInvokeProcessor.java` |
| ToolInvokeRequest | `src/main/java/.../core/lsp/model/tool/ToolInvokeRequest.java` |
| ToolInvokeResponse | `src/main/java/.../core/lsp/model/tool/ToolInvokeResponse.java` |
| ChatAskParam | `src/main/java/.../chat/model/ChatAskParam.java` |
| ChatAnswerParams | `src/main/java/.../core/lsp/model/params/ChatAnswerParams.java` |
| ChatFinishParams | `src/main/java/.../core/lsp/model/params/ChatFinishParams.java` |
| ChatThinkingParams | `src/main/java/.../core/lsp/model/params/ChatThinkingParams.java` |
| GlobalConfig | `src/main/java/.../core/lsp/model/model/GlobalConfig.java` |
| LanguageWebSocketService | `src/main/java/.../core/lsp/LanguageWebSocketService.java` |
| CosyWebSocketConnectClient | `src/main/java/.../core/websocket/CosyWebSocketConnectClient.java` |
| CosyWebSocketMessageConsumer | `src/main/java/.../core/websocket/CosyWebSocketMessageConsumer.java` |

## 预期成果

1. 通义灵码 IDE 插件连接 Clawith 后端进行 AI 对话
2. 工具调用（文件读写、命令执行等）通过 IDE 执行并返回结果
3. ACP 插件继续正常工作，两者互不干扰
4. 对话持久化到数据库，Web UI 可查看 LSP4J 对话
5. 重连后从数据库恢复历史上下文
6. 智能体自我进化不受影响（记忆完整保存）
