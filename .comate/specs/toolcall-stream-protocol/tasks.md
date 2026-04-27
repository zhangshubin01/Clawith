# toolCall 流式 Markdown 协议对齐 — 任务计划（三次评审修正版）

> 对齐 doc.md "三、修正后的核心问题清单" 中的 10 个问题

- [x] Task 1: 移除 `add_tasks` + 修复 `get_problems` 参数名（doc.md 问题 #3、#7、#8）
    - 1.1: 从 `tool_hooks.py` 的 `_LSP4J_IDE_TOOL_NAMES` frozenset 中移除 `"add_tasks"`（`ToolInvokeProcessor.java:146-149` 无 handler，返回 `"tool not support yet"`）
    - 1.2: 从 `_LSP4J_IDE_TOOLS` 列表中移除 `add_tasks` 的完整工具定义（约40行，第213-251行）
    - 1.3: 修复 `get_problems` 参数名：`filePath`（单数字符串）→ `filePaths`（复数数组），对齐 `GetProblemsToolHandler.getRequestFilePaths()` 源码（`GetProblemsToolHandler.java:255-261`，检查 `containsKey("filePaths")`，发送 `filePath` 时静默返回空结果）
    - 1.4: 在 `_lsp4j_aware_execute_tool` 中，若 `tool_name` 为 `add_tasks` 且 LSP4J 活跃，记录 WARNING 日志："add_tasks 为纯 UI 工具，插件无 handler，已移除注册"

- [x] Task 2: 重构 toolCallId 管理（doc.md 问题 #2）
    - 2.1: 在 `JsonRpcRouter.__init__` 中，将 `self._current_tool_call_id: str | None = None` 替换为 `self._tool_call_id_queue: list[tuple[str, str]] = []`，删除旧字段
    - 2.2: 在 `_handle_chat_ask` 开头（`self._cancel_event = asyncio.Event()` 附近），添加 `self._tool_call_id_queue = []` 重置队列
    - 2.3: 修改 `on_tool_call(status="running")`：`tool_call_id = str(uuid.uuid4())` 后执行 `self._tool_call_id_queue.append((tool_name, tool_call_id))`，替代旧的单字段赋值
    - 2.4: 修改 `invoke_tool_on_ide`：替换 `if getattr(self, "_current_tool_call_id", None)` 逻辑为队列消费：遍历 `_tool_call_id_queue`，按序找到第一个 `stored_name == tool_name` 的条目，pop 并复用其 toolCallId；未匹配则 `str(uuid.uuid4())` 兜底
    - 2.5: `on_tool_call(status="done")` 中不再清理队列（toolCallId 已被 `invoke_tool_on_ide` 消费），删除旧代码 `self._current_tool_call_id = None`

- [x] Task 3: 移除 FINISHED markdown 块（doc.md 问题 #1）
    - 3.1: 删除 `on_tool_call(status="done")` 中 4 行 FINISHED markdown 发送代码（`markdown_block = f"```toolCall::...::FINISHED..."` 到 `await self._send_chat_answer(...)`）
    - 3.2: 保留 `on_tool_call(status="done")` 中的 `_send_process_step_callback`、`_send_chat_think`、持久化逻辑不变
    - 3.3: 在 `invoke_tool_on_ide` 的 FINISHED sync 发送处添加中文注释："★ 状态变更由 tool/call/sync 事件通道处理，不再发送 FINISHED markdown 块（避免创建重复卡片）"

- [x] Task 4: 在 INIT markdown 块后发送 PENDING sync（doc.md 问题 #4）
    - 4.1: 在 `on_tool_call(status="running")` 中，INIT markdown 块 `_send_chat_answer(...)` 之后，添加 `await self._send_tool_call_sync(session_id, request_id, tool_call_id, "PENDING", tool_name=tool_name, parameters=data.get("args", {}))`，使卡片立即获得参数（scopeLabel 依赖 parameters）
    - 4.2: 在 `invoke_tool_on_ide` 中移除第一个 `_send_tool_call_sync("PENDING", ...)` 调用（原第984-987行），避免重复发送 PENDING
    - 4.3: 保留 `invoke_tool_on_ide` 中的 RUNNING sync 不变

- [x] Task 5: FINISHED/ERROR sync 携带 parameters（doc.md 问题 #5）
    - 5.1: 修改 `invoke_tool_on_ide` 的 FINISHED sync：`await self._send_tool_call_sync(self._session_id, request_id, tool_call_id, "FINISHED", tool_name=tool_name, parameters=arguments, results=result[:500] if result else None)`
    - 5.2: 修改超时 ERROR sync：添加 `parameters=arguments` 参数
    - 5.3: 验证 `_send_tool_call_sync` 签名中 `parameters` 参数默认为 `None`，传 `arguments` 字典后会被放入 `ToolCallSyncResult.parameters` 字段

- [x] Task 6: 添加工具名映射（doc.md 问题 #6）
    - 6.1: 在 `tool_hooks.py` 中定义 `_TOOL_NAME_MAP = {"edit_file": "replace_text_by_path", "create_file": "create_file_with_text", "delete_file": "delete_file_by_path"}`
    - 6.2: 在 `_lsp4j_aware_execute_tool` 中，LSP4J 活跃时先检查 `_TOOL_NAME_MAP`：若 `tool_name` 在映射中，替换为映射值并记录 INFO 日志 `[LSP4J-TOOL] 工具名映射: edit_file → replace_text_by_path`
    - 6.3: 在 `_lsp4j_aware_get_tools` 中，过滤掉基础工具中与 IDE 工具重名/重叠的：`edit_file`、`write_file`、`delete_file`、`search_files`、`find_files`，避免 LLM 混淆（只保留 IDE 版本）

- [x] Task 7: 可选增强 — think markdown 块（doc.md 问题 #9）
    - 7.1: 在 `on_thinking` 回调中，`_send_chat_think()` 之后，追加发送 think markdown 块到 `chat/answer` 流，格式为 4 反引号：`f"````think::{think_time_ms}\n{text}\n````"`（基于 `MarkdownStreamPanel.java:341-344` 源码验证，`think_time_ms` 为毫秒时间值）
    - 7.2: 若无法计算实际思考时间，使用固定值 `0` 或 `{THINK_TIME}`（插件对 `{THINK_TIME}` 有特殊处理，不显示时间）

- [x] Task 8: 部署验证
    - 8.1: 重启后端服务
    - 8.2: 单工具验证：发"读一下 build.gradle"，确认只显示一个"查看文件"卡片、显示文件路径、状态 INIT→PENDING→RUNNING→FINISHED
    - 8.3: 多工具验证：发"读 A 文件再列 B 目录"，确认各卡片独立不冲突
    - 8.4: 映射验证：发"修改 xxx 文件"，确认 `edit_file` 自动映射到 `replace_text_by_path`
    - 8.5: 参数验证：发"检查 xxx 文件的代码问题"，确认 `get_problems` 使用 `filePaths`（复数）参数正常返回
    - 8.6: 日志验证：后端无 "tool not support yet"、无异常

- [x] Task 9: 代码提交
    - 9.1: 确认修改范围：`jsonrpc_router.py`（toolCallId 队列、移除 FINISHED 块、PENDING sync）+ `tool_hooks.py`（移除 add_tasks、修复 get_problems、工具名映射）
    - 9.2: 提交信息：`fix: toolCall 流式 markdown 协议对齐 — 移除重复 FINISHED 块、toolCallId 队列化、修复 get_problems 参数名、添加工具名映射`
    - 9.3: 提交并推送
