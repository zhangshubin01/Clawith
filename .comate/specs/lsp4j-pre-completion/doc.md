# LSP4J 插件协议补全：方法注册 + 参数映射 + 工具审批

## 需求场景

通义灵码 IDE 插件与 Clawith 后端通过 LSP4J JSON-RPC 通信。当前存在以下问题：

1. **`textDocument/preCompletion` 未注册** — 返回 `-32601 Method not found`，IDE 补全请求失败
2. **`create_file_with_text` 参数名不匹配** — 后端发送 `"content"` 键，但插件 `CreateNewFileWithTextToolHandler.getRequestText()` 查找 `"text"` 键，导致 NPE
3. **`tool/call/approve` 忽略拒绝逻辑** — `approval=false`（用户拒绝）时未取消 pending Future
4. **`textDocument/inlineEdit` 返回格式不符** — stub 返回 `{}`，不符合 `InlineEditResult{success, message}` 协议
5. **`textDocument/editPredict` 响应类型不准确** — 响应为 `Void`，stub 返回 `{}` 可以接受但 `null` 更准确
6. **`save_file` 工具参数名不匹配** — 后端定义 `file_path`，但插件期望 `filePath`（camelCase）

## 插件协议源码验证

基于 `/Users/shubinzhang/Downloads/demo-new` 插件源码验证。

### 1. textDocument/preCompletion
- **源码**: `TextDocumentService.java:314`, `PreCompletionParams.java`
- **请求参数**: `requestId`, `fileContent`, `triggerMode`, `textDocument`(uri), `position`(line/character)
- **响应**: `CompletableFuture<Void>` — fire-and-forget，返回 null 即可
- **日志证据** (idea.log):
  ```
  [LSP4J ←] method=textDocument/preCompletion id=7 params_keys=['requestId', 'fileContent', 'triggerMode', 'textDocument', 'position']
  [LSP4J →] error id=7 code=-32601 msg=Method not found: textDocument/preCompletion
  ```

### 2. create_file_with_text 参数名不匹配 (严重 BUG)
- **插件源码**: `CreateNewFileWithTextToolHandler.java:68-70`
  ```java
  public String getRequestText(ToolInvokeRequest request) {
      return request.getParameters() != null && request.getParameters().containsKey("text")
          ? this.getStringParamValue(request, "text")
          : null;  // ← "content" 键不存在时返回 null
  }
  ```
- **Clawith 后端定义** (`tool_hooks.py:186`): LLM 工具参数名为 `"content"`
- **IDE 日志证据** (idea.log):
  ```
  [LSP] toolInvoke request: {...,"name":"create_file_with_text","parameters":{"filePath":"...","content":"package siamr..."},"async":true}
  [LSP] reportToolInvokeResult request: {...,"success":false,"errorMessage":"Cannot invoke \"String.getBytes(java.nio.charset.Charset)\" because \"finalText\" is null"}
  ```
- **根因**: 后端发 `{"filePath":"...", "content":"..."}` → 插件找 `"text"` 键找不到 → `finalText=null` → `null.getBytes()` NPE
- **修复**: 在 `_lsp4j_aware_execute_tool` 中，当工具名为 `create_file_with_text` 时，将 `"content"` 映射为 `"text"`

### 3. save_file content 参数无效（非参数名不匹配）
- **插件源码**: `SaveFileToolHandler.java` — 使用 `getRequestFilePathWithUnderLine()` 读取 `file_path`（snake_case，参数名正确），**不读取 content/text 参数**
- **Clawith 后端定义** (`tool_hooks.py:86-111`): 参数名为 `file_path`（正确）和 `content`（无效）
- **修复**: 移除 `save_file` 工具定义中的 `content` 参数（插件忽略该参数，误导 LLM）

### 4. tool/call/approve（审批 + 拒绝）
- **插件源码**: `ToolCallService.java:16`, `ToolCallApproveRequest.java`
- **请求参数**: `sessionId`, `requestId`, `toolCallId`, **`approval`(boolean)**
- **关键**: 无独立 reject 方法！拒绝 = `approve(approval=false)`
- **当前后端实现**: 忽略 `approval` 字段，一律返回 `{}`（自动批准）
- **正确逻辑**: `approval=false` 时应取消对应 pending tool Future，让 LLM 收到拒绝信息

### 5. chat/codeChange/apply（Diff 渲染入口）
- **已实现**: 后端 `_handle_code_change_apply` 正确返回 `applyCode`
- **Diff 流程**: 客户端收到 `applyCode` → JGit 计算行级 diff → 红绿高亮 → Accept/Reject 纯客户端行为
- **结论**: Diff 比对、同意、拒绝的渲染逻辑全在 IDE 客户端，后端无需改动

