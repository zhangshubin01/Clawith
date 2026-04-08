# ide_write_file 写前 Diff 审批流程 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现 ide_write_file 写前 Diff 审批：后端在发权限请求前先读旧文件，将 old_content + new_content 一起发给前端，前端弹出 Diff 对比弹窗，用户同意后才真正写文件。

**Architecture:** 后端 `_acp_await_client_permission` 新增 `extra_payload` 参数，在调用前由 `_custom_execute_tool` 传入读取的旧文件内容；前端新增 `PermissionModal` 组件处理 `permission_request` 消息，`ide_write_file` 时渲染 diff，其他工具渲染简洁确认框；用户操作后通过原 WebSocket 发回 `permission_result`。

**Tech Stack:** Python asyncio, FastAPI WebSocket, React + TypeScript, inline CSS (项目现有风格，无 Tailwind)

---

## 文件清单

| 操作 | 文件 | 职责 |
|------|------|------|
| Modify | `backend/app/plugins/clawith_acp/router.py` | 读旧文件 + 扩展 permission_request 消息 |
| Create | `frontend/src/components/PermissionModal.tsx` | Diff 审批弹窗组件 |
| Modify | `frontend/src/pages/AgentDetail.tsx` | 处理 permission_request 消息，渲染弹窗 |

---

## Task 1：后端 — 扩展 `_acp_await_client_permission` 支持 `extra_payload`

**Files:**
- Modify: `backend/app/plugins/clawith_acp/router.py:306-354`

- [ ] **Step 1: 修改函数签名，新增 `extra_payload` 参数**

在 `router.py` 第 306 行，将函数签名从：
```python
async def _acp_await_client_permission(
    websocket: WebSocket,
    pending_permissions: dict[str, asyncio.Future],
    tool_name: str,
    args: dict[str, Any],
    session_id: str = "",
    timeout: float = 120.0,
) -> bool:
```
改为：
```python
async def _acp_await_client_permission(
    websocket: WebSocket,
    pending_permissions: dict[str, asyncio.Future],
    tool_name: str,
    args: dict[str, Any],
    session_id: str = "",
    timeout: float = 120.0,
    extra_payload: dict | None = None,
) -> bool:
```

- [ ] **Step 2: 将 `extra_payload` 合并到发出的消息中**

将第 321-330 行的 `send_json` 调用从：
```python
        summary = json.dumps(args, ensure_ascii=False)[:800]
        await websocket.send_json(
            _acp_ws_envelope(
                {
                    "type": "permission_request",
                    "permission_id": perm_id,
                    "tool_name": tool_name,
                    "args_summary": summary,
                }
            )
        )
```
改为：
```python
        summary = json.dumps(args, ensure_ascii=False)[:800]
        payload: dict[str, Any] = {
            "type": "permission_request",
            "permission_id": perm_id,
            "tool_name": tool_name,
            "args_summary": summary,
        }
        if extra_payload:
            payload.update(extra_payload)
        await websocket.send_json(_acp_ws_envelope(payload))
```

- [ ] **Step 3: 提交**

```bash
cd /Users/shubinzhang/Documents/UGit/Clawith
git add backend/app/plugins/clawith_acp/router.py
git commit -m "feat(acp): add extra_payload param to _acp_await_client_permission"
```

---

## Task 2：后端 — 新增 `_read_file_for_diff`，在权限请求前读旧文件

**Files:**
- Modify: `backend/app/plugins/clawith_acp/router.py:362-448`

- [ ] **Step 1: 在 `_custom_execute_tool` 之前新增 `_read_file_for_diff` 函数**

在 `router.py` 第 356 行（`_custom_get_tools` 之前）插入：

```python
async def _read_file_for_diff(
    ws: WebSocket,
    pending: dict[str, asyncio.Future],
    file_path: str,
    session_id: str = "",
) -> str:
    """通过 IDE bridge 读取文件现有内容，供 diff 展示用。文件不存在或读取失败时返回空字符串。"""
    call_id = str(uuid.uuid4())
    loop = asyncio.get_running_loop()
    future = loop.create_future()
    pending[call_id] = future
    try:
        await ws.send_json(
            _acp_ws_envelope(
                {
                    "type": "execute_tool",
                    "call_id": call_id,
                    "name": "ide_read_file",
                    "args": {"path": file_path},
                }
            )
        )
        result = await asyncio.wait_for(future, timeout=10.0)
        return str(result) if result else ""
    except Exception:
        return ""
    finally:
        pending.pop(call_id, None)
```

- [ ] **Step 2: 在 `_custom_execute_tool` 中，对 `ide_write_file` 先读旧文件再发权限请求**

