# LSP4J 模块添加详细诊断日志（修订版 v3.1）

> 基于 19 条评审需求全面修订，覆盖全部功能流的完整调用链日志。
> **关键发现 1：当前代码已有约 38 处日志，另有约 36 处缺失（其中 25 处已规划在下方表格中）。**
> **关键发现 2（v3 新增）：`caller.py` 的 `from ... import` 绑定导致工具 hook 从未被 `call_llm` 调用，LSP4J 工具桥完全失效。必须先修复此 bug，日志才有意义。**

## 需求场景

LSP4J 协议模块缺乏足够的运行时日志，导致以下问题难以排查：
1. `tool/invoke` 工具调用链路断裂无法定位
2. `statusCode` 错误场景无法追踪
3. 生命周期事件（连接、断开、关闭）无记录
4. 工具审批、代码变更应用等关键操作无审计日志
5. **（v3 新增）`caller.py` 的 `from ... import` 绑定 bug 导致工具 hook 从未被 `call_llm` 调用**

## 前置修复：caller.py 的 `from ... import` 绑定 bug

### 问题描述

`caller.py:26` 使用模块级 `from ... import` 绑定：
```python
from app.services.agent_tools import AGENT_TOOLS, execute_tool, get_agent_tools_for_llm
```

**导入时序**：
1. `main.py:291` → `from app.api.websocket import router` → 触发 `caller.py` 加载 → `execute_tool` 绑定到**原始函数**
2. `main.py:377` → `load_plugins(app)` → `install_lsp4j_tool_hooks()` → `agent_tools.execute_tool = _lsp4j_aware_execute_tool`
   - 修改了**模块属性**，但 `caller.py` 的**局部名称**不会更新

**后果**：
- `call_llm` → `execute_tool` 走**原始路径**，不经过 `_lsp4j_aware_execute_tool`
- `call_llm` → `get_agent_tools_for_llm` 走**原始路径**，LLM 看不到 IDE 工具定义
- `read_file` 走服务端实现（`agent_tools.py:2207`），而非 IDE 端
- ACP hook 同样受影响，但 Web Chat 不依赖 IDE 工具所以未暴露

### 修复方案

```python
# caller.py:26 改为
from app.services import agent_tools

# caller.py 后续调用改为
result = await agent_tools.execute_tool(...)
tools_for_llm = await agent_tools.get_agent_tools_for_llm(agent_id) if agent_id else agent_tools.AGENT_TOOLS
```

这样 `agent_tools.execute_tool` 每次调用时都读取模块的最新属性，而非导入时的快照。

### 受影响的调用点

| 文件 | 行号 | 当前写法 | 修改后 |
|------|------|---------|--------|
| `caller.py` | 26 | `from app.services.agent_tools import AGENT_TOOLS, execute_tool, get_agent_tools_for_llm` | `from app.services import agent_tools` |
| `caller.py` | 258 | `await execute_tool(tool_name, args, ...)` | `await agent_tools.execute_tool(tool_name, args, ...)` |
| `caller.py` | 343 | `await get_agent_tools_for_llm(agent_id) if agent_id else AGENT_TOOLS` | `await agent_tools.get_agent_tools_for_llm(agent_id) if agent_id else agent_tools.AGENT_TOOLS` |

### 对其他模块的影响

`heartbeat.py:259` 同样使用 `from app.services.agent_tools import execute_tool`，但它在**函数体内**导入（lazy import），在 `load_plugins()` 之后执行，因此绑定的是已 patch 版本。**无需修改。**

## 编码规范

### 中文注释规范
- 每个新增日志语句上方保留中文注释，说明该日志的业务含义
- 函数文档字符串保持中文，与项目现有风格一致
- 日志消息本身使用英文前缀 + 中文说明的混合格式

### 项目风格一致性
- `router.py` 现有日志无方括号前缀（如 `LSP4J WS connected`），统一添加 `[LSP4J-LIFE]` 前缀
- `jsonrpc_router.py` 已有 `[LSP4J ←]`/`[LSP4J →]` 前缀，保持一致

