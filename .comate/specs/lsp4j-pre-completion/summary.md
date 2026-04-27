# LSP4J 插件协议补全 — 实施总结

## 修改文件

| 文件 | 修改内容 |
|------|----------|
| `backend/app/plugins/clawith_lsp4j/tool_hooks.py` | 参数名映射、save_file 定义修复、run_in_terminal 参数补全 |
| `backend/app/plugins/clawith_lsp4j/jsonrpc_router.py` | preCompletion/inlineEdit/editPredict 处理器、tool/call/approve 拒绝逻辑、_send_client_request 方法 |

## 任务完成情况

### Task 1: create_file_with_text 参数名映射 (CRITICAL)
- 添加 `_PARAM_NAME_MAP = {"create_file_with_text": {"content": "text"}}`
- 在 `_lsp4j_aware_execute_tool` 中，调用 `invoke_lsp4j_tool` 前应用映射
- 修复 NPE：后端发 `content` → 映射为 `text` → 插件 `getRequestText()` 找到键 → 文件创建正常

### Task 2: save_file 工具定义语义修复 (HIGH)
- 移除 `save_file` 中的 `content` 参数（插件 `SaveFileToolHandler` 不读取 content/text，只保存当前文档到磁盘）
- 更新描述为"保存 IDE 编辑器中已修改的文件到磁盘"
- `file_path` 参数名确认正确（插件使用 `getRequestFilePathWithUnderLine` 读取 `file_path`）

### Task 3: textDocument/preCompletion 处理器 (HIGH)
- 添加 `_handle_pre_completion`：返回 `None`（Void 响应），消除 `-32601 Method not found` 错误
- 在 `_METHOD_MAP` 注册 `"textDocument/preCompletion"`

### Task 4: tool/call/approve 拒绝逻辑 (HIGH)
- 修改 `_handle_tool_call_approve`，读取 `approval` 字段
- `approval=false` 时查找 `_pending_tools` 中的 Future 并 `set_result("[用户拒绝] ...")`
- `future.done()` 检查防止重复设置，`approval` 缺失时默认 `true` 保持兼容

### Task 5: textDocument/inlineEdit 和 editPredict 响应格式 (MEDIUM)
- `_handle_inline_edit`：返回 `{"success": False, "message": ""}`（符合 InlineEditResult 协议）
- `_handle_edit_predict`：返回 `None`（Void 响应）
- 从 `_METHOD_MAP` 的 `_handle_stub` 改为独立处理器

### Task 6: run_in_terminal 参数补全 (MEDIUM)
- 添加 `isBackground`(boolean, optional) 参数（插件 `RunTerminalToolHandlerV2` 会读取）
- 移除 `workDirectory` 参数（插件始终使用 `project.getBasePath()`，忽略该参数）

### Task 7: 服务端→客户端协议类型修复 (CRITICAL)
- **确认问题**：插件使用标准 LSP4J `GenericEndpoint` 分发，notification（无 `id`）不会触发 `@JsonRequest` 处理器
- **影响**：`chat/answer`、`chat/think`、`chat/finish` 等 9 个方法发送的消息被插件静默忽略
- **修复方案**：新增 `_send_client_request` 方法，发送带 `id` 的请求但不注册 pending_response Future
- **变更**：10 个调用点从 `_send_notification` 切换到 `_send_client_request`
- **保留**：`image/uploadResultNotification` 继续使用 `_send_notification`（插件中为 `@JsonNotification`）

## 修正的重要发现

在 Task 1 实施过程中，发现 doc.md 中关于 `save_file` 参数名的描述有误：
- 原描述：插件使用 `getRequestFilePath()` 读取 `filePath`（camelCase），需映射 `file_path→filePath`
- 实际情况：插件使用 `getRequestFilePathWithUnderLine()` 读取 `file_path`（snake_case），参数名本身正确
- `save_file` 的真正问题是 `content` 参数被完全忽略（插件只做 `FileDocumentManager.saveDocument`）

## 未修改的文件

以下问题已记录但未在本轮修改（属于运维/配置层面）：
- Heartbeat LLM 调用失败（DNS/连接/超时）— 网络问题
- 默认 SECRET_KEY 未更换 — 安全配置
- `config/updateGlobal` 等通知丢失 — LOW 优先级
