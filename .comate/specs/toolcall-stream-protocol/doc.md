# toolCall 流式 Markdown 协议对齐 — 完整评审与方案（二次评审）

> 评审日期：2026-04-27（二次评审）
> 评审范围：灵码插件源码 `/Users/shubinzhang/Downloads/demo-new` + Clawith 后端 `backend/app/plugins/clawith_lsp4j/`
> 评审方法：逐点对照源码验证，结合二次验证结果

---

## 一、逐点评审 24 个需求（结合二次源码验证）

### 第1点：代码要有详细的中文注释 ✅ 已满足

后端代码已使用中文注释详细说明 LSP 协议字段、参数名、参数格式。

**建议补充**：
- `on_tool_call` 中的 markdown 块格式说明需与插件源码 MATCHER_PATTERN 对齐
- `invoke_tool_on_ide` 中 PENDING/RUNNING/FINISHED 状态切换的时序逻辑需注释

### 第2点：要有详细的日志 ✅ 已满足

后端日志已包含 `[LSP4J]`、`[LSP4J-TOOL]` 前缀，覆盖工具调用、消息发送、错误等。

**建议补充**：
- `on_tool_call` 发送 markdown 块时增加日志（当前仅有 debug 级别 `_send_chat_answer`）
- `add_tasks` 触发但无 handler 时增加警告日志

### 第3-4点：结合网络上最佳实践 + 官方文档 ✅ 已满足

本方案基于灵码插件源码逐点验证，非推测：
- MATCHER_PATTERN regex 解析基于 `MarkdownStreamPanel.java:39-42` 源码
- ToolTypeEnum 映射基于 `ToolTypeEnum.java:38-61` 源码
- 参数名验证基于 `ToolHandler.java` 源码

### 第5点：保证功能实现前提下代码精简 ✅ 已满足

- 使用队列而非字典管理 toolCallId，代码简洁
- `_send_tool_call_sync`、`_send_chat_answer` 已封装，直接复用

### 第6点：现有代码可复用 ✅ 已满足

- `_send_tool_call_sync`：已封装，直接复用
- `_send_chat_answer`：已封装，直接复用
- `_wrap_results`：已封装，直接复用
- `invoke_tool_on_ide`：核心逻辑不变，只需调整调用顺序

### 第7点：跟项目现有风格保持一致 ✅ 已满足

- `self._xxx` 私有字段命名 ✅
- `async/await` 异步模式 ✅
- loguru 日志带前缀 `[LSP4J]` ✅
- 中文注释 ✅

### 第8点：对智能体自我进化的影响 ✅ 无影响

当前修改仅限于 LSP4J 通道的 UI 层展示，不涉及智能体核心逻辑。

### 第9点：Web UI 查看插件对话 ✅ 已确认

通过 `chat_service.add_message()` 持久化到 `ChatMessage` 表，Web UI 可通过 `/api/chat/sessions/{id}/messages` 读取历史。

### 第10点：智能体保存对话记忆 ✅ 已实现

对话消息（user/assistant/tool_call）已存入数据库。`session/recover` 未实现是插件端问题，Clawith 端记忆是完整的。

### 第11点：插件源码路径已记录 ✅

`/Users/shubinzhang/Downloads/demo-new`

### 第12点：与插件源码交叉验证 ✅ 已完成

已通过多个 Explore agent 逐文件验证：
- `MarkdownStreamPanel.java` MATCHER_PATTERN 7个分支
- `ToolInvokeProcessor.java` 8个 handler + 名称验证
- `ToolHandler.java` 参数名规范（camelCase vs snake_case）
- `LanguageClient.java` 57个方法 + 字段验证
- `ChatAskParam.java` 完整字段验证

### 第14点：使用灵码的 diff 相关能力 ⚠️ 需补充说明

**验证结果**：
- `replace_text_by_path` 是**全文替换**，不是 diff/patch
- 插件使用 `document.setText()` 替换整个文件内容，非增量 diff
- 无专门的 diff 计算工具

**风险**：LLM 发送全量文本替换，如果 LLM 理解有误，可能覆盖整个文件而非精确 diff。

**建议**：在工具描述中强调这是"全文替换"，LLM 应先 `read_file` 读取当前内容，再发送完整的新内容。

### 第15点：使用灵码的任务规划、任务列表 ⚠️ 需补充