### 代码复用
- `cleanup` 日志不与 `router.py:179` 重复 — `router.py` 已有 `LSP4J WS cleanup done`，`jsonrpc_router.py` 的 `cleanup()` 只记录内部 Future 清理详情

### 对智能体逻辑的影响
- P2-16（`_lsp4j_aware_execute_tool` 异常处理）：**只加日志不捕获异常**，异常继续向上传播到 `call_llm` 走 500 错误路径，不改变智能体的工具调用语义
- 所有日志均为纯观测性，不修改任何业务逻辑
- **修复 `from ... import` bug 不改变业务逻辑**，只是让工具路由正确工作

## 日志规范

### 日志前缀约定

| 前缀 | 用途 | 插件对应类 |
|------|------|-----------|
| `[LSP4J ←]` | 接收到的消息 | LanguageWebSocketService |
| `[LSP4J →]` | 发出的消息 | LanguageWebSocketService |
| `[LSP4J]` | 内部处理状态 | — |
| `[LSP4J-TOOL]` | 工具调用链路 | ToolInvokeProcessor / ToolService |
| `[LSP4J-LIFE]` | 生命周期事件 | LanguageClientImpl |

### 日志级别约定

| 级别 | 使用场景 |
|------|---------|
| `info` | 关键状态变更、请求开始/完成、路由决策 |
| `debug` | 消息内容详情、中间数据、高频回调（on_chunk 后续 chunk） |
| `warning` | 异常但可恢复、降级处理、数据缺失、验证拒绝 |
| `error` | 发送失败、持久化失败、不可恢复错误 |
| `exception` | 未预期异常（自动带堆栈） |

### 数据脱敏

- API Key / Token：只显示前4位 `api_key=sk-a***`
- 文件内容/代码：只显示长度 `content_len=1024`
- 大型参数：截断 `args={k: v[:50] for k, v in args.items()}`

## 当前代码日志状态

### 已有日志（38 处）

| 文件 | 已有日志点 |
|------|-----------|
| `jsonrpc_router.py` | `_dispatch`(←), `_handle_chat_ask`(开始/完成/BYOK/模型配置), `_send_chat_finish`, `_send_message`(→), `invoke_tool_on_ide`(入口/超时), `invoke_lsp4j_tool`(未找到/调用), `_persist_lsp4j_chat_turn`(跳过/持久化失败/前端通知失败, 1133行), `_load_lsp4j_history_from_db`(拒绝), `_handle_tool_invoke_result`(缺少toolCallId, 609行) |
| `tool_hooks.py` | `_lsp4j_aware_execute_tool`(入口/路径/结果), `_lsp4j_aware_get_tools`(注册), `install_lsp4j_tool_hooks`(已安装) |
| `router.py` | `WS connected`, `WS disconnected`, `WS error`, `WS cleanup done`, `_resolve_agent_override`(4处错误) |

### 缺失日志（约 36 处，其中 25 处已规划在下表）

**流程 A 回复（#13）— 6 处缺失：**
| 编号 | 函数 | 位置 | 缺失日志 | 严重度 |
|------|------|------|---------|--------|
| A1 | `_handle_chat_ask` | 362-364 | 空消息拒绝 | P0 |
| A2 | `_handle_chat_ask` | 370-372 | 并发拒绝 | P0 |
| A3 | `_handle_chat_ask` | 391-397 | 历史加载 | P1 |
| A4 | `on_chunk` | 409-417 | 块接收（首次用 info，后续用 debug） | P0 |
| A5 | `on_chunk` | 412-413 | 取消 | P0 |
| A6 | `_send_chat_answer` | 720-746 | 回答发送 | P0 |

**流程 B diff（#14）— 3 处缺失：**
| 编号 | 函数 | 位置 | 缺失日志 | 严重度 |
|------|------|------|---------|--------|
| B1 | `_handle_code_change_apply` | 948 | 入口 | P0 |
| B2 | `_handle_code_change_apply` | 949 | 空内容 | P1 |
| B3 | `_handle_code_change_apply` | 969 | finish通知 | P1 |

