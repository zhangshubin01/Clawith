# ide_write_file 写前 Diff 审批流程

**日期**: 2026-04-08  
**分支**: feature/user-api-key  
**状态**: 已审批，待实现

## 背景

当前 `ide_write_file` 工具调用流程存在问题：后端发送的 `permission_request` 消息只包含截断的 `args_summary`，前端未实现 `permission_request` 消息处理，导致弹窗从未出现，后端等待 2 分钟超时后自动拒绝，用户无法审批文件写入操作。

## 目标

实现完整的写前审批流程：**先展示 diff → 用户在 diff 上同意 → 才真正写文件**。

## 数据流

### 新流程

```
LLM → ide_write_file 
  → 后端读旧文件内容（ide_read_file via IDE bridge）
  → 发 permission_request（含 old_content + new_content）
  → 前端弹 Diff 弹窗
  → 用户点同意/拒绝
  → 前端发 permission_result
  → 后端写文件 or 跳过，返回结果给 LLM
```

### permission_request 消息结构（ide_write_file 专用）

```json
{
  "schemaVersion": 3,
  "type": "permission_request",
  "permission_id": "<uuid>",
  "tool_name": "ide_write_file",
  "file_path": "/path/to/file.kt",
  "old_content": "原文件内容（文件不存在时为空字符串）",
  "new_content": "LLM 要写入的完整内容"
}
```

其他工具（`ide_execute_command`、`ide_kill_terminal` 等）维持原有 `args_summary` 字段，使用简洁确认框。

### permission_result 消息结构（前端发回）

```json
{
  "schemaVersion": 3,
  "type": "permission_result",
  "permission_id": "<uuid>",
  "granted": true
}
```

## 后端改动（router.py）

**文件**: `backend/app/plugins/clawith_acp/router.py`

在 `_custom_execute_tool` 函数中，对 `ide_write_file` 工具在发送 `permission_request` 前插入读旧文件逻辑：

1. 新增 `_read_file_for_diff(ws, pending_perm, file_path, session_id)` 异步函数：
   - 复用现有 IDE bridge 通道，发送 `ide_read_file` 请求
   - 文件不存在时返回 `""`（空字符串，表现为纯新增 diff）
   - 读取失败时同样返回 `""`，不阻断流程

2. 在 `_acp_await_client_permission` 调用前，对 `ide_write_file` 额外传入 `old_content` 和 `new_content`：
   - `new_content` = `args.get("content", "")`
   - `old_content` = 上一步读取的结果

3. `_acp_await_client_permission` 新增可选参数 `extra_payload: dict = {}`，合并到发出的 `permission_request` 消息中。

**改动范围**: ~30 行

## 前端改动

### AgentDetail.tsx

1. 新增 state：
   ```typescript
   const [pendingPermission, setPendingPermission] = useState<PendingPermission | null>(null);
   ```

2. `ws.onmessage` 新增分支（在现有类型判断之前）：
   ```typescript
   if (d.type === 'permission_request') {
       setPendingPermission({
           permissionId: d.permission_id,
           toolName: d.tool_name,
           filePath: d.file_path,
           oldContent: d.old_content ?? '',
           newContent: d.new_content ?? '',
           argsSummary: d.args_summary ?? '',
       });
       return;
   }
   ```

3. 新增 `handlePermissionResult(granted: boolean)` 函数，通过当前 session 的 WebSocket 发回 `permission_result`，然后清空 `pendingPermission`。

4. 在 JSX 中渲染 `<PermissionModal>`。

### 新建 PermissionModal.tsx

**文件**: `frontend/src/components/PermissionModal.tsx`

- Props: `permission: PendingPermission | null`, `onResult: (granted: boolean) => void`
- `ide_write_file` 时：展示文件路径 + old/new 内容对比（使用 `react-diff-viewer-continued` 或简单文本对比），底部同意/拒绝按钮
- 其他工具时：展示工具名 + `argsSummary`，底部同意/拒绝按钮
- 弹窗关闭/拒绝均发送 `granted: false`

**改动范围**: `AgentDetail.tsx` ~30 行 + `PermissionModal.tsx` ~80 行新文件

## 类型定义

```typescript
interface PendingPermission {
    permissionId: string;
    toolName: string;
    filePath?: string;
    oldContent?: string;
    newContent?: string;
    argsSummary?: string;
}
```

## 不在本次范围内

- 其他工具（`ide_execute_command` 等）的 diff 显示
- 权限记忆（"本次会话始终允许"）
- 后端超时时长调整
