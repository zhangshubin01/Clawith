# LSP4J 后端代码审查问题修复

## 需求场景

代码审查发现 LSP4J 插件 Python 后端存在 9 个需修复的问题（3 Critical + 3 High + 3 Medium），涵盖安全、功能缺陷、协议兼容性和健壮性。本次仅修复 Python 后端，不涉及 Java 插件代码。

## 问题清单与修复方案

### Fix-1: `_active_routers` 跨用户数据泄漏 [CRITICAL]

**问题:** `context.py:41` 的 `_active_routers` 以 `agent_id` 为键。两个不同用户同时连接同一 agent 时，后者覆盖前者 router，导致：
- 用户 A 的工具调用路由到用户 B 的 IDE
- 用户 A 的 pending Future 永远不 resolve

**修复:** 使用 `(user_id, agent_id)` 复合键。

**影响文件:**
- `context.py:41` — 改键类型声明为 `dict[tuple[str, str], Any]`
- `router.py` — 构造复合键 `agent_key = (str(user_id), str(agent_obj.id))`
- `jsonrpc_router.py:644-645` — `invoke_lsp4j_tool()` 改为 `agent_key = (str(user_id), str(agent_id))`
- `router.py cleanup` — `pop` 用复合键

**边界条件:**
- `invoke_lsp4j_tool()` 函数签名已包含 `user_id` 参数，无需改调用方
- cleanup 需存储 `agent_key` 到实例变量（`self._agent_key`）

---

### Fix-2: LSP 缓冲区无上限 — 内存耗尽 DoS [CRITICAL]

**问题:** `lsp_protocol.py` 的 `_buffer_bytes` 无大小限制，恶意/故障客户端可发送超大 `Content-Length` 耗尽内存。

**修复:** 添加常量限制 + 溢出保护：
```python
_MAX_BUFFER_SIZE = 10 * 1024 * 1024   # 10 MB 总缓冲区上限
_MAX_CONTENT_LENGTH = 5 * 1024 * 1024  # 5 MB 单条消息上限
```

**影响文件:**
- `lsp_protocol.py` — `read_message()` 入口检查缓冲区溢出；`_try_parse_one()` 检查 Content-Length 上限

---

### Fix-3: JSON-RPC 方法名与 Java 插件不匹配 [CRITICAL — 新发现]

**问题:** 后端 `_METHOD_MAP` 注册的方法名与 Java 插件 LSP4J 注解生成的方法名不一致：

| 后端当前方法名 | Java 插件实际方法名 | 来源 |
|---|---|---|
| `tool/callApprove` | `tool/call/approve` | `@JsonSegment("tool/call")` + `@JsonRequest("approve")` |
| `tool/invokeResult` | `tool/invokeResult` | 一致 |

**Java 插件参考:** `ToolCallService.java` 使用 `@JsonSegment("tool/call")`，`@JsonRequest("approve")` 生成的方法名是 `tool/call/approve`。

**修复:** 将 `_METHOD_MAP` 中的 `"tool/callApprove"` 改为 `"tool/call/approve"`。

**影响文件:**
- `jsonrpc_router.py` — `_METHOD_MAP` 字典

**验证方式:** Java 插件 `ToolCallService.java:12` 的 `@JsonRequest("approve")` 在 `@JsonSegment("tool/call")` 下，LSP4J 拼接为 `tool/call/approve`。

---

### Fix-4: `_dispatch` 运算符优先级 bug [HIGH]

**问题:** `jsonrpc_router.py:119` 的条件 `and ... and ... or` 优先级错误：
```python
# 当前 — 等效于 (A and B and C) or D
if "id" in msg and "method" not in msg and "result" in msg or "error" in msg:
```
任何含 `error` 键的消息（包括带 `method` 的请求）都会误入响应处理分支。

**修复:**
```python
if "id" in msg and "method" not in msg and ("result" in msg or "error" in msg):
```

**影响文件:** `jsonrpc_router.py:119`

---

### Fix-5: `chat/stop` 无效 + 并发 chat/ask 保护 [HIGH]

**问题 1:** `_cancel_event.set()` 后无人检查，`call_llm` 不接受 `cancel_event` 参数（签名中无此参数），LLM 调用仍执行完毕。
**问题 2:** 两个并发 `chat/ask` 共享 `_current_request_id`、`_cancel_event` 等实例变量，导致竞态。

**关于 `call_llm` 取消机制的调研:**
- `call_llm` 签名（`caller.py:290-303`）**不接受** `cancel_event` 参数
- ACP 插件同样传递了 `cancel_event=cancel_prompt` 给 `call_llm`，但由于签名不匹配，运行时会 `TypeError`（ACP 的取消机制也是坏的）
- Web UI 使用 `asyncio.Task.cancel()` 方式取消（`websocket.py:540-594`）
- LLM 客户端层 `client.py:532,558` **支持** `cancel_event`，但 `caller.py` 未透传