**流程 C 任务规划（#15）— 2 处缺失：**
| 编号 | 函数 | 位置 | 缺失日志 | 严重度 |
|------|------|------|---------|--------|
| C1 | `_send_process_step_callback` | 808 | **核心缺失：步骤推送** | P0 |
| C2 | `_handle_step_process_confirm` | 921 | 确认 | P1 |

**流程 D/E 改代码/本地工具（#16/#17）— 8 处缺失：**
| 编号 | 函数 | 位置 | 缺失日志 | 严重度 |
|------|------|------|---------|--------|
| D1-D5 | `_handle_response` | 636-659 | **全部缺失** | P0 |
| D6-D7 | `_handle_tool_invoke_result` | 615-626 | 成功/失败路径 | P0 |
| D8 | `_handle_tool_call_approve` | 587 | 审批审计 | P0 |

**流程 F 生命周期 — 7 处缺失：**
| 编号 | 函数 | 位置 | 缺失日志 | 严重度 |
|------|------|------|---------|--------|
| F1 | `_handle_initialize` | 303 | initialize | P1 |
| F2 | `_handle_shutdown` | 316 | shutdown | P1 |
| F3 | `_handle_exit` | 320 | exit | P1 |
| F4 | `_handle_chat_stop` | 573 | chat/stop | P0 |
| F5 | `cleanup` | 889 | 清理详情 | P0 |
| F7-F8 | `_resolve_model_by_key` | 164 | 找到/未找到 | P1 |

**流程 G 记忆持久化（#9/#10）— 3 处缺失：**
| 编号 | 函数 | 位置 | 缺失日志 | 严重度 |
|------|------|------|---------|--------|
| G1 | `_load_lsp4j_history_from_db` | 1193 | 成功 | P1 |
| G2 | `_load_lsp4j_history_from_db` | 1177-1178 | 已有 `LSP4J hydrate denied`（保持 `warning`，拒绝访问是安全事件） | P2 |
| G3 | `_persist_lsp4j_chat_turn` | 1122 | 成功 | P1 |

**流程 G' 前端通知（#9 补充）— 1 处缺失：**
| 编号 | 函数 | 位置 | 缺失日志 | 严重度 |
|------|------|------|---------|--------|
| G4 | `_persist_lsp4j_chat_turn` | 1127 | 前端通知结果（`send_to_session` 调用后，1133行为失败日志） | P1 |

**流程 H 工具钩子 — 2 处缺失：**
| 编号 | 函数 | 位置 | 缺失日志 | 严重度 |
|------|------|------|---------|--------|
| H1 | `install_lsp4j_tool_hooks` | 229 | 重复 | P2 |
| H2 | `_lsp4j_aware_execute_tool` | — | 异常（只加日志不吞异常） | P1 |

## 受影响文件

| 文件 | 修改类型 |
|------|---------|
| `backend/app/services/llm/caller.py` | **修复 `from ... import` 绑定 bug（前置修复）** |
| `backend/app/plugins/clawith_lsp4j/jsonrpc_router.py` | 添加约 25 处日志 |
| `backend/app/plugins/clawith_lsp4j/tool_hooks.py` | 添加约 3 处日志 |
| `backend/app/plugins/clawith_lsp4j/router.py` | 统一已有日志的前缀（10 处） |

## 实现细节

按功能流组织，每条日志标注对应的需求编号（#13-#19）和插件源码对应类。

### 前置修复：caller.py 的 `from ... import` 绑定 bug

