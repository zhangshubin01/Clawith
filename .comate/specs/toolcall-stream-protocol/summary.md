# toolCall 流式 Markdown 协议对齐 — 总结

> 提交：`99d22b7` — `fix: toolCall 流式 markdown 协议对齐`
> 分支：`feature/user-api-key`
> 修改文件：`jsonrpc_router.py`（+67/-19）、`tool_hooks.py`（+54/-35）

---

## 已修复的 10 个核心问题

| # | 问题 | 严重性 | 修复方式 |
|---|------|--------|----------|
| 1 | 发送了 FINISHED markdown 块，导致重复卡片 | 高 | 移除 FINISHED markdown 块，状态变更仅通过 tool/call/sync 事件通道 |
| 2 | `_current_tool_call_id` 单字段无法处理多工具 | 高 | 重构为 `_tool_call_id_queue: list[tuple[str, str]]`，按序匹配消费 |
| 3 | `add_tasks` 在 `_LSP4J_IDE_TOOL_NAMES` 中但插件无 handler | 高 | 从注册列表移除，添加拦截日志和提前返回 |
| 4 | `on_tool_call` 未发送 PENDING sync，卡片参数缺失 | 中 | INIT markdown 块后立即发送 PENDING sync（含 parameters） |
| 5 | FINISHED/ERROR sync 未携带 parameters | 中 | 添加 `parameters=arguments` 参数 |
| 6 | 基础工具名 `edit_file` 等可能走 ACP 路径 | 中 | 添加 `_TOOL_NAME_MAP` 映射 + 基础工具过滤 |
| 7 | `get_problems` 参数名错误：`filePath` 应为 `filePaths` | 高 | 修复参数名为 `filePaths`（复数数组），对齐 `GetProblemsToolHandler` |
| 8 | `add_tasks` 进入队列但无 handler，导致队列错位 | 高 | 已移除 `add_tasks`，不再进入队列 |
| 9 | think markdown 块未发送 | 低 | 在 `on_thinking` 中追加 4 反引号格式 think 块 |
| 10 | 工具名映射不一致导致队列匹配失败 | 高(审查发现) | `on_tool_call` 入队前也应用 `_TOOL_NAME_MAP`，确保名称一致 |

---

## 关键修改点

### jsonrpc_router.py

1. **模块级 `_TOOL_NAME_MAP`**：与 `tool_hooks.py` 保持同步的工具名映射
2. **`__init__`**：`_current_tool_call_id` → `_tool_call_id_queue`
3. **`_handle_chat_ask`**：添加 `_tool_call_id_queue = []` 重置
4. **`on_tool_call("running")`**：应用工具名映射 → 入队 → INIT markdown → PENDING sync
5. **`on_tool_call("done")`**：应用工具名映射 → 移除 FINISHED markdown 块
6. **`on_thinking`**：追加 think markdown 块（4 反引号格式，`{THINK_TIME}` 占位符）
7. **`invoke_tool_on_ide`**：队列按序匹配消费（支持映射名）、移除重复 PENDING、FINISHED/ERROR sync 携带 parameters

### tool_hooks.py

1. **`_LSP4J_IDE_TOOL_NAMES`**：移除 `add_tasks`，修复 `get_problems` 注释
2. **`_LSP4J_IDE_TOOLS`**：移除 `add_tasks` 定义，修复 `get_problems` 参数名
3. **`_TOOL_NAME_MAP`**：新增映射（edit_file→replace_text_by_path, create_file→create_file_with_text, write_file→create_file_with_text, delete_file→delete_file_by_path）
4. **`_LSP4J_OVERLAP_BASE_TOOL_NAMES`**：新增过滤集合
5. **`_lsp4j_aware_execute_tool`**：添加 `add_tasks` 拦截 + 工具名映射逻辑
6. **`_lsp4j_aware_get_tools`**：过滤重名基础工具

---

## 数据流路径（修正后）

```
LLM 开始调用工具
  ↓
on_tool_call(status="running", name="edit_file")
  ├─ 应用映射: edit_file → replace_text_by_path
  ├─ toolCallId 入队: [("replace_text_by_path", uuid1)]
  ├─ chat/answer: ```toolCall::replace_text_by_path::uuid1::INIT``` → 创建卡片
  ├─ tool/call/sync: PENDING + parameters(file_path)                  → 卡片显示"查看文件 /a/b.java"
  ├─ chat/process_step_callback: doing
  └─ chat/think: "正在调用工具: replace_text_by_path"
  ↓
_lsp4j_aware_execute_tool("edit_file", {file_path: "/a/b.java"})
  ├─ 映射: edit_file → replace_text_by_path
  └─ invoke_lsp4j_tool("replace_text_by_path", ...)
      ↓
invoke_tool_on_ide("replace_text_by_path", {file_path: "/a/b.java"})
  ├─ 队列匹配: pop ("replace_text_by_path", uuid1) ✓
  ├─ tool/call/sync: RUNNING + parameters
  ├─ tool/invoke → IDE 执行
  ├─ tool/invokeResult ← IDE 返回内容
  └─ tool/call/sync: FINISHED + parameters + results
  ↓
on_tool_call(status="done", name="edit_file")
  ├─ 应用映射: edit_file → replace_text_by_path
  ├─ chat/process_step_callback: done
  ├─ chat/think: "工具 replace_text_by_path 执行完成"
  └─ 持久化 tool_call 消息
  ❌ 不再发送 FINISHED markdown 块
```

---

## 审查中发现并修复的额外问题

Code review 发现工具名映射导致队列匹配失败（`on_tool_call` 入队原始名 `edit_file`，但 `invoke_tool_on_ide` 收到映射名 `replace_text_by_path`），通过在 `on_tool_call` 入队前也应用映射修复。

同时补充了 `write_file` → `create_file_with_text` 映射（基础工具注册名是 `write_file`，不是 `create_file`）。
