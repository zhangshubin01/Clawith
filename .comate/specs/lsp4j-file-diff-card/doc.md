# 修复 LSP4J 文件工具缺失 Diff 卡片

## 需求场景

使用 LSP4J 协议时，文件工具调用（`edit_file`、`create_file`、`delete_file`）只显示 "FINISHED" 状态文字，不显示文件/diff 卡片（带文件链接和接受/拒绝按钮）。ACP 协议可正常显示。

## 根因

后端发送的 `toolCall::` markdown 块和 `tool/call/sync` 通知使用了插件原生名称（`replace_text_by_path`、`create_file_with_text`、`delete_file_by_path`），但插件的 `ToolPanel` 通过 `ToolTypeEnum.getByToolName()` 识别文件工具时只认 LLM 侧名称（`edit_file`、`create_file`、`delete_file`、`search_replace`）。

由于 `ToolCallSyncResult` 没有 `toolName` 字段，sync 通知无法覆盖名称——markdown 块是唯一决定 ToolPanel 工具名的因素。`getByToolName("replace_text_by_path")` → `UNKNOWN` → 永远不会进入文件工具分支，因此不会创建 `AIDevFilePanel`。

此外，FINISHED sync 的 results 中缺少 `fileId` 字段（`AIDevFilePanel` 需要 `results[0]["fileId"]` 来渲染文件链接），且参数中 `file_path` 被转为 `filePath`（ToolPanel 用 `file_path` 取值），双重问题导致文件卡片永远不出现。

## 架构与技术方案

### 名称双轨制

在所有 UI 可见通道（markdown 块、sync 通知）使用 **LLM 侧名称**，仅在 `tool/invoke` 请求中保留 **插件原生名称**。

```
LLM 调用 "edit_file"
  → tool_hooks 映射: edit_file → replace_text_by_path（供 tool/invoke 使用）
  → on_tool_call 映射: edit_file → replace_text_by_path（供队列匹配使用）

各通道使用的名称：
  - markdown 块: "edit_file"        ← LLM 侧名称（ToolPanel 需要）
  - tool/call/sync: "edit_file"     ← LLM 侧名称（与 ToolPanel 存储的名称匹配）
  - tool/invoke: "replace_text_by_path" ← 插件原生名称（ToolInvokeProcessor 需要）
  - 队列匹配: "replace_text_by_path" ← 插件原生名称（invoke_tool_on_ide 按此匹配）
```

### 参数命名统一策略

| 通道 | 参数 key 风格 | 原因 |
|------|-------------|------|
| markdown 块 | — | 不涉及参数 |
| tool/call/sync parameters | snake_case (`file_path`) | ToolPanel 用 `file_path` 取文件路径 |
| tool/call/sync results | camelCase (`fileId`) | ToolPanel 用 `fileId` 取文件 ID |
| tool/invoke | camelCase (`filePath`) | ToolHandler 用 `getRequestFilePath()` 取 |
| 队列暂存 (`_tool_params`) | snake_case | 供 sync 通知使用 |

## 受影响文件

### `backend/app/plugins/clawith_lsp4j/tool_hooks.py`
- 类型：修改常量集合
- 位置：`_LSP4J_OVERLAP_BASE_TOOL_NAMES`（line 80-83）
- 变更：添加 `"create_file"` 避免与 `create_file_with_text` 参数风格冲突

### `backend/app/plugins/clawith_lsp4j/jsonrpc_router.py`
- 类型：修改多处逻辑
- 位置及变更：

#### 1. `_tool_call_id_queue` 类型标注（line 366）
当前标注已是 3-tuple，但实际 append 仍为 2-tuple，需统一。

#### 2. `on_tool_call` running 分支（lines 697-788）
- **队列存储改为 3 元组**（line 712）：`(original_name, tool_name, tool_call_id)` 替代 `(tool_name, tool_call_id)`
- **存储原始 snake_case 参数**（line 714-715）：保留 `file_path` 不转换
- **移除参数 camelCase 转换**（lines 749-761）：sync 通知保留 snake_case，转换在 `invoke_tool_on_ide` 集中处理
- **Markdown 块使用 `original_name`**（line 731）：让 ToolPanel 识别为 `edit_file` 而非 `replace_text_by_path`
- **PENDING sync 使用 `original_name`**（line 773-775）：与 markdown 块名称一致