在 `router.py` 第 384-395 行，将权限判断代码从：
```python
    if ws and tool_name in _IDE_BRIDGE_TOOL_NAMES:
        if tool_name in _IDE_TOOLS_REQUIRING_PERMISSION:
            allowed = await _acp_await_client_permission(
                ws, pending_perm, tool_name, args, session_id=session_id
            )
```
改为：
```python
    if ws and tool_name in _IDE_BRIDGE_TOOL_NAMES:
        if tool_name in _IDE_TOOLS_REQUIRING_PERMISSION:
            extra: dict[str, Any] = {}
            if tool_name == "ide_write_file":
                file_path = args.get("path", "")
                old_content = await _read_file_for_diff(ws, pending, file_path, session_id)
                extra = {
                    "file_path": file_path,
                    "old_content": old_content,
                    "new_content": args.get("content", ""),
                }
            allowed = await _acp_await_client_permission(
                ws, pending_perm, tool_name, args, session_id=session_id,
                extra_payload=extra if extra else None,
            )
```

注意：`pending` 变量在第 367 行已定义为 `current_acp_pending_tools.get()`，可直接使用。

- [ ] **Step 3: 验证后端语法正确**

```bash
cd /Users/shubinzhang/Documents/UGit/Clawith
python -c "import ast; ast.parse(open('backend/app/plugins/clawith_acp/router.py').read()); print('OK')"
```

预期输出：`OK`

- [ ] **Step 4: 提交**

```bash
git add backend/app/plugins/clawith_acp/router.py
git commit -m "feat(acp): read old file before ide_write_file permission request for diff"
```

---

## Task 3：前端 — 新建 `PermissionModal.tsx`

**Files:**
- Create: `frontend/src/components/PermissionModal.tsx`

- [ ] **Step 1: 创建组件文件**

新建 `frontend/src/components/PermissionModal.tsx`，内容如下：

```tsx
import { useEffect, useRef } from 'react';

export interface PendingPermission {
    permissionId: string;
    wsKey: string;          // 用于找回对应 WebSocket
    toolName: string;
    filePath?: string;
    oldContent?: string;
    newContent?: string;
    argsSummary?: string;
}

interface PermissionModalProps {
    permission: PendingPermission | null;
    onResult: (granted: boolean) => void;
}

function DiffView({ oldContent, newContent }: { oldContent: string; newContent: string }) {
    const oldLines = oldContent.split('\n');
    const newLines = newContent.split('\n');

    // 简单行级 diff：标记新增/删除/不变行
    const maxLen = Math.max(oldLines.length, newLines.length);
    const rows: { type: 'add' | 'remove' | 'same'; line: string }[] = [];

    // 使用最长公共子序列思路的简化版本：逐行对比
    // 对于 ide_write_file 场景，整文件替换，直接展示旧行删除 + 新行新增
    oldLines.forEach(line => rows.push({ type: 'remove', line }));
    newLines.forEach(line => rows.push({ type: 'add', line }));

    const colors: Record<string, string> = {
        add: 'rgba(40,167,69,0.15)',
        remove: 'rgba(220,53,69,0.15)',
        same: 'transparent',
    };
    const prefixes: Record<string, string> = { add: '+', remove: '-', same: ' ' };

    return (
        <div style={{
            fontFamily: 'monospace', fontSize: '12px', overflowY: 'auto',
            maxHeight: '50vh', border: '1px solid var(--border-subtle)',
            borderRadius: '6px', background: 'var(--bg-secondary)',
        }}>
            {rows.map((row, i) => (
                <div key={i} style={{
                    background: colors[row.type],
                    padding: '1px 8px',
                    whiteSpace: 'pre-wrap',
                    wordBreak: 'break-all',
                    color: row.type === 'add' ? '#3fb950' : row.type === 'remove' ? '#f85149' : 'var(--text-primary)',
                }}>
                    {prefixes[row.type]} {row.line}
                </div>
            ))}
        </div>
    );
}

export default function PermissionModal({ permission, onResult }: PermissionModalProps) {
    const confirmBtnRef = useRef<HTMLButtonElement>(null);

    useEffect(() => {
        if (permission) setTimeout(() => confirmBtnRef.current?.focus(), 100);
    }, [permission]);

    if (!permission) return null;

    const isWriteFile = permission.toolName === 'ide_write_file';
    const title = isWriteFile
        ? `写入文件：${permission.filePath ?? ''}`
        : `执行操作：${permission.toolName}`;

    return (
        <div style={{
            position: 'fixed', top: 0, left: 0, right: 0, bottom: 0,
            background: 'rgba(0,0,0,0.6)', display: 'flex', alignItems: 'center',
            justifyContent: 'center', zIndex: 10000,
        }}>
            <div style={{
                background: 'var(--bg-primary)', borderRadius: '12px', padding: '24px',
                width: isWriteFile ? '80vw' : '420px', maxWidth: '95vw',
                maxHeight: '90vh', display: 'flex', flexDirection: 'column',
                border: '1px solid var(--border-subtle)',
                boxShadow: '0 20px 60px rgba(0,0,0,0.5)',
            }}>
                <h4 style={{ marginBottom: '12px', fontSize: '15px', flexShrink: 0 }}>{title}</h4>

                <div style={{ flex: 1, overflow: 'hidden', marginBottom: '16px' }}>
                    {isWriteFile ? (
                        <DiffView
                            oldContent={permission.oldContent ?? ''}
                            newContent={permission.newContent ?? ''}
                        />
                    ) : (
                        <pre style={{
                            fontSize: '12px', background: 'var(--bg-secondary)',
                            borderRadius: '6px', padding: '10px', overflowX: 'auto',
                            color: 'var(--text-secondary)', whiteSpace: 'pre-wrap', wordBreak: 'break-all',
                        }}>
                            {permission.argsSummary ?? ''}
                        </pre>
                    )}
                </div>

                <div style={{ display: 'flex', justifyContent: 'flex-end', gap: '8px', flexShrink: 0 }}>
                    <button className="btn btn-secondary" onClick={() => onResult(false)}>拒绝</button>
                    <button ref={confirmBtnRef} className="btn btn-primary" onClick={() => onResult(true)}>
                        同意写入
                    </button>
                </div>
            </div>
        </div>
    );
}
```