| # | 文件 | 位置 | 修改 | 优先级 |
|---|------|------|------|--------|
| P0-IMPORT | `caller.py` | 26 | `from app.services.agent_tools import ...` → `from app.services import agent_tools` | **P0-Critical** |
| P0-CALL | `caller.py` | 258 | `await execute_tool(...)` → `await agent_tools.execute_tool(...)` | **P0-Critical** |
| P0-TOOLS | `caller.py` | 343 | `await get_agent_tools_for_llm(agent_id) if agent_id else AGENT_TOOLS` → `await agent_tools.get_agent_tools_for_llm(agent_id) if agent_id else agent_tools.AGENT_TOOLS` | **P0-Critical** |

**验证方法**：修复后启动服务，在 LSP4J 会话中让 LLM 调用 `read_file`，日志中应看到 `[LSP4J-TOOL] execute_tool: name=read_file lsp4j_ws=True is_lsp4j_tool=True`。如果看不到此日志，说明 bug 未修复。

### 流程 A：灵码中能回复（#13）

**调用链**：IDE → `chat/ask` → `_handle_chat_ask` → `on_chunk` → `_send_chat_answer` → `_send_chat_finish` → `_persist_lsp4j_chat_turn`

**插件对应**：`ChatService.java` → `BaseChatPanel.java`

| # | 函数 | 位置 | 日志代码 | 级别 | 优先级 |
|---|------|------|---------|------|--------|
| A1 | `_handle_chat_ask` | 362-364 | `logger.warning("[LSP4J] chat/ask rejected: empty questionText, requestId={}", request_id)` | warning | P0 |
| A2 | `_handle_chat_ask` | 370-372 | `logger.warning("[LSP4J] chat/ask rejected: concurrent request, requestId={} current={}", request_id, self._current_request_id)` | warning | P0 |
| A3 | `_handle_chat_ask` | 391-397 | `logger.debug("[LSP4J] history loaded: {} messages, session_id={}", len(message_history), session_id)` | debug | P1 |
| A4 | `on_chunk` | 409-417 | 首次 chunk：`logger.info("[LSP4J] on_chunk: first_chunk text_len={} requestId={}", len(text), request_id)`；后续：`logger.debug("[LSP4J] on_chunk: chunk_count={} text_len={}", len(reply_parts), len(text))` | info/debug | P0 |
| A5 | `on_chunk` | 412-413 | `logger.info("[LSP4J] on_chunk: cancelled by chat/stop, chunks_sent={}", len(reply_parts))` | info | P0 |
| A6 | `_send_chat_answer` | 720-746 | `logger.debug("[LSP4J] chat/answer: requestId={} text_len={}", request_id, len(text))` | debug | P0 |

**v3 修订**：A4 首次 chunk 改为 info 级别（生产环境可观测），后续 chunk 保持 debug（避免高频噪音）。

### 流程 B：灵码的 diff 能力（#14）

**调用链**：IDE → `chat/codeChange/apply` → `_handle_code_change_apply` → 响应 + `apply/finish` 通知

**插件对应**：`ChatService.java` → `LanguageClientImpl.chatCodeChangeApplyFinished()` → `CodeChangeApplyPanel.java`

| # | 函数 | 位置 | 日志代码 | 级别 | 优先级 |
|---|------|------|---------|------|--------|
| B1 | `_handle_code_change_apply` | 948 | `logger.info("[LSP4J] codeChange/apply: applyId={} filePath={} requestId={} codeEdit_len={}", apply_id, file_path, request_id, len(code_edit))` | info | P0 |
| B2 | `_handle_code_change_apply` | 949 | `logger.warning("[LSP4J] codeChange/apply: empty codeEdit, applyId={} filePath={}", apply_id, file_path)` | warning | P1 |
| B3 | `_handle_code_change_apply` | 969 | `logger.debug("[LSP4J] codeChange/apply/finish sent: applyId={}", apply_id)` | debug | P1 |

### 流程 C：灵码的任务规划/任务列表（#15）

**调用链**：Clawith → `_send_process_step_callback` → IDE 渲染步骤 → 用户确认 → `_handle_step_process_confirm`

**插件对应**：`ChatProcessStepCallbackParams` → `CheckEnvFooterPanel` / `GenerateCaseFooterPanel`

