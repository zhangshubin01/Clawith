# 审计 LSP4J 协议中 "成功==0" 及错误状态码缺失问题

## 需求场景

用户发现 `chat/finish` 中 `statusCode: 0` 被灵码插件当作错误（插件检查 `statusCode == 200` 判断成功），修复后要求全面审计是否还有其他类似情况。

## 审计结果

### 1. `statusCode: 0` — 已修复，无残留

| 文件 | 行号 | 状态 |
|------|------|------|
| `jsonrpc_router.py:781` | `_send_chat_finish` | ✅ 已改为 `statusCode: 200` |

全局搜索 `backend/app/plugins/clawith_lsp4j/` 目录，**无其他 `statusCode: 0` 或类似 `成功==0` 的代码**。

### 2. 灵码插件对 statusCode 的完整定义（BaseChatPanel.stopGenerate()）

插件 `BaseChatPanel.java` 中 `statusCode` 的所有分支：

| statusCode | 含义 | Clawith 当前是否使用 |
|-----------|------|---------------------|
| **200** | 成功 | ✅ 正常流程 |
| 429 | 限流/请求过多 | ❌ 未区分 |
| 403 | 认证/配额错误 | ❌ 未区分 |
| 406 | 内容过滤 | ❌ 未区分 |
| 408 | 超时/空响应 | ❌ 未区分 |
| 500 | 内部服务器错误 | ❌ 未区分 |
| 601 | 内容过滤拦截 | ❌ 未区分 |
| 其他（31404, 409, 40505, 602, 400, 401, 604, 605, 90000） | 各类业务错误 | ❌ 未区分 |

### 3. 发现的关联问题：异常时 statusCode 仍然为 200

**当前代码逻辑** (`jsonrpc_router.py:519-558`)：

```python
except asyncio.CancelledError:
    cancelled = True
    reply = ""
except Exception as e:
    logger.exception("LSP4J call_llm error")
    reply = f"[错误] {type(e).__name__}: {str(e)[:200]}"

# ... 后续：
finish_reason = "cancelled" if cancelled else "success"
await self._send_chat_finish(session_id, finish_reason, reply, request_id)
```

**问题**：
- `call_llm` 抛异常时，`cancelled = False`，所以 `finish_reason = "success"`
- `_send_chat_finish` 固定 `statusCode: 200`
- 插件收到 `statusCode: 200` → 进入成功分支 → 把 `[错误] xxx` 当作正常回答显示
- 同理，取消时 `reason: "cancelled"` 但 `statusCode: 200`，插件可能误判

### 4. 其他消息体的状态指示字段（均正确）

| 消息 | 字段 | 当前值 | 插件期望 | 状态 |
|------|------|--------|---------|------|
| `chat/ask` 响应 | `isSuccess` | `True` | `Boolean true` | ✅ |
| `stepProcessConfirm` 响应 | `successful` | `True` | `boolean true` | ✅ |
| `chat/answer` | (无 statusCode) | — | — | ✅ |
| `chat/think` | (无 statusCode) | — | — | ✅ |
| `chat/process_step_callback` | `status` | `"doing"/"done"/"error"` | 字符串枚举 | ✅ |
| `tool/invokeResult` 响应 | `status` | `"ok"` | 字符串 | ✅ |
| `codeChange/apply` 响应 | (无 statusCode) | — | — | ✅ |

## 架构与技术方案

### 修复方案：根据 call_llm 结果动态设置 statusCode 和 reason

修改 `_handle_chat_ask` 的异常处理和 finish 逻辑：

1. **正常完成**：`statusCode: 200`, `reason: "success"`
2. **用户取消**：`statusCode: 200`, `reason: "cancelled"` （取消不是错误，灵码也正常处理）
3. **LLM 调用异常**：`statusCode: 500`, `reason: "[错误类型]"`
4. **超时**：`statusCode: 408`, `reason: "timeout"`

## 受影响文件

| 文件 | 修改类型 | 受影响函数 |
|------|---------|-----------|
| `backend/app/plugins/clawith_lsp4j/jsonrpc_router.py` | 修改 | `_handle_chat_ask` (异常→statusCode映射), `_send_chat_finish` (新增 statusCode 参数) |

## 实现细节

### 修改 `_send_chat_finish` 增加 statusCode 参数

