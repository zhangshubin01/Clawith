# LSP4J 后端代码审查修复任务

## 评审结论（基于实际代码核实）

- Fix-7 (questionText) 已在代码中正确实现，Task 7 改为验证确认
- Fix-5 (cancel) 确认 `call_llm` 不接受 `cancel_event`，`on_chunk` 回调方案是唯一可行路径
- Fix-3 (方法名) 确认 `tool/call/approve` 是 Java `@JsonSegment("tool/call")` + `@JsonRequest("approve")` 的拼接结果
- Fix-6 (权限) 确认 `check_agent_access` 需完整 `User` 对象（访问 `user.role`、`user.tenant_id`、`user.id`）
- ACP 的 `_resolve_agent_override` 同样不检查权限（已知缺陷，不在本次范围）

---

- [x] Task 1: Fix-1 `_active_routers` 改用复合键防跨用户泄漏
    - 1.1: `context.py:41` — `_active_routers` 类型从 `dict[str, Any]` 改为 `dict[tuple[str, str], Any]`，注释说明复合键 `(user_id_hex, agent_id_hex)` 用途
    - 1.2: `jsonrpc_router.py:67-98` — `__init__()` 中添加 `self._agent_key: tuple[str, str]` 实例变量
    - 1.3: `router.py:134-135` — 改为 `agent_key = (str(user_id), str(agent_obj.id))`，`_active_routers[agent_key] = jsonrpc`，同时 `jsonrpc._agent_key = agent_key`
    - 1.4: `router.py:157` — cleanup 改为 `_active_routers.pop(jsonrpc._agent_key, None)`
    - 1.5: `jsonrpc_router.py:644` — `invoke_lsp4j_tool()` 改为 `agent_key = (str(user_id), str(agent_id))`

- [x] Task 2: Fix-2 LSP 缓冲区添加大小限制
    - 2.1: `lsp_protocol.py` — 在类外添加常量 `_MAX_BUFFER_SIZE = 10 * 1024 * 1024` 和 `_MAX_CONTENT_LENGTH = 5 * 1024 * 1024`，附中文注释
    - 2.2: `read_message()` — 入口添加缓冲区溢出检查：`if len(self._buffer_bytes) + len(incoming) > _MAX_BUFFER_SIZE` 则清空并记录错误
    - 2.3: `_try_parse_one()` — Content-Length 解析后检查 `if content_length > _MAX_CONTENT_LENGTH` 则丢弃并记录警告

- [x] Task 3: Fix-3 修正 JSON-RPC 方法名匹配 Java 插件
    - 3.1: `jsonrpc_router.py:616` — `_METHOD_MAP` 中 `"tool/callApprove"` 改为 `"tool/call/approve"`
    - 3.2: 添加中文注释说明方法名来源：Java `@JsonSegment("tool/call")` + `@JsonRequest("approve")` → `tool/call/approve`

- [x] Task 4: Fix-4 修正 `_dispatch` 运算符优先级
    - 4.1: `jsonrpc_router.py:119` — 添加括号：`if "id" in msg and "method" not in msg and ("result" in msg or "error" in msg):`

- [x] Task 5: Fix-5 `chat/stop` 取消 + 并发保护
    - 5.1: `jsonrpc_router.py` — `__init__()` 添加 `self._chat_lock = asyncio.Lock()`，中文注释说明用途
    - 5.2: `_handle_chat_ask()` — 入口加锁：`async with self._chat_lock:`，并重置 `self._cancel_event = asyncio.Event()`
    - 5.3: `_handle_chat_ask()` — `on_chunk` 回调中检查 `self._cancel_event.is_set()`，若已取消则抛 `asyncio.CancelledError`
    - 5.4: `_handle_chat_ask()` — 外层 `try/except CancelledError`，捕获后发送 `chat/finish`（reason="cancelled"）
    - 5.5: `_handle_chat_stop()` — 保留 `_cancel_event.set()`，添加中文注释说明 MVP 取消机制

- [x] Task 6: Fix-6 agent 权限校验（复用 `check_agent_access`）
    - 6.1: `router.py` — 导入 `from app.core.permissions import check_agent_access` 和 `from app.models.user import User`
    - 6.2: `_resolve_agent_override()` — agent 查找成功后，查询 User 对象，调用 `check_agent_access(db, user_obj, agent.id)`，`HTTPException` 转 `return None`，中文注释说明复用逻辑