| # | 函数 | 位置 | 日志代码 | 级别 | 优先级 |
|---|------|------|---------|------|--------|
| C1 | `_send_process_step_callback` | 808 | `logger.info("[LSP4J] process_step_callback: requestId={} step={} status={} desc={}", request_id, step, status, description[:80])` | info | P0 |
| C2 | `_handle_step_process_confirm` | 921 | `logger.info("[LSP4J] stepProcessConfirm: requestId={} params_keys={}", params.get("requestId", ""), list(params.keys()))` | info | P1 |

### 流程 D：灵码的改代码能力（#16）

**调用链**：Clawith → `tool/invoke(save_file/replace_text_by_path/create_file_with_text)` → IDE `ToolInvokeProcessor` → `_handle_response` / `_handle_tool_invoke_result`

**插件对应**：`LanguageClient.toolInvoke()` → `ToolInvokeProcessor` → `SaveFileToolHandler` / `ReplaceTextByPathToolHandler` / `CreateNewFileWithTextToolHandler`

| # | 函数 | 位置 | 日志代码 | 级别 | 优先级 |
|---|------|------|---------|------|--------|
| D1 | `_handle_response` | 640 | `logger.warning("[LSP4J-TOOL] 收到未匹配的响应: id={}", msg_id)` | warning | P0 |
| D2 | `_handle_response` | 642-644 | `logger.warning("[LSP4J-TOOL] 工具响应错误: id={} code={} msg={}", msg_id, error.get("code"), error.get("message"))` | warning | P0 |
| D3 | `_handle_response` | 649-655 | `logger.info("[LSP4J-TOOL] 工具执行成功: id={} name={}", msg_id, result.get("name", "unknown"))` | info | P0 |
| D4 | `_handle_response` | 656-657 | `logger.warning("[LSP4J-TOOL] 工具执行失败: id={} name={} error={}", msg_id, result.get("name"), result.get("errorMessage", ""))` | warning | P0 |
| D5 | `_handle_response` | 641 | `logger.debug("[LSP4J-TOOL] Future 已完成/不存在，忽略响应: id={}", msg_id)` | debug | P1 |

**分支结构说明**（对应代码 640-659 行）：
```
if future and not future.done():
    if "error" in msg:     → D2 日志
    else:
        if success:        → D3 日志
        else:              → D4 日志
else:
    if future is None:     → D1 日志
    elif future.done():    → D5 日志
```

| D6 | `_handle_tool_invoke_result` | 615-626 | `logger.info("[LSP4J-TOOL] invokeResult: toolCallId={} success={} name={}", tool_call_id, params.get("success", True), params.get("name"))` | info | P0 |
| D7 | `_handle_tool_invoke_result` | 614-615 | `logger.warning("[LSP4J-TOOL] invokeResult: toolCallId={} 无匹配 Future（可能已超时）", tool_call_id)` | warning | P1 |
| D8 | `_handle_tool_call_approve` | 587 | `logger.info("[LSP4J-TOOL] 工具审批: toolCallId={} name={} → 自动批准", params.get("toolCallId"), params.get("name"))` | info | P0 |

### 流程 E：灵码的本地工具（#17）

**调用链**：同流程 D，工具名为 `read_file`/`run_in_terminal`/`get_terminal_output`/`get_problems`/`delete_file_by_path`

**插件对应**：`ReadFileToolHandler` / `RunTerminalToolHandlerV2` / `GetTerminalOutputToolHandler` / `GetProblemsToolHandler` / `DeleteFileByPathToolHandler`

日志已由流程 D 的 D1-D8 覆盖，无需额外添加。`tool_hooks.py` 中已有的 `[LSP4J-TOOL] execute_tool` 和 `[LSP4J-TOOL] 注册工具` 日志覆盖注册和执行路由。

**v3 注意**：此流程依赖前置修复 `caller.py` 的 `from ... import` bug。修复前，`tool_hooks.py` 中的日志不会被 `call_llm` 触发。

### 流程 F：生命周期 + 基础设施

