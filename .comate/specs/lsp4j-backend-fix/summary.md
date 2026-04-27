# LSP4J 后端代码审查修复 — 总结

## 修复概览

| # | 修复项 | 严重程度 | 涉及文件 | 状态 |
|---|--------|---------|---------|------|
| Fix-1 | `_active_routers` 复合键防跨用户泄漏 | Critical | context.py, router.py, jsonrpc_router.py | ✅ |
| Fix-2 | LSP 缓冲区大小限制防 DoS | Critical | lsp_protocol.py | ✅ |
| Fix-3 | 修正方法名 `tool/call/approve` | High | jsonrpc_router.py | ✅ |
| Fix-4 | 修正 `_dispatch` 运算符优先级 | High | jsonrpc_router.py | ✅ |
| Fix-5 | `chat/stop` 取消机制 + 并发保护 | High | jsonrpc_router.py | ✅ |
| Fix-6 | agent 权限校验 | High | router.py | ✅ |
| Fix-7 | 验证 `questionText` 字段 | — | — (已正确) | ✅ |
| Fix-8 | JSON-RPC 解析错误返回 -32700 | Medium | lsp_protocol.py, jsonrpc_router.py | ✅ |
| Fix-9 | f-string 日志改 loguru 惰性格式 | Medium | jsonrpc_router.py | ✅ |

## 详细变更

### Fix-1: `_active_routers` 复合键
- `context.py:41` — 类型从 `dict[str, Any]` 改为 `dict[tuple[str, str], Any]`
- `router.py:135` — `agent_key = (str(user_id), str(agent_obj.id))`
- `router.py:137` — `jsonrpc._agent_key = agent_key`
- `router.py:159` — cleanup 使用 `getattr(jsonrpc, "_agent_key", None)`
- `jsonrpc_router.py:644` — `invoke_lsp4j_tool()` 使用复合键查找

### Fix-2: LSP 缓冲区大小限制
- `lsp_protocol.py:25-29` — 添加 `_MAX_BUFFER_SIZE = 10MB` 和 `_MAX_CONTENT_LENGTH = 5MB`
- `lsp_protocol.py:46-60` — `read_message()` 缓冲区溢出保护，超出限制清空缓冲区
- `lsp_protocol.py:82-97` — `_try_parse_one()` Content-Length 超限丢弃

### Fix-3: 修正方法名
- `jsonrpc_router.py:618` — `"tool/callApprove"` → `"tool/call/approve"`

### Fix-4: 修正运算符优先级
- `jsonrpc_router.py:119` — `"result" in msg or "error" in msg` 加括号

### Fix-5: 取消机制 + 并发保护
- `jsonrpc_router.py:97` — `__init__()` 添加 `self._chat_lock = asyncio.Lock()`
- `jsonrpc_router.py:216-218` — 并发保护，锁已占用时返回错误
- `jsonrpc_router.py:232` — `async with self._chat_lock:` 包裹整个方法体
- `jsonrpc_router.py:257-259` — `on_chunk` 回调检查 `_cancel_event.is_set()`
- `jsonrpc_router.py:297` — `except asyncio.CancelledError` 标记 `cancelled = True`
- `jsonrpc_router.py:326` — 发送 `chat/finish` reason="cancelled"
- `jsonrpc_router.py:340-347` — `_handle_chat_stop` 添加 MVP 取消机制注释

### Fix-6: agent 权限校验
- `router.py:25-26` — 导入 `check_agent_access` 和 `User`
- `router.py:65-72` — agent 查找后查询 User 对象，调用 `check_agent_access(db, user_obj, agent.id)`，HTTPException 转 return None

### Fix-7: questionText 字段
- 已验证 `_handle_chat_ask()` 正确使用 `params.get("questionText")`，与 Java `ChatAskParam` 匹配

### Fix-8: JSON-RPC 解析错误
- `lsp_protocol.py:33-38` — 添加 `ParseError` 标记类
- `lsp_protocol.py:106` — JSON 解析失败返回 `ParseError(str(e))`
- `jsonrpc_router.py:47` — 导入 `ParseError`
- `jsonrpc_router.py:114-117` — `route()` 中检测 `isinstance(msg, ParseError)` 并发送 -32700 错误

### Fix-9: 日志格式
- `jsonrpc_router.py:779` — `logger.error(f"...{e}")` → `logger.error("...{}", e)`

## 验证结果

- ✅ Python 语法检查：4 个文件全部通过 `py_compile`
- ✅ Clawith 后端重启：启动成功，无报错
- ✅ WebSocket 端点可达：路由注册正确（400 = token 无效，非 404 路由缺失）
- ✅ Graphify 重建：3971 nodes, 15320 edges, 163 communities

## 未处理项（IDEA 插件侧，不在本次范围）

| # | 问题 | 严重程度 |
|---|------|---------|
| J-1 | API Key 明文存储在 XML | Critical |
| J-2 | API Key 作为 URL 查询参数 | Critical |
| J-3 | API Key 泄露给 Lingma 后端 | High |
| J-4 | CosySetting.toString() 暴露 API Key | High |
| J-5 | isModified() 不跟踪 Clawith 字段 | High |
| J-6 | 双重存储无权威来源 | High |
| J-7 | API Key 回显不一致 | Medium |
| J-8 | 无连接生命周期管理 | Medium |
| J-9 | 插件发送大量未实现方法 | Low |