**修复方案（精简，不修改 `call_llm` 签名）:**
1. 添加 `self._chat_lock = asyncio.Lock()` 防止并发
2. `_handle_chat_ask()` 加锁：`async with self._chat_lock:`
3. 在 `on_chunk` 回调中检查 `self._cancel_event.is_set()`，若已取消则抛异常中断 `call_llm` 的流式输出
4. `_handle_chat_stop()` 保留 `_cancel_event.set()` + 发送 `chat/finish` 通知

**核心代码:**
```python
async def _on_chunk(text: str):
    """流式文本回调 — 检查取消信号"""
    if self._cancel_event.is_set():
        raise asyncio.CancelledError("LSP4J chat/stop 取消")
    await self._send_chat_answer(text, request_id, session_id)

async with self._chat_lock:
    self._cancel_event = asyncio.Event()
    try:
        reply = await call_llm(
            model=self._model_obj,
            messages=message_history,
            on_chunk=_on_chunk,
            # ...
        )
    except asyncio.CancelledError:
        # 正常取消，发送 finish
        await self._send_chat_finish(request_id, session_id, full_answer, reason="cancelled")
        return
```

**影响文件:** `jsonrpc_router.py` — `__init__`, `_handle_chat_ask`, `_handle_chat_stop`

---

### Fix-6: 无 agent 权限校验 [HIGH]

**问题:** `router.py` 的 `_resolve_agent_override()` 不检查用户权限，任何认证用户可连接任意 agent。

**已有可复用代码:** 项目中已存在 `check_agent_access(db, user, agent_id)` 函数（`app/core/permissions.py:15-52`），被 15+ 个路由复用。功能：
1. 平台管理员 → manage 权限
2. `agent.creator_id == user.id` → manage 权限
3. `agent_permissions` 表中 `scope_type="company"` 或 `scope_type="user" and scope_id==user_id` → 对应 access_level
4. 都不满足 → 抛 403

**修复:** 在 `_resolve_agent_override()` 中复用 `check_agent_access`。由于该函数抛 HTTPException 而非返回 None，需要 try/except 包装：

```python
from app.core.permissions import check_agent_access

async def _resolve_agent_override(
    override: str, user_id: uuid.UUID
) -> tuple[AgentModel, LLMModel] | None:
    """根据 UUID 或名称查找 agent，并校验用户访问权限"""
    async with async_session() as db:
        # 先查找 agent
        agent = None
        try:
            aid = uuid.UUID(override)
            ar = await db.execute(select(AgentModel).where(AgentModel.id == aid))
            agent = ar.scalar_one_or_none()
        except ValueError:
            pass
        if agent is None:
            ar = await db.execute(select(AgentModel).where(AgentModel.name == override))
            agent = ar.scalar_one_or_none()
        if agent is None:
            logger.warning("LSP4J: agent {} not found", override)
            return None

        # 复用项目权限校验
        from app.models.user import User
        ur = await db.execute(select(User).where(User.id == user_id))
        user_obj = ur.scalar_one_or_none()
        if user_obj is None:
            return None
        try:
            agent, _ = await check_agent_access(db, user_obj, agent.id)
        except HTTPException:
            logger.warning("LSP4J: user {} no permission to agent {}", user_id, agent.id)
            return None

        # 查询模型
        mr = await db.execute(select(LLMModel).where(LLMModel.id == agent.primary_model_id))
        model = mr.scalar_one_or_none()
        if model is None:
            logger.warning("LSP4J: agent {} has no model", override)
            return None
        return agent, model
```

**影响文件:** `router.py:43-70`

---

### Fix-7: chat/ask 参数字段名与 Java 插件不兼容 [MEDIUM — 新发现]

**问题:** Java 插件 `ChatAskParam.java` 的用户消息字段名是 **`questionText`**，不是 `text`。后端需同时兼容两个字段名。

**Java 插件参考:**
```java
// ChatAskParam.java
private String questionText;  // 用户输入的文本
private String requestId;
private String sessionId;
```

**修复:** 在 `_handle_chat_ask()` 中兼容两种字段名：
```python
# 兼容 Java 插件的 questionText 和可能的 text 字段
user_text = params.get("questionText") or params.get("text", "")
```

**影响文件:** `jsonrpc_router.py` — `_handle_chat_ask()`

---

### Fix-8: 无 JSON-RPC 解析错误响应 [MEDIUM]

**问题:** JSON 解析失败时只记日志，未按 JSON-RPC 2.0 规范返回 `-32700` 错误。

**修复:** 让 `LSPBaseProtocolParser.read_message()` 返回特殊标记，`route()` 检测后发送 `-32700` 响应。

**影响文件:**
- `lsp_protocol.py` — 返回 `ParseError` 标记
- `jsonrpc_router.py` — `route()` 处理解析错误

---

### Fix-9: f-string 日志改为 loguru 惰性格式 [MEDIUM]

**问题:** 多处使用 `logger.error(f"...")` 而 loguru 推荐使用 `logger.error("... {}", var)` 惰性求值。