| # | 函数 | 位置 | 日志代码 | 级别 | 优先级 |
|---|------|------|---------|------|--------|
| F1 | `_handle_initialize` | 303 | `logger.info("[LSP4J-LIFE] initialize: params_keys={}", list(params.keys()))` | info | P1 |
| F2 | `_handle_shutdown` | 316 | `logger.info("[LSP4J-LIFE] shutdown")` | info | P1 |
| F3 | `_handle_exit` | 320 | `logger.info("[LSP4J-LIFE] exit")` | info | P1 |
| F4 | `_handle_chat_stop` | 573 | `logger.info("[LSP4J] chat/stop: requestId={} cancel_set={}", params.get("requestId", ""), self._cancel_event.is_set() if self._cancel_event else False)` | info | P0 |
| F5 | `cleanup` | 889 | `logger.info("[LSP4J-LIFE] 连接断开清理: pending_tools={} pending_responses={}", tool_count, resp_count)` | info | P0 |
| F6 | `_handle_stub` | — | 不额外添加，避免 12 个存根方法产生噪音；`_dispatch` 已记录方法名 | — | 不添加 |
| F7 | `_resolve_model_by_key` | 164 | `logger.debug("[LSP4J] 模型查找: key={} → model={} provider={}", model_key, model.model, model.provider)` | debug | P1 |
| F8 | `_resolve_model_by_key` | 164 | `logger.warning("[LSP4J] 模型查找失败: key={}", model_key)` | warning | P0 |
| F9 | `router.py` 已有日志统一前缀 | — | `LSP4J WS connected` → `[LSP4J-LIFE] WS connected` 等（共 10 处） | info | P1 |

### 流程 G：记忆与持久化（#9, #10）

| # | 函数 | 位置 | 日志代码 | 级别 | 优先级 |
|---|------|------|---------|------|--------|
| G1 | `_load_lsp4j_history_from_db` | 1193 | `logger.debug("[LSP4J] history loaded from DB: session_id={} count={}", session_id, len(result))` | debug | P1 |
| G2 | `_load_lsp4j_history_from_db` | 1177-1178 | 已有 `LSP4J hydrate denied`（保持 `warning`，拒绝访问是安全事件） | warning | P2 |
| G3 | `_persist_lsp4j_chat_turn` | 1122 | `logger.debug("[LSP4J] persist success: session_id={} user_len={} reply_len={}", session_id, len(user_text), len(reply_text))` | debug | P1 |
| G4 | `_persist_lsp4j_chat_turn` | 1131 | `logger.debug("[LSP4J] 前端通知已发送: session_id={}", sid_normalized)` | debug | P1 |

**v3 新增 G4**：补充前端 WebSocket 通知日志，用于验证 #9（Web UI 对话可见）。

### 流程 H：工具钩子增强（tool_hooks.py）

| # | 函数 | 位置 | 日志代码 | 级别 | 优先级 |
|---|------|------|---------|------|--------|
| H1 | `install_lsp4j_tool_hooks` | 229 | `logger.debug("[LSP4J-TOOL] 工具钩子已安装，跳过")` | debug | P2 |
| H2 | `_lsp4j_aware_execute_tool` | — | `logger.exception("[LSP4J-TOOL] LSP4J 工具调用异常: tool={} error={}", tool_name, e)` | exception | P1 |
| **注意** | H2 只加日志不捕获异常，异常继续向上传播 | — | `logger.exception(...) + raise` | — | — |

## 不添加日志的函数（精简原则 #5）

| 函数 | 原因 |
|------|------|
| `_send_chat_think` | 低频且已有 `_send_message` 的 `[LSP4J →]` 追踪 |
| `_handle_stub` | 12 个存根方法，避免噪音；`_dispatch` 已记录方法名 |
| `_send_response` / `_send_error_response` / `_send_request` / `_send_notification` | 均委托给 `_send_message`，已有追踪 |
| `_next_request_id` | 纯工具函数 |
| `route()` ParseError | 发送的 error response 已被 `_send_message` 记录 |