- [x] Task 7: Fix-7 验证 chat/ask 使用 Java 插件 `questionText` 字段
    - 7.1: 验证确认 `_handle_chat_ask()` 已正确使用 `params.get("questionText")` 字段（`jsonrpc_router.py:204-207`），与 Java `ChatAskParam.java:15` 完全匹配，无需修改代码

- [x] Task 8: Fix-8 JSON-RPC 解析错误返回 -32700
    - 8.1: `lsp_protocol.py` — 添加 `class ParseError` 标记类（含 `message` 属性），`_try_parse_one()` JSON 解析失败返回 `ParseError()` 替代 `None`
    - 8.2: `jsonrpc_router.py` — `route()` 循环中检测 `isinstance(msg, ParseError)`，调用 `_send_error_response(None, -32700, msg.message)`

- [x] Task 9: Fix-9 f-string 日志改为 loguru 惰性格式
    - 9.1: `jsonrpc_router.py:754` — `logger.error(f"LSP4J: 对话持久化失败: {e}")` → `logger.error("LSP4J: 对话持久化失败: {}", e)`

- [x] Task 10: 验证与部署
    - 10.1: Python 语法检查（`python -m py_compile` 每个修改的文件）
    - 10.2: 重启 Clawith 后端，确认插件加载且无报错
    - 10.3: WebSocket 端点可达性测试（wscat 连接 + initialize 握手）
    - 10.4: 运行 graphify 重建：`python3 -c "from graphify.watch import _rebuild_code; from pathlib import Path; _rebuild_code(Path('.'))"`

---

## IDEA 插件源码问题清单

以下问题基于 `/Users/shubinzhang/Downloads/demo-new` 源码分析，不属于本次 Python 后端修复范围，但需记录以便后续迭代：

### 安全问题

| # | 问题 | 严重程度 | 文件 | 说明 |
|---|------|---------|------|------|
| J-1 | API Key 明文存储在 XML | Critical | `CosySetting.java:59` | `clawithApiKey` 由 `CosyPersistentSetting` 序列化到 `cosy_setting.xml`，任何有文件系统访问权限的人都能读取。应使用 IntelliJ `PasswordSafe` API |
| J-2 | API Key 作为 URL 查询参数 | Critical | `LanguageWebSocketService.java:180-182` | `token=apiKey` 拼在 WebSocket URL 上，会被服务器访问日志、代理、CDN 记录。应改为握手 header 或首条消息认证 |
| J-3 | API Key 泄露给 Lingma 后端 | High | `ConfigMainForm.java:1093-1094, 1107` | Clawith API Key 通过 `updateGlobalConfig()` 发送给 Lingma LSP 服务端。应在发送前剥离 `clawithApiKey` 字段 |
| J-4 | `CosySetting.toString()` 暴露 API Key | High | `CosySetting.java:304` | `JsonUtil.toJson(this)` 序列化所有字段。`GlobalConfig.toString()` 已正确脱敏为 `"***"`，但 `CosySetting` 没有 |

### 功能问题

| # | 问题 | 严重程度 | 文件 | 说明 |
|---|------|---------|------|------|
| J-5 | `isModified()` 不跟踪 Clawith 字段 | High | `CosyConfigurable.java:75-193` | 用户修改 Clawith 配置后 Apply/OK 按钮保持禁用。需添加 4 个 Clawith 字段比较 |
| J-6 | 双重存储无权威来源 | High | `CosySetting` + `GlobalConfig` | 4 个 Clawith 字段同时存在两处，可能不同步。`CosySetting`（XML 持久化）应为唯一权威 |
| J-7 | API Key 回显不一致 | Medium | `ConfigMainForm.java:568` vs `CosyConfigurable.java:359` | `initGlobalConfigPanel()` 注释说"API Key 不回显"，但 `reset()` 实际会填入。需统一策略 |
| J-8 | 无连接生命周期管理 | Medium | `LanguageWebSocketService.java` | 启用/禁用 Clawith 后端或修改 URL 时，未关闭旧连接或创建新连接 |

### 协议问题

| # | 问题 | 严重程度 | 文件 | 说明 |
|---|------|---------|------|------|
| J-9 | 插件发送大量未实现方法 | Low | 多个 Service 接口 | Java 插件定义了 50+ 个 JSON-RPC 方法（`chat/listAllSessions`, `chat/getSessionById`, `session/getCurrent`, `auth/login`, `config/getGlobal` 等），Python 后端目前只实现了 7 个。插件调用未实现方法会收到 `-32601 Method not found` 错误，不影响核心对话功能但部分 UI 功能不可用 |