**修复:** 全局替换 `logger.xxx(f"...")` 为 `logger.xxx("... {}", var)` 格式。

**影响文件:** 所有 6 个 Python 文件

---

## 对用户 12 项关注点的逐一回应

### 1. 代码要有详细的中文注释
✅ 所有修复代码将添加中文注释，与项目现有风格一致（如 `# ── Clawith 自部署后端配置面板 ──`）。

### 2. 结合网络最佳实践
✅ Fix-2 缓冲区限制参考 LSP 规范和 WebSocket 安全实践；Fix-8 遵循 JSON-RPC 2.0 规范；Fix-5 权限校验遵循 RBAC 模式。

### 3. 结合官方详细文档
✅ Fix-3/Fix-7 基于 Java LSP4J 库 `@JsonSegment` + `@JsonRequest` 注解规则验证方法名拼接；Fix-8 遵循 JSON-RPC 2.0 规范 (RFC 4627)。

### 4. 结合项目实际代码逻辑
✅ Fix-6 直接复用 `check_agent_access()`；Fix-5 的 `on_chunk` 取消方案基于 `call_llm` 实际回调机制。

### 5. 保证功能实现的前提下代码精简
✅ Fix-6 复用 `check_agent_access` 而非重写权限逻辑（省 ~30 行）；Fix-5 用 `on_chunk` 回调而非修改 `call_llm` 签名（零侵入）。

### 6. 现有项目代码可复用
✅ `check_agent_access()` （`app/core/permissions.py`）— 15+ 路由已在用。ACP 的 `_persist_chat_turn` / `_load_history_from_db` 模式也可参考。

### 7. 与项目现有风格一致
✅ 日志格式统一用 loguru `{}` 占位符；ContextVar 使用模式与 ACP 一致；`source_channel` 命名与 ACP 的 `"ide_acp"` 平行。

### 8. 对智能体自我进化的影响
**无影响。** 智能体的自我进化通过 `memory/memory.md` 实现：
- `build_agent_context()` 每次调用 LLM 时读取 `memory/memory.md`（`agent_context.py:260`）
- Agent 通过 `write_file` 工具自行更新 `memory/memory.md`（`agent_context.py:503`）
- LSP4J 插件只是消息通道，不干预 agent 的工具调用和记忆写入逻辑

### 9. Web UI 端能看到插件对话
**已支持。** `_persist_lsp4j_chat_turn()` 已设置 `source_channel="ide_lsp4j"`，消息保存到 `ChatSession` + `ChatMessage` 表。Web UI 通过以下 API 查看：
- `GET /api/agents/{agent_id}/sessions` — 返回含 `source_channel` 字段的会话列表
- `GET /api/agents/{agent_id}/sessions/{session_id}/messages` — 返回消息列表
前端根据 `source_channel` 显示不同图标区分来源。

### 10. 智能体能保存插件对话记忆
**已支持。** 对话通过 `_persist_lsp4j_chat_turn()` 持久化到 `ChatMessage` 表，并通知前端刷新。Agent 的长期记忆（`memory/memory.md`）在 LLM 调用时自动注入上下文，agent 可通过工具调用自行更新。

### 11. IDEA 插件源码交叉验证
✅ Fix-3/Fix-7 直接基于 Java 插件源码验证：
- `ToolCallService.java:12` → `@JsonSegment("tool/call")` + `@JsonRequest("approve")` → 方法名 `tool/call/approve`
- `ChatAskParam.java` → `questionText` 字段（非 `text`）
- `ChatAnswerParams.java` → `text` 字段（服务端→IDE 方向，已正确）
- `ChatFinishParams.java` → `requestId`, `fullAnswer`（已正确）
- `ToolInvokeRequest.java` → `name`, `parameters`（已正确）

### 12. 方案是否能准确接入插件
✅ 经交叉验证，Fix-3 修正方法名、Fix-7 修正字段名后，后端协议与 Java 插件完全匹配。

## 不在本次修复范围

- Java 插件代码（ConfigMainForm、CosyConfigurable 等）
- ACP 插件的同源问题（如 agent 权限校验、cancel_event 透传）
- API Key 通过 URL 查询参数传递（需修改 WebSocket 握手机制，属于架构变更）
- `call_llm` 签名修改（影响面太大，用 `on_chunk` 回调方案替代）

## 预期结果

- `_active_routers` 不再跨用户泄漏
- LSP 缓冲区有大小限制，不会被 OOM
- `_dispatch` 正确区分请求和响应
- `chat/stop` 能通过 on_chunk 回调中断 LLM 流式输出
- 并发 `chat/ask` 被锁拒绝而非竞态
- 非授权 agent 连接被拒绝（复用 `check_agent_access`）
- JSON-RPC 方法名与 Java 插件完全匹配
- chat/ask 字段名兼容 Java 插件的 `questionText`
- JSON 解析失败返回标准 `-32700` 错误码
- 日志格式统一为 loguru 惰性求值