### 6. textDocument/inlineEdit
- **源码**: `TextDocumentService.java:308`, `InlineEditParams.java`
- **响应**: `InlineEditResult{success: boolean, message: String}`
- **当前**: `_handle_stub` 返回 `{}`，不符合协议

### 7. textDocument/editPredict
- **源码**: `TextDocumentService.java:318`, `CompletionEditPredictParams.java`
- **响应**: `CompletableFuture<Void>` — fire-and-forget

## 技术方案

### 修改文件

| 文件 | 修改内容 |
|------|----------|
| `backend/app/plugins/clawith_lsp4j/jsonrpc_router.py` | 添加 preCompletion/inlineEdit 处理器，改进 tool_call_approve |
| `backend/app/plugins/clawith_lsp4j/tool_hooks.py` | 修复 create_file_with_text 和 save_file 参数映射 |

### 具体实现

#### A. 参数名映射 (tool_hooks.py) — 最高优先级

在 `_lsp4j_aware_execute_tool` 中添加参数名映射逻辑：

```python
# ★ 参数名映射：后端工具定义 → 插件 ToolHandler 期望的参数名
# 插件 ToolHandler 统一使用 camelCase（filePath, text），
# 而后端工具定义使用 snake_case（file_path, content）
_PARAM_NAME_MAP = {
    "create_file_with_text": {"content": "text"},
    "save_file": {"file_path": "filePath", "content": "text"},
}

# 在调用 invoke_lsp4j_tool 前执行参数映射
if tool_name in _PARAM_NAME_MAP:
    name_map = _PARAM_NAME_MAP[tool_name]
    args = {name_map.get(k, k): v for k, v in args.items()}
```

#### B. 添加 `_handle_pre_completion` (jsonrpc_router.py)

```python
async def _handle_pre_completion(self, params: dict, msg_id: Any) -> None:
    """处理 textDocument/preCompletion — IDE 补全预请求。"""
    logger.debug("[LSP4J] preCompletion: requestId={} triggerMode={}",
                 params.get("requestId", ""), params.get("triggerMode", ""))
    await self._send_response(msg_id, None)
```

#### C. 添加 `_handle_inline_edit` (jsonrpc_router.py)

```python
async def _handle_inline_edit(self, params: dict, msg_id: Any) -> None:
    """处理 textDocument/inlineEdit — 行内编辑建议。"""
    await self._send_response(msg_id, {"success": False, "message": ""})
```

#### D. 改进 `_handle_tool_call_approve` (jsonrpc_router.py)

```python
async def _handle_tool_call_approve(self, params: dict, msg_id: Any) -> None:
    tool_call_id = params.get("toolCallId", "")
    approved = params.get("approval", True)
    if not approved:
        logger.info("[LSP4J-TOOL] 工具审批拒绝: toolCallId={}", tool_call_id[:8] if tool_call_id else "")
        if tool_call_id:
            future = self._pending_tools.get(str(tool_call_id))
            if future and not future.done():
                future.set_result("[用户拒绝] 工具调用已被用户拒绝")
    else:
        logger.info("[LSP4J-TOOL] 工具审批通过: toolCallId={}", tool_call_id[:8] if tool_call_id else "")
    await self._send_response(msg_id, {})
```

#### E. 更新 `_METHOD_MAP` (jsonrpc_router.py)

```python
"textDocument/preCompletion": _handle_pre_completion,
"textDocument/inlineEdit": _handle_inline_edit,
```

同时更新 `_handle_stub` 对 `textDocument/inlineEdit` 的引用（从 stub 改为 inline_edit）。

## 边界条件

- `preCompletion` 响应 `Void`，LSP4J 对 `null` result 正常处理
- `tool/call/approve` 的 `approval` 可能缺失（旧版插件），默认 true 保持兼容
- 拒绝工具调用时 `future.done()` 检查防止重复设置
- `inlineEdit` 返回 `success=false` 让插件静默跳过
- 参数映射仅在 LSP4J 路径生效，ACP 路径不受影响
- Diff 比对/Accept/Reject 全在客户端，后端无需改动 `_handle_code_change_apply`

## 预期结果

- `create_file_with_text` 不再 NPE，文件创建正常工作
- `save_file` 参数名匹配，保存文件正常工作
- 不再出现 `textDocument/preCompletion` 的 `-32601` 错误
- 用户拒绝工具调用时，LLM 收到 `[用户拒绝]` 信息
- `inlineEdit` 返回符合协议的 `InlineEditResult` 格式

---

## 深度交叉验证发现（第二轮）

对插件所有 JSON-RPC 接口与后端 `_METHOD_MAP` 做了完整的交叉引用，并检查了最新日志。

