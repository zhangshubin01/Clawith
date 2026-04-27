# LSP4J 插件协议补全 — 任务计划

- [x] Task 1: 修复 `create_file_with_text` 和 `save_file` 参数名映射（CRITICAL）
    - 1.1: 在 `tool_hooks.py` 添加 `_PARAM_NAME_MAP` 字典：`create_file_with_text` 的 `content→text`
    - 1.2: 在 `_lsp4j_aware_execute_tool` 中，调用 `invoke_lsp4j_tool` 前应用参数名映射
    - 1.3: 验证映射仅在 LSP4J 路径生效，ACP 路径不受影响

- [x] Task 2: 修复 `save_file` 工具定义语义（HIGH）
    - 2.1: 移除 `save_file` 工具定义中的 `content` 参数（插件忽略该参数，误导 LLM）
    - 2.2: 更新 `save_file` 描述为"保存 IDE 编辑器中已修改的文件到磁盘"

- [x] Task 3: 添加 `textDocument/preCompletion` 处理器（HIGH）
    - 3.1: 在 `jsonrpc_router.py` 添加 `_handle_pre_completion` 方法，返回 `None`（Void 响应）
    - 3.2: 在 `_METHOD_MAP` 注册 `"textDocument/preCompletion": _handle_pre_completion`

- [x] Task 4: 修复 `tool/call/approve` 拒绝逻辑（HIGH）
    - 4.1: 修改 `_handle_tool_call_approve`，读取 `approval` 字段
    - 4.2: 当 `approval=false` 时，查找 `_pending_tools` 中的 Future 并 `set_result("[用户拒绝] ...")`
    - 4.3: 添加 `future.done()` 检查防止重复设置
    - 4.4: `approval` 缺失时默认 `true` 保持向后兼容

- [x] Task 5: 修复 `textDocument/inlineEdit` 和 `editPredict` 响应格式（MEDIUM）
    - 5.1: 将 `_METHOD_MAP` 中 `textDocument/inlineEdit` 从 `_handle_stub` 改为独立处理器
    - 5.2: 实现 `_handle_inline_edit`，返回 `{"success": False, "message": ""}`
    - 5.3: 同样处理 `textDocument/editPredict`，实现 `_handle_edit_predict` 返回 `None`（Void 响应）

- [x] Task 6: 补全 `run_in_terminal` 参数定义（MEDIUM）
    - 6.1: 在 `run_in_terminal` 工具定义中添加 `isBackground`(boolean, optional) 参数
    - 6.2: 移除无效的 `workDirectory` 参数（插件忽略该参数）

- [x] Task 7: 修复服务端→客户端协议类型（CRITICAL — 已确认并修复）
    - 7.1: 验证插件 LSP4J 使用标准 GenericEndpoint 分发，确认 notification→@JsonRequest 不可达
    - 7.2: 新增 `_send_client_request` 方法：发送带 `id` 的请求但不注册 pending_response Future
    - 7.3: 将 chat/answer, chat/think, chat/finish, chat/process_step_callback, session/title/update, tool/call/sync, commitMsg/answer, commitMsg/finish, chat/codeChange/apply/finish 改用 `_send_client_request`
    - 7.4: image/uploadResultNotification 保持 `_send_notification`（插件中为 @JsonNotification）
    - 7.5: 修复 `_send_notification` 注释，移除自相矛盾描述
