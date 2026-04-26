# LSP4J 集成综合修复 — 执行摘要

## 执行概况

- **特征名称**: `lsp4j-method-adaptation`
- **执行日期**: 2026-04-26
- **总任务数**: 23 个（含 Task 6a 和 Task 8a/8b）
- **完成状态**: 全部完成

## 修改文件清单

### 1. `backend/app/plugins/clawith_lsp4j/jsonrpc_router.py`（主要修改）

**P0 修复：**
- Task 1: `invoke_tool_on_ide()` 中 `request_id` 改为 `self._current_request_id or str(uuid.uuid4())`，确保工具调用使用当前 chat/ask 的 requestId
- Task 2: 新增 `_send_tool_call_sync()` 方法 + 在 `invoke_tool_on_ide` 中嵌入 PENDING→RUNNING→FINISHED/ERROR sync 通知 + 在 `_handle_chat_ask` 末尾发送 REQUEST_FINISHED 清理通知
- Task 3: 新增 `self._closed` 标记 + `_send_message()` 中前置检查 + `_handle_exit`/`_handle_shutdown`/`cleanup()` 中设置 + `_handle_chat_ask` 入口拦截

**P1 修复：**
- Task 4: 新增 `_handle_image_upload()` 双响应模式（校验→success 响应→异步 uploadResultNotification）+ `_cleanup_expired_images()` + `_image_cache` + 注册到 `_METHOD_MAP`
- Task 7: 重写 `_build_lsp4j_ide_prompt()` — 使用正确字段名（`activeFilePath`/`sourceCode`/`extra.context[].selectedItem.extra.contextType`）+ try/except 保护 + 工具可用性提示 + 项目路径注入
- Task 8: `thinking_chunks` 局部变量 + `on_thinking` append + `on_tool_call(status=="done")` 持久化 `ChatMessage(role="tool_call")`（JSON 字段与 Web 通道一致：`name`/`args`/`status`/`result[:500]`/`reasoning_content`）+ `_persist_lsp4j_chat_turn` 增加 `thinking_text` 参数 + 构造时传入 `thinking=thinking_text`

**P2 修复：**
- Task 10: 新增 `_handle_config_get_endpoint()` + `_handle_config_update_endpoint()`
- Task 11: 新增 `_handle_commit_msg_generate()` — `role_description="Git commit message generator"` + `list[dict]` 格式 + `_send_response` 先于 `_send_notification`
- Task 12: 新增 `_send_session_title_update()` + `_handle_chat_ask` 中自动生成标题
- Task 13: 新增 `_cancelled_requests` 集合 + `invoke_tool_on_ide` 超时记录 + `_handle_response` 检查迟达响应
- Task 14: `_resolve_model_by_key` 中 `key="auto"` 直接返回 None
- Task 18: 新增 `_handle_tool_call_results()` — MVP 返回空列表 `ListToolCallInfoResponse`

**P3 修复：**
- Task 15: `_METHOD_MAP` 扩展约 55 个方法（LanguageServer 24 + @JsonDelegate 服务 19 + ChatService 9 + 其他）
- Task 19: 更新 `__init__.py` docstring 为准确描述
- Task 20: 所有新增方法含中文 docstring 和日志

### 2. `backend/app/plugins/clawith_lsp4j/tool_hooks.py`

- Task 8b: `_LSP4J_IDE_TOOL_NAMES` 添加 `"add_tasks"` + `_LSP4J_IDE_TOOLS` 添加 `add_tasks` 工具定义

### 3. `backend/app/services/llm/caller.py`

- Task 5: `is_retryable_error()` 重写 — 先检查错误前缀 + HTTP 状态码增加上下文关键词和边界符匹配 + 限流关键词独立匹配
- Task 5: failover 检查块增加正常回复前置判断（不打 WARNING）

## 关键设计决策

1. **`on_tool_call(data: dict)` 回调签名** — 与 `call_llm` 实际定义一致，从 `data` 字典提取字段
2. **JSON 字段名与 Web 通道一致** — `name`/`args`/`status`/`result[:500]`/`reasoning_content`
3. **thinking 传入构造函数** — 不事后更新 detached 对象，避免 SQLAlchemy session 问题
4. **图片上传校验先于响应** — 避免返回 success 后再报告错误
5. **commitMsg/generate `_send_response` 先于 `_send_notification`** — 确保客户端先收到请求确认
6. **`tool/call/sync` 是通知而非方法** — 不注册到 `_METHOD_MAP`，通过 `_send_notification` 推送

## 验证结果

- 4 个 Python 文件语法检查全部通过
- graphify 知识图谱已更新（4003 nodes, 15493 edges, 159 communities）
- 无插件代码修改（符合标准 #22）