#### 3. `on_tool_call` done 分支（lines 789-826）
- **IDE 工具跳过 FINISHED sync**：`invoke_tool_on_ide` 已发送带结果的 FINISHED，此处重复发送会覆盖结果
- **队列匹配适配 3 元组**（line 806）：作为防御代码保留

#### 4. `invoke_tool_on_ide`（lines 1299-1405）
- **队列匹配适配 3 元组**（line 1325）：提取 `original_name`
- **补充 `file_path → filePath` 转换**（line 1342-1347）：覆盖 `replace_text_by_path`、`create_file_with_text`、`delete_file_by_path`
- **RUNNING sync 使用 `original_name`**（line 1350-1353）：与 markdown 块一致
- **FINISHED sync 使用 `original_name` + 注入 `fileId`**（lines 1385-1389）：
  - `results` 从纯文本改为 `[{"fileId": "<file_path>", "message": "<result_text>"}]`
  - ToolPanel 在 FINISHED 到达时用 `results[0]["fileId"]` 创建 `AIDevFilePanel`
  - `parameters` 保留 snake_case 供 ToolPanel 读取 `file_path`

## 边界条件与异常处理

1. **LLM 直接调用插件原生名称**（如 `create_file_with_text` 而非 `create_file`）：
   - `original_name` = `tool_name` = `create_file_with_text`
   - `ToolTypeEnum.getByToolName("create_file_with_text")` → `UNKNOWN`
   - 这种情况下文件卡片不出现，但这是预期行为——LLM 应通过映射名称调用

2. **队列匹配失败**（`invoke_tool_on_ide` 先消费，`on_tool_call` done 再匹配）：
   - 跳过 IDE 工具的 done FINISHED 后，此问题不再发生
   - 防御代码保留 3 元组匹配逻辑

3. **`file_path` 为空**：
   - FINISHED sync 不注入 `fileId`，退化为普通文本结果
   - ToolPanel 不会创建 `AIDevFilePanel`，显示普通工具卡片

4. **`search_replace` 不需要名称映射**：
   - 插件原生名称就是 `search_replace`，`ToolTypeEnum` 也认这个名字
   - `original_name` = `tool_name` = `search_replace`，逻辑一致

## 数据流路径

```
LLM 调用 "edit_file" (args: {file_path: "/a/b.py", ...})
  ↓
on_tool_call(running):
  original_name = "edit_file"
  tool_name = "replace_text_by_path"  (映射后)
  tool_call_id = uuid4()
  queue.append(("edit_file", "replace_text_by_path", uuid))
  params = {file_path: "/a/b.py", ...}  (snake_case 原始参数)
  → chat/answer: ```toolCall::edit_file::uuid::INIT\n```
  → tool/call/sync: PENDING, toolName="edit_file", parameters={file_path: ...}
  ↓
invoke_tool_on_ide("replace_text_by_path", arguments):
  从队列匹配 → original_name="edit_file", tool_call_id=uuid
  params = {filePath: "/a/b.py", ...}  (转换后)
  → tool/call/sync: RUNNING, toolName="edit_file"
  → tool/invoke: name="replace_text_by_path", parameters={filePath: ...}
  ← tool/invokeResult: result
  → tool/call/sync: FINISHED, toolName="edit_file",
      parameters={file_path: "/a/b.py", ...},
      results=[{"fileId": "/a/b.py", "message": "result text"}]
  ↓
插件 ToolPanel:
  ToolTypeEnum.getByToolName("edit_file") → EDIT_FILE ✓
  parameters["file_path"] = "/a/b.py" ✓
  results[0]["fileId"] = "/a/b.py" ✓
  → 创建 AIDevFilePanel ✓
```

## 预期结果

1. `edit_file` 调用后显示 diff 卡片（带文件链接和接受/拒绝按钮）
2. `create_file` 调用后显示文件卡片
3. `delete_file` 调用后显示文件卡片
4. `search_replace` 不受影响（名称一致）
5. 每个工具只有 1 次 FINISHED sync，无重复
6. `create_file` 不再同时出现在 base tools 和 IDE tools 中
