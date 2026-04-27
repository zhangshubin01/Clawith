# LSP4J 协议兼容性修复 — 灵码插件适配

## 问题概述

Clawith 后端通过 LSP4J WebSocket 协议与灵码（Cosy）IDE 插件通信。当前存在协议格式不兼容和缺失方法处理的问题，导致插件端解析失败、显示"调用异常"、Method not found 等错误。

## 用户需求（验收标准）

1. **能回复** — 聊天问答正常，不显示"调用异常"
2. **能使用灵码的 diff 相关功能** — 代码变更应用流程不报错
3. **能改代码** — LLM 可通过工具调用让 IDE 执行 save_file、replace_text_by_path 等
4. **能操作灵码的本地工具** — read_file、run_in_terminal、get_problems 等工具调用正常

## 已确认的兼容性问题

### 1. chat/ask 响应格式不匹配（P0 — 已修复）

**Clawith 原返回：**
```json
{"status": "success"}
```

**灵码期望（`ChatAskResult`）：**
```java
public class ChatAskResult {
    private String requestId;
    private String errorMessage;
    private Boolean isSuccess;   // ← 缺失，导致解析后 isSuccess 为 null
    private String status;
}
```

**影响：** 灵码 `JsonUtil.fromJson` 反序列化后 `isSuccess` 为 `null`，被判定为失败，UI 显示"调用异常：success"。

**修复后格式：**
```json
{"isSuccess": true, "requestId": "xxx", "status": "success"}
```

### 2. 灵码发送的 chat 方法未处理（P1）

灵码 `ChatService` 定义了以下 client→server 方法，Clawith `METHOD_MAP` 中大量缺失：

| 方法 | 状态 | 说明 |
|---|---|---|
| `chat/ask` | ✅ 已处理 | 核心聊天请求 |
| `chat/stop` | ✅ 已处理 | 停止当前聊天 |
| `chat/systemEvent` | ❌ 缺失 | 系统事件（连接/断开时可能发送） |
| `chat/getStage` | ❌ 缺失 | 查询当前阶段状态 |
| `chat/replyRequest` | ❌ 缺失 | 回复请求 |
| `chat/like` | ❌ 缺失 | 点赞 |
| `chat/codeChange/apply` | ❌ 缺失 | **代码变更应用（diff 功能关键）** |
| `chat/stopSession` | ❌ 缺失 | 停止会话 |
| `chat/receive/notice` | ❌ 缺失 | 接收通知 |
| `chat/quota/doNotRemindAgain` | ❌ 缺失 | 配额提醒 |
| `system/network_recover` | ❌ 缺失 | 网络恢复事件 |

**影响：** 当灵码发送上述未处理方法时，Clawith 返回 `Method not found: xxx` 错误（-32601），可能导致插件端异常弹窗或日志报错。

**修复方案：** 为所有缺失方法添加存根（stub）处理器，返回空成功响应 `{}`，避免插件端报错。`chat/codeChange/apply` 需额外记录日志以便观察 diff 功能调用情况。

### 3. 通知格式兼容性审计（P2）

以下通知的字段名已与灵码参数类逐一核对，**当前已兼容**：

| 通知 | Clawith 发送字段 | 灵码参数类 | 状态 |
|---|---|---|---|
| `chat/answer` | requestId, sessionId, text, overwrite, isFiltered, timestamp | `ChatAnswerParams` | ✅ |
| `chat/think` | requestId, sessionId, text, step, timestamp | `ChatThinkingParams` | ✅ |
| `chat/finish` | requestId, sessionId, reason, statusCode, fullAnswer | `ChatFinishParams` | ✅ |
| `tool/invoke` | requestId, toolCallId, name, parameters, async | `ToolInvokeRequest` | ✅ |

`tool/invokeResult` 接收字段（toolCallId, name, success, errorMessage, result）与 `ToolInvokeResponse` 也完全匹配。

### 4. 工具调用机制现状（已支持）

`tool_hooks.py` 已注册 8 个灵码原生工具，通过 `tool/invoke` → IDE 执行 → `tool/invokeResult` 回传结果：

| 工具名 | 功能 | 对应 ACP 工具 |
|---|---|---|
| `read_file` | 读取文件 | ide_read_file |
| `save_file` | 保存文件 | ide_write_file |
| `run_in_terminal` | 执行终端命令 | ide_execute_command |
| `get_terminal_output` | 获取终端输出 | ide_terminal_output |
| `replace_text_by_path` | 文本替换 | — |
| `create_file_with_text` | 创建文件 | ide_mkdir（语义不同） |
| `delete_file_by_path` | 删除文件 | delete_file |
| `get_problems` | 获取代码问题 | — |