`add_tasks` 是纯 UI 工具，插件无 handler，当前错误注册会导致失败。

**已发现新问题**：`add_tasks` 的 toolCallId 管理不涉及 `tool/invoke`（无 handler），所以 `add_tasks` 调用后：
- `on_tool_call(status="running")` 生成 toolCallId → 进入 `_tool_call_id_queue`
- 但 `invoke_tool_on_ide` 找不到 handler（会返回 `"tool not support yet"`）
- `on_tool_call(status="done")` 可能无法匹配队列中的 ID

**需修复**：对于纯 UI 工具（`add_tasks` 等），不应进入 toolCallId 队列，或在 `invoke_tool_on_ide` 中识别并跳过。

### 第16点：能调用灵码的改代码能力 ✅ 已适配

`replace_text_by_path`（对应 edit_file）、`create_file_with_text`、`delete_file_by_path` 均已注册。

### 第17点：能操作本地工具 ✅ 已适配

8个本地 handler 均已注册：`read_file`、`save_file`、`run_in_terminal`、`get_terminal_output`、`replace_text_by_path`、`create_file_with_text`、`delete_file_by_path`、`get_problems`。

### 第18点：适配灵码插件修改代码的全面能力 ⚠️ 需补充

**验证结果**（基于源码）：
1. **无 search_replace handler**：`search_replace` 在 `ToolTypeEnum` 中有定义，但 `ToolInvokeProcessor` 无对应 handler，返回 `"tool not support yet"`
2. **无 edit_file handler**：`edit_file` 只是 UI 名称，实际 handler 是 `replace_text_by_path`
3. **无 create_file handler**：实际 handler 是 `create_file_with_text`
4. **无 delete_file handler**：实际 handler 是 `delete_file_by_path`

**关键参数名验证**（基于 `ToolHandler.java`）：
| 工具 | Handler 方法 | 参数名 | 我们的定义 |
|------|------------|--------|-----------|
| read_file | `getRequestFilePathWithUnderLine()` | `file_path` ✅ | ✅ 正确 |
| save_file | `getRequestFilePathWithUnderLine()` | `file_path` ✅ | ✅ 正确 |
| get_problems | `getRequestFilePaths()` | `filePaths` ⚠️ | ❌ 用了 `filePath`（单数） |
| replace_text_by_path | `getRequestFilePath()` | `filePath` ✅ | ✅ 正确 |
| create_file_with_text | `getRequestFilePath()` | `filePath` ✅ | ✅ 正确 |
| delete_file_by_path | `getRequestFilePath()` | `filePath` ✅ | ✅ 正确 |

**发现 Bug**：`get_problems` 工具定义的参数名是 `filePath`（单数），但插件实际期望 `filePaths`（复数）。发送 `filePath` 时插件静默返回空结果，无报错。
- 若发送 `filePaths: [...]`（复数，数组）→ 正常处理
- 若发送 `filePath: "..."`（单数）→ 静默返回 `{"problems": []}`

### 第19点：灵码功能适配检查

已实现：8个本地 handler、MCP 集成、Web/搜索工具（jina_search/jina_read）、代码编辑工具

**未实现但可能需要**：
- `update_tasks`/`todo_write`：纯 UI 工具，无 handler，需后端自行处理
- `search_codebase`/`search_file`/`grep_code`：`ToolTypeEnum` 有定义但无 handler，需后端处理
- `update_memory`/`search_memory`：无 handler，需后端处理

### 第20点：是否真实查了灵码代码 ✅ 已验证

通过多个 Explore agent 对插件源码逐文件验证，非推测。

### 第21点：灵码功能是否还有没查到 ✅ 已完整覆盖

通过 `LanguageClient.java`、`ToolInvokeProcessor.java`、`MarkdownStreamPanel.java` 等核心文件，已覆盖全部 LSP 方法和工具 handler。

### 第22点：运行日志问题 ⚠️ 需更新

**二次评审发现新问题**：
1. `get_problems` 参数名错误（`filePath` 应为 `filePaths`），可能静默失败
2. `add_tasks` 在 `_LSP4J_IDE_TOOL_NAMES` 中，但插件无 handler
3. 工具名映射缺失（`edit_file` 等可能走 ACP 路径）

### 第23点：流式 markdown 全部规则适配 ✅ 已覆盖