### 8. `save_file` 语义不匹配（HIGH）
- **插件源码**: `SaveFileToolHandler.java:39-67`
- **实际行为**: 调用 `FileDocumentManager.getInstance().saveDocument(document)` — 只是持久化 IDE 内存中的文档到磁盘
- **后端定义**: 参数 `content`(required) 暗示"写入指定内容"
- **影响**: `content` 参数被完全忽略。如果 LLM 发 `save_file(file_path="...", content="new text")`，content 不会写入文件
- **真实流程**: LLM 先调 `replace_text_by_path` 修改内容，再调 `save_file` 保存 → `content` 参数实际无用
- **修复**: 后端工具定义移除 `content` 参数（或标记为 optional），更新描述为"保存 IDE 编辑器中已修改的文件"

### 9. `run_in_terminal` 参数缺失（MEDIUM）
- **`workDirectory`**: 后端定义了但插件忽略（始终用 `project.getBasePath()`）
- **`isBackground`**: 插件 `RunTerminalToolHandlerV2.java:98` 读取 `isBackground` 参数，但后端工具定义中不存在
- **影响**: 无法触发后台终端模式；指定工作目录无效
- **修复**: 后端添加 `isBackground` 参数定义；移除或标注 `workDirectory` 为无效参数

### 10. 服务端→客户端 协议类型不匹配（CRITICAL — 需验证）

后端 `_send_notification()` 发送的消息**没有 `id` 字段**（JSON-RPC notification），但插件的 `LanguageClient.java` 将以下方法定义为 `@JsonRequest`（期望 `id` 字段）：

| 后端发送方式 | 插件 LanguageClient.java 定义 | 影响 |
|-------------|-------------------------------|------|
| `_send_notification("chat/answer")` | `@JsonRequest("chat/answer")` L325 | 流式文本可能无法到达 UI |
| `_send_notification("chat/think")` | `@JsonRequest("chat/think")` L358 | 思考过程显示可能失效 |
| `_send_notification("chat/finish")` | `@JsonRequest("chat/finish")` L336 | 聊天完成信号可能不到达 |
| `_send_notification("chat/process_step_callback")` | `@JsonRequest("chat/process_step_callback")` L314 | 步骤回调可能不处理 |
| `_send_notification("session/title/update")` | `@JsonRequest("session/title/update")` L666 | 会话标题不更新 |
| `_send_notification("tool/call/sync")` | `@JsonRequest("tool/call/sync")` L633 | 工具调用状态同步失效 |
| `_send_notification("commitMsg/answer")` | `@JsonRequest("commitMsg/answer")` L435 | Commit 消息流式输出失效 |
| `_send_notification("commitMsg/finish")` | `@JsonRequest("commitMsg/finish")` L446 | Commit 消息完成信号失效 |
| `_send_notification("chat/codeChange/apply/finish")` | `@JsonRequest("chat/codeChange/apply/finish")` L644 | 代码变更完成信号失效 |

**代码中的注释自相矛盾**（`jsonrpc_router.py:1325-1328`）：
```python
# 注释说"我们发送带 id 的请求"，但代码实际发送的是不带 id 的 notification
# "实际上 LSP4J Launcher 会自动处理未匹配的响应"
```

**注意**: 系统当前似乎在正常运行，可能有以下原因之一：
1. 插件有自定义消息处理逻辑绕过了 LSP4J 标准分发
2. LSP4J 的 ReflectiveEndpoint 实际上也处理 notification → @JsonRequest 的分发
3. 插件版本与后端之间存在某种适配

**修复**: 验证插件实际行为后，将 `_send_notification` 改为 `_send_request`（带 `id` 字段），或在插件侧为这些方法添加 `@JsonNotification` 注解

### 11. `config/updateGlobal` 等通知未处理（LOW）
- 用户在 IDE 中修改设置后，后端收不到通知，可能导致配置状态过时
- 涉及方法：`settings/change`, `config/updateGlobal`, `config/updateGlobalMcpAutoRun` 等
- 当前：这些通知被后端静默丢弃（不在 `_METHOD_MAP` 也不在忽略列表中）

### 12. 工具调用超时模式（MEDIUM — 运维问题）
- 今日 7 次 `read_file`/`run_in_terminal` 工具调用超时
- 6 次 `invokeResult` 孤儿 Future（工具已超时但结果迟到了）
- `run_in_terminal` 120s 超时可能不够复杂命令使用

### 13. Heartbeat LLM 调用失败（LOW — 网络/配置问题）
- 09:24 DNS 解析失败影响 14 个 agent
- 13:26-13:28 连接断开 + 远程协议错误
- 散布全天的 ReadTimeout（06:09, 13:27, 13:28, 14:18）

### 14. 默认密钥未更换（LOW — 安全提醒）
- `SECRET_KEY`/`JWT_SECRET_KEY` 仍为 `change-me` 默认值