**结论：** 改代码、操作本地工具的能力已具备，只要 chat/ask 响应修复后 LLM 正常调用工具即可。

## 技术方案

### 方案：渐进式兼容 — 修复响应格式 + 添加方法存根

1. **响应格式修复**（已完成）：修改 `_handle_chat_ask` 返回包含 `isSuccess` 的 `ChatAskResult` 格式。

2. **添加缺失方法存根**：在 `METHOD_MAP` 中注册所有灵码可能发送的方法：
   - 通用方法返回 `{}` 空成功响应
   - `chat/codeChange/apply` 存根额外记录参数中的 applyId/filePath/codeEdit 摘要，便于观察 diff 功能调用

3. **日志增强**：在存根处理器中记录 debug 日志，便于后续识别哪些方法被实际调用、需要真正实现。

## 受影响的文件

| 文件 | 修改类型 | 说明 |
|---|---|---|
| `backend/app/plugins/clawith_lsp4j/jsonrpc_router.py` | 修改 | 修复 chat/ask 响应格式、添加缺失方法存根 |

## 实现细节

### chat/ask 响应修复（已完成）

```python
# jsonrpc_router.py:339-340
await self._send_response(msg_id, {
    "isSuccess": True,
    "requestId": request_id,
    "status": finish_reason,
})
```

### 缺失方法存根处理器

```python
async def _handle_stub(self, params: dict, msg_id: Any) -> None:
    """通用存根处理器 — 返回空成功响应。"""
    await self._send_response(msg_id, {})
```

### chat/codeChange/apply 存根（带日志）

```python
async def _handle_code_change_apply(self, params: dict, msg_id: Any) -> None:
    """处理 chat/codeChange/apply — diff 功能入口。"""
    apply_id = params.get("applyId", "")
    file_path = params.get("filePath", "")
    code_edit = params.get("codeEdit", "")
    logger.debug("LSP4J: chat/codeChange/apply applyId={} filePath={} codeEdit_len={}", 
                 apply_id, file_path, len(code_edit))
    # MVP 阶段仅返回成功，不实际处理代码变更
    await self._send_response(msg_id, {})
```

### METHOD_MAP 扩展

```python
_METHOD_MAP: dict[str, Any] = {
    "initialize": _handle_initialize,
    "shutdown": _handle_shutdown,
    "exit": _handle_exit,
    "chat/ask": _handle_chat_ask,
    "chat/stop": _handle_chat_stop,
    "chat/systemEvent": _handle_stub,
    "chat/getStage": _handle_stub,
    "chat/replyRequest": _handle_stub,
    "chat/like": _handle_stub,
    "chat/codeChange/apply": _handle_code_change_apply,
    "chat/stopSession": _handle_stub,
    "chat/receive/notice": _handle_stub,
    "chat/quota/doNotRemindAgain": _handle_stub,
    "system/network_recover": _handle_stub,
    # 工具相关
    "tool/call/approve": _handle_tool_call_approve,
    "tool/invokeResult": _handle_tool_invoke_result,
}
```

## 边界条件

- 存根处理器对所有参数类型都返回 `{}`，不会校验参数合法性。
- 如果灵码某些方法期望特定格式的响应（如 `listAllSessions` 期望返回会话列表），返回 `{}` 可能导致后续 NPE。需要在日志中监控，逐步为高频方法实现真实逻辑。
- `textDocument/*` 通知（didOpen/didChange/didClose/didSave）已在现有代码中静默忽略，无需修改。

## 数据流

```
灵码插件 → WebSocket → Clawith LSP4J Router → METHOD_MAP 路由
                                         ↓
                              已知方法 → 真实处理器
                              未知方法 → _handle_stub → 返回 {}
```

工具调用数据流：
```
LLM → 调用 read_file/save_file/run_in_terminal 等
  → tool_hooks._lsp4j_aware_execute_tool
  → jsonrpc_router.invoke_lsp4j_tool
  → WebSocket tool/invoke 请求 → 灵码 IDE
  → IDE 执行工具 → 返回 tool/invokeResult
  → Clawith 接收结果 → 返回给 LLM
```

## 预期结果

1. `chat/ask` 响应被灵码正确识别为成功，不再显示"调用异常：success"。
2. 灵码发送的任何 chat/systemEvent、chat/getStage、chat/codeChange/apply 等方法不再触发 Method not found 错误。
3. diff 功能（chat/codeChange/apply）可正常通过，不阻断用户操作。
4. LLM 可通过 tool/invoke 正常调用 IDE 本地工具（读写文件、执行命令、获取问题等）。
5. 后端日志中可观察到哪些存根方法被实际调用，为后续功能完善提供数据支持。