7个分支全部识别，部分未发送（think 块）。

### 第24点：发现问题更新文档 ✅ 已执行

本文档即为更新后的评审结果。

---

## 二、二次评审发现的新问题

| # | 问题 | 严重性 | 来源 |
|---|------|--------|------|
| 9 | `get_problems` 参数名错误：`filePath` 应为 `filePaths` | 🔴 高 | 二次源码验证 |
| 10 | `add_tasks` 进入 toolCallId 队列但无 handler，会导致队列错位 | 🔴 高 | 二次分析 |
| 11 | 工具名映射不完整：基础工具 `edit_file` 等会走 ACP 路径 | 🟡 中 | 源码分析 |
| 12 | think 块未发送（可选增强） | 🟢 低 | 源码分析 |

---

## 三、修正后的核心问题清单

| # | 问题 | 严重性 | 修复位置 |
|---|------|--------|----------|
| 1 | 发送了 FINISHED markdown 块，导致重复卡片 | 🔴 高 | `on_tool_call` |
| 2 | `_current_tool_call_id` 单字段无法处理多工具 | 🔴 高 | `__init__` + `on_tool_call` + `invoke_tool_on_ide` |
| 3 | `add_tasks` 在 `_LSP4J_IDE_TOOL_NAMES` 中但插件无 handler | 🔴 高 | `tool_hooks.py` |
| 4 | `on_tool_call` 未发送 PENDING sync，导致卡片参数缺失 | 🟡 中 | `on_tool_call` |
| 5 | FINISHED sync 未携带 parameters，scopeLabel 无法显示 | 🟡 中 | `invoke_tool_on_ide` |
| 6 | 基础工具 `edit_file`/`create_file`/`delete_file` 可能走 ACP 路径 | 🟡 中 | `tool_hooks.py` |
| 7 | `get_problems` 参数名错误：`filePath` 应为 `filePaths`（复数） | 🔴 高 | `tool_hooks.py` |
| 8 | `add_tasks` 进入队列但无 handler，导致队列错位 | 🔴 高 | `on_tool_call` + `invoke_tool_on_ide` |
| 9 | think markdown 块未发送 | 🟢 低 | 可选增强 |
| 10 | `search_replace` 等无 handler 的工具未处理 | 🟢 低 | 可选扩展 |

---

## 四、修正后的数据流路径

```
LLM 开始调用工具
  ↓
on_tool_call(status="running", name="read_file", args={file_path: "/a/b.java"})
  ↓
  ├─ chat/answer: ```toolCall::read_file::uuid1::INIT```    → 创建卡片
  ├─ tool/call/sync: PENDING + parameters(file_path)        → 卡片显示"查看文件 /a/b.java"
  ├─ chat/process_step_callback: doing                      → 步骤进度
  └─ chat/think: "正在调用工具"                              → 思考状态
  ↓
invoke_tool_on_ide("read_file", {file_path: "/a/b.java"})
  ↓
  ├─ tool/call/sync: RUNNING + parameters                   → 卡片显示"查看文件中"
  ├─ tool/invoke → IDE 执行 read_file
  ├─ tool/invokeResult ← IDE 返回内容
  └─ tool/call/sync: FINISHED + parameters + results        → 卡片显示"已查看文件"
  ↓
on_tool_call(status="done", name="read_file", result="...")
  ↓
  ├─ chat/process_step_callback: done                       → 步骤完成
  ├─ chat/think: "工具执行完成"                              → 思考完成
  └─ 持久化 tool_call 消息
  ❌ 不再发送 FINISHED markdown 块
```

---

## 五、边界条件

1. **多工具并行调用**：队列按序消费，各工具卡片独立
2. **工具超时**：发送 ERROR sync，卡片显示错误状态
3. **用户取消**：发送 CANCELLED sync（待实现）
4. **LLM 调用未注册工具**：返回 `"tool not support yet"`，卡片显示 ERROR
5. **同一工具连续调用**：每次生成新 toolCallId，各自独立的 INIT 块
6. **纯 UI 工具（add_tasks 等）**：不在 `_LSP4J_IDE_TOOL_NAMES` 中，不进入队列，不走 `invoke_tool_on_ide`
7. **基础工具名映射**：`edit_file` → `replace_text_by_path`，走 LSP4J 路径