```python
async def _send_chat_finish(
    self, session_id: str | None, reason: str, full_answer: str,
    request_id: str, status_code: int = 200,
) -> None:
    """发送 chat/finish（ChatFinishParams 格式）。

    statusCode 取值（基于 BaseChatPanel.stopGenerate 分支）：
    - 200: 成功
    - 408: 超时/空响应
    - 500: 内部服务器错误
    """
    await self._send_notification("chat/finish", {
        "requestId": request_id,
        "sessionId": session_id or "",
        "reason": reason,
        "statusCode": status_code,
        "fullAnswer": full_answer,
    })
```

### 修改 `_handle_chat_ask` 异常映射

```python
# 异常处理
error_status_code = 200  # 默认成功
except asyncio.CancelledError:
    cancelled = True
    reply = ""
except asyncio.TimeoutError:
    error_status_code = 408
    reply = ""
except Exception as e:
    logger.exception("LSP4J call_llm error")
    error_status_code = 500
    reply = f"[错误] {type(e).__name__}: {str(e)[:200]}"

# 发送 finish
finish_reason = "cancelled" if cancelled else ("success" if error_status_code == 200 else "error")
await self._send_chat_finish(session_id, finish_reason, reply, request_id, status_code=error_status_code)
```

## 边界条件

- `call_llm` 返回空字符串但不抛异常：仍为 `statusCode: 200`（正常完成但无内容）
- 用户手动取消：`statusCode: 200`, `reason: "cancelled"`（取消是用户主动行为，不是服务端错误）
- 网络断开导致 WebSocket 异常：不在 chat/finish 路径，无需 statusCode
- BYOK 密钥无效：`call_llm` 内部会抛认证异常，映射到 `statusCode: 500`

## 数据流

```
call_llm 成功 → finish(statusCode=200, reason="success")
call_llm 取消 → finish(statusCode=200, reason="cancelled") 
call_llm 超时 → finish(statusCode=408, reason="error")
call_llm 异常 → finish(statusCode=500, reason="error")
                ↓
插件 BaseChatPanel.stopGenerate(statusCode)
  → 200: 正常显示回答
  → 408: 显示超时提示
  → 500: 显示服务器错误提示
```

## 预期结果

- 灵码插件能正确区分成功/超时/错误场景
- 用户在灵码中看到有意义的错误提示而非 `[错误] xxx` 文本
- 无 `statusCode: 0` 残留

---

## 附：tool/invoke 调用链路分析

### 问题描述

LSP4J 协议中已实现 `tool/invoke` 能力（`jsonrpc_router.py:661 invoke_tool_on_ide()`），
理论上 Clawith 智能体可以通过 `read_file` 等工具让 IDE 端读取本地文件，
但实际运行时**智能体没有调用这些工具**。

### 链路追踪

```
call_llm → get_agent_tools_for_llm(agent_id) → 返回含 _LSP4J_IDE_TOOLS 的工具列表
call_llm → LLM 决定是否调用工具
  → 如果调用: _process_tool_call → execute_tool → _lsp4j_aware_execute_tool
    → current_lsp4j_ws.get() 检查
    → invoke_lsp4j_tool → router_instance.invoke_tool_on_ide
      → 发送 tool/invoke JSON-RPC 请求到 IDE
      → IDE ToolInvokeProcessor 执行并返回 ToolInvokeResponse
```

### 可能的失败点

| # | 失败点 | 症状 | 严重度 |
|---|--------|------|--------|
| 1 | **LLM 不调用工具** | 日志中无 `[LLM] Calling tool: read_file` | 高 |
| 2 | requestId 无法关联 Project | IDE 返回 `get project by requestId is null` | 中（有降级） |
| 3 | _lsp4j_aware_get_tools 未生效 | LLM 看不到 IDE 工具定义 | 高 |
| 4 | _installed 防重入导致热重载失败 | 工具钩子未安装 | 低 |

### 诊断方案

1. 在 `_lsp4j_aware_get_tools` 添加日志，确认 IDE 工具是否被注册
2. 在 `_lsp4j_aware_execute_tool` 添加日志，确认是否走到 LSP4J 路径
3. 检查 call_llm 日志中是否有 tool_calls
4. 检查 IDE 端是否有 `tool/invoke` 请求日志