- [ ] **Step 2: 提交**

```bash
cd /Users/shubinzhang/Documents/UGit/Clawith
git add frontend/src/components/PermissionModal.tsx
git commit -m "feat(frontend): add PermissionModal component for ide_write_file diff approval"
```

---

## Task 4：前端 — `AgentDetail.tsx` 集成 PermissionModal

**Files:**
- Modify: `frontend/src/pages/AgentDetail.tsx`

- [ ] **Step 1: 新增 import**

在文件顶部的 import 区域（第 1 行附近），找到其他组件 import（如 `ConfirmModal`），在其后新增：

```typescript
import PermissionModal, { PendingPermission } from '../components/PermissionModal';
```

- [ ] **Step 2: 新增 `pendingPermission` state**

在 `AgentDetail` 组件内，找到其他 `useState` 声明（约第 50 行附近），新增：

```typescript
const [pendingPermission, setPendingPermission] = useState<PendingPermission | null>(null);
```

- [ ] **Step 3: 在 `ws.onmessage` 中处理 `permission_request`**

在 `ws.onmessage` 回调（第 1987 行）的 `const d = JSON.parse(e.data);` 之后，在现有 `if (['thinking', 'chunk'...]` 判断之前，插入：

```typescript
        if (d.type === 'permission_request') {
            setPendingPermission({
                permissionId: d.permission_id,
                wsKey: key,
                toolName: d.tool_name,
                filePath: d.file_path,
                oldContent: d.old_content ?? '',
                newContent: d.new_content ?? '',
                argsSummary: d.args_summary ?? '',
            });
            return;
        }
```

- [ ] **Step 4: 新增 `handlePermissionResult` 函数**

在 `ensureSessionSocket` 函数之外、同组件内，新增（可放在 `sendMessage` 函数附近）：

```typescript
const handlePermissionResult = (granted: boolean) => {
    if (!pendingPermission) return;
    const socket = wsMapRef.current[pendingPermission.wsKey];
    if (socket && socket.readyState === WebSocket.OPEN) {
        socket.send(JSON.stringify({
            schemaVersion: 3,
            type: 'permission_result',
            permission_id: pendingPermission.permissionId,
            granted,
        }));
    }
    setPendingPermission(null);
};
```

- [ ] **Step 5: 在 JSX 中渲染 `<PermissionModal>`**

在组件 return 的 JSX 最外层 div 内，找到其他 Modal 渲染位置（如 `<ConfirmModal` 附近），新增：

```tsx
<PermissionModal
    permission={pendingPermission}
    onResult={handlePermissionResult}
/>
```

- [ ] **Step 6: 构建验证**

```bash
cd /Users/shubinzhang/Documents/UGit/Clawith/frontend
npm run build 2>&1 | tail -20
```

预期：无 TypeScript 错误，build 成功。

- [ ] **Step 7: 提交**

```bash
cd /Users/shubinzhang/Documents/UGit/Clawith
git add frontend/src/pages/AgentDetail.tsx
git commit -m "feat(frontend): handle permission_request in AgentDetail with PermissionModal"
```

---

## Task 5：端到端验证

- [ ] **Step 1: 重启后端**

```bash
cd /Users/shubinzhang/Documents/UGit/Clawith
# 按项目实际启动方式重启后端，例如：
pkill -f "uvicorn app.main" 2>/dev/null; sleep 1
cd backend && uvicorn app.main:app --reload --port 8000 &
```

- [ ] **Step 2: 在 WL4 智能体会话中触发文件写入**

让智能体执行一个会调用 `ide_write_file` 的任务（如修改一个 kt 文件）。

- [ ] **Step 3: 验证弹窗出现**

预期：前端弹出 `PermissionModal`，显示文件路径和新旧内容 diff（旧内容红色 `-`，新内容绿色 `+`）。

- [ ] **Step 4: 点击"同意写入"，验证文件被写入**

预期：
- 后端日志出现 `ACP permission resolved ... allowed=True`
- 文件内容被更新
- 智能体继续回复

- [ ] **Step 5: 再次触发，点击"拒绝"**

预期：
- 后端日志出现 `ACP ide tool blocked by permission`
- 智能体收到 `Permission denied for ide_write_file` 结果，继续回复（告知用户未执行写入）