## 插件源码验证摘要

基于 `/Users/shubinzhang/Downloads/demo-new` 验证的关键发现：

| 验证项 | 插件源码 | Clawith 实现 | 状态 |
|--------|---------|-------------|------|
| statusCode 语义 | `BaseChatPanel.stopGenerate()`: 200=成功，500=服务端错误，429=限流等 16 种 | `statusCode: 200` 已修复 | ✅ |
| tool/invoke 字段 | `ToolInvokeRequest`: requestId, toolCallId, name, parameters, async | 字段名完全匹配 | ✅ |
| codeChange/apply 字段 | `ChatCodeChangeApplyResult`: applyId, projectPath, filePath, applyCode, requestId, sessionId, extra, sessionType, mode | 9 字段完全匹配 | ✅ |
| requestId→Project 映射 | `LanguageClientImpl.toolInvoke()`: 先查 `CosyKey.REQUEST_TO_PROJECT`，失败回退 `ProjectUtils.getActiveProject()` | 每次生成新 requestId，不在 map 中，依赖回退 | ⚠️ 需验证回退是否成功 |
| ChatService 方法数 | `@JsonSegment("chat")` 下 15 个 `@JsonRequest` | `_METHOD_MAP` 覆盖全部 15 个 + 扩展方法 | ✅ |

## 19 条需求覆盖情况

| # | 需求 | 状态 | 说明 |
|---|------|------|------|
| 1 | 中文注释 | ✅ | 每个日志上方保留中文注释 |
| 2 | 网络最佳实践 | ✅ | 结构化前缀 `[LSP4J ←]`/`[LSP4J →]` |
| 3 | 官方文档 | ✅ | 每条日志标注插件对应 Java 类；补充 JSON-RPC 2.0 规范 |
| 4 | 项目实际代码 | ✅ | 基于实际审计，发现并修复 `caller.py` 绑定 bug |
| 5 | 代码精简 | ✅ | P2 已精简 |
| 6 | 代码复用 | ✅ | cleanup 不与 router.py 重复；D1-D5 侧重业务语义，`_send_message` 侧重协议层 |
| 7 | 项目风格 | ⚠️ | `router.py` 前缀需统一（Task 1 已规划 10 处） |
| 8 | 智能体逻辑影响 | ✅ | 不改变异常传播；修复 `from ... import` 不改变业务逻辑 |
| 9 | Web UI 对话可见 | ✅ | G3 覆盖持久化成功；G4 覆盖前端通知送达 |
| 10 | 保存对话记忆 | ✅ | G1 覆盖历史加载 |
| 11 | 插件源码 | ✅ | 已标注对应类 |
| 12 | 源码相互验证 | ✅ | 补充 requestId→Project 回退机制验证说明 |
| 13 | 灵码能回复 | ✅ | A1-A6 已规划 |
| 14 | diff 能力 | ✅ | B1-B3 已规划 |
| 15 | 任务规划 | ✅ | C1-C2 已规划 |
| 16 | 改代码 | ✅ | D1-D8 已规划；依赖前置修复 |
| 17 | 本地工具 | ✅ | D1-D8 + hooks 覆盖；依赖前置修复 |
| 18 | 功能都要实现 | ✅ | 全部覆盖；前置修复是功能工作的前提 |
| 19 | 完整调用链日志 | ✅ | 5 个功能流全链路覆盖；前置修复确保调用链真正工作 |

## 预期结果

- **修复 `caller.py` 的 `from ... import` bug，使 LSP4J 工具桥真正工作**
- LSP4J 模块日志总数从 38 增加到约 69（31 处新增 + 38 处已有）
- 5 个功能流（回复/diff/任务规划/改代码/本地工具）全链路可追踪
- 生命周期事件完整记录：连接 → 初始化 → 对话 → 断开
- 异常场景有 warning/error 级别日志，不再静默失败
- 不改变任何业务逻辑和异常传播行为
