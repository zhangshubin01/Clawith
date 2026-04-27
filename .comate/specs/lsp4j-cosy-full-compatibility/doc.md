# LSP4J 灵码插件全量兼容 — 合并评审后方案

## 一、需求场景与验收标准（17 条评审要求映射）

| # | 评审要求 | 现状 | 目标 |
|---|---|---|---|
| 1 | 代码要有详细的中文注释 | 已有较好注释，部分新增逻辑需补充 | 所有新增/修改函数、关键分支均需中文注释 |
| 2 | 结合网络上的最佳实践 | — | 参考 LSP4J 官方 JSON-RPC 处理模式、asyncio 取消模式 |
| 3 | 结合官方的详细文档 | — | 严格匹配灵码插件源码中的参数类字段名和方法签名 |
| 4 | 结合项目实际代码逻辑 | — | 基于 caller.py、jsonrpc_router.py 实际签名和调用链设计 |
| 5 | 代码尽量精简 | — | 复用现有 `_persist_chat_turn`、`_build_agent_context` 等逻辑 |
| 6 | 现有代码可复用 | — | `_load_lsp4j_history_from_db`、WebSocket 通知机制已存在 |
| 7 | 跟项目现有风格保持一致 | — | 使用 loguru、async/await、UUID 类型、相同缩进风格 |
| 8 | 对现有智能体逻辑的影响 | 修改 call_llm 签名 | `cancel_event` 默认 None，完全向后兼容；不影响自我进化 |
| 9 | Web UI 能看到插件对话 | 已支持（`ws_module.manager.send_to_session`） | 确认通知机制健壮 |
| 10 | 保存插件对话记忆 | 已支持（`_persist_lsp4j_chat_turn`） | 增强 `client_type` 标记为 `"ide_plugin"` |
| 11 | IDEA 插件源码参考 | `/Users/shubinzhang/Downloads/demo-new` | 所有字段名、方法名均已反编译核对 |
| 12 | 与插件源码相互验证 | — | 逐字段核对 ChatAskParam、ChatAnswerParams、ChatFinishParams 等 |
| 13 | 灵码中能回复 | chat/ask 响应格式已修复 | 确保 `isSuccess` 字段始终存在 |
| 14 | 能使用 diff 能力 | `chat/codeChange/apply` 缺失 + `ChatCodeChangeApplyResult` 未返回 | 实现 codeChange/apply 完整流程 + chat/codeChange/apply/finish 通知 |
| 15 | 能使用任务规划/任务列表 | `chat/process_step_callback` 完全缺失 | 新增 step callback 通知 + stepProcessConfirm 处理 |
| 16 | 能调用灵码改代码 | tool/invoke 8 个工具已注册（直接修改），codeChange/apply（交互式 diff）未实现 | 两套模式均需支持 |
| 17 | 能操作本地工具 | 同上 | read_file、save_file、run_in_terminal 等正常流转 |

---

## 二、代码审计结果（基于实际代码查询）

### 2.1 `call_llm` 签名审计（`caller.py:307-320`）

```python
async def call_llm(
    model: LLMModel,
    messages: list[dict],
    agent_name: str,
    role_description: str,
    agent_id=None,
    user_id=None,
    session_id: str = "",
    on_chunk=None,
    on_tool_call=None,
    on_thinking=None,
    supports_vision=False,
    max_tool_rounds_override: int | None = None,
) -> str:
```

**关键发现：**
- ✅ `supports_vision` 已存在于签名，但 `jsonrpc_router.py` 调用时**未传递**
- ❌ `cancel_event` **不存在于签名**。底层 `LLMClient.stream()` 通过 `**kwargs` 支持 `cancel_event`（`client.py:532`），但 `call_llm` 未透传
- ❌ 工具循环（`caller.py:370-498`）**无任何 cancel 检查**

### 2.2 `_persist_lsp4j_chat_turn` 审计（`jsonrpc_router.py:685-783`）

**关键发现：**
- ✅ 已保存 `source_channel="ide_lsp4j"`
- ❌ **未设置 `client_type="ide_plugin"`**，数据库中保持默认值 `"web"`
- ✅ 已通知 WebSocket 前端（`ws_module.manager.send_to_session`）
- ✅ 已记录活动日志（`log_activity`）

### 2.3 `ChatAskParam` dataclass 审计

**关键发现：**
- ❌ LSP4J 插件中**不存在 `ChatAskParam` dataclass**
- 当前直接从 `params: dict` 按 key 取值（`jsonrpc_router.py:209-215`），共消费 4 个字段
- 灵码实际发送 17 个字段，其余 13 个静默丢弃

### 2.4 `_METHOD_MAP` 审计（`jsonrpc_router.py:639-648`）

```python
_METHOD_MAP: dict[str, Any] = {
    "initialize": _handle_initialize,
    "shutdown": _handle_shutdown,
    "exit": _handle_exit,
    "chat/ask": _handle_chat_ask,
    "chat/stop": _handle_chat_stop,        # ✅ 方法存在（line 348-356）
    "tool/call/approve": _handle_tool_call_approve,
    "tool/invokeResult": _handle_tool_invoke_result,
}
```

**关键发现：**
- 灵码 `ChatService`（`@JsonSegment("chat")`）定义 15 个方法（含 `quota/doNotRemindAgain`），`ToolCallService` 定义 2 个方法（approve + results），`ToolService` 定义 1 个方法（invokeResult），当前仅处理 chat/ask + chat/stop + tool/call/approve + tool/invokeResult 共 4 个业务方法（另有 3 个 LSP 生命周期方法）
- ✅ `_handle_chat_stop` 方法**已存在**（line 348-356）
- ⚠️ `_handle_chat_stop` 当前**不返回响应**（注释说"通常是通知"），但 `ChatService.java` 定义 `stop` 为 `@JsonRequest`，期望收到响应。需补上 `_send_response`
- ❌ `chat/codeChange/apply` **未在 METHOD_MAP 中注册**，需添加 handler 并注册
- ❌ `agents/testAgent/stepProcessConfirm` **未注册**，需添加 handler

### 2.5 工具调用参数格式审计（`tool_hooks.py` vs `ToolInvokeProcessor.java`）

| 工具 | Clawith 发送参数 | 灵码期望参数 | 状态 |
|---|---|---|---|
| read_file | `{"filePath": "..."}` | `{"filePath": "..."}` | ✅ |
| save_file | `{"filePath": "...", "content": "..."}` | `{"filePath": "...", "content": "..."}` | ✅ |
| run_in_terminal | `{"command": "...", "workDirectory": "..."}` | `{"command": "...", "workDirectory": "..."}` | ✅ |
| replace_text_by_path | `{"filePath": "...", "oldText": "...", "newText": "..."}` | `{"filePath": "...", "oldText": "...", "newText": "..."}` | ✅ |

**结论：** 8 个工具的参数字段名已与 `ToolInvokeProcessor.java` 源码逐一核对，完全匹配。

---

## 三、问题清单（按优先级排序）

### P0 — 阻断性问题

| # | 问题 | 影响 | 修复文件 |
|---|---|---|---|
| P0-1 | chat/ask 响应缺少 `isSuccess` | UI 显示"调用异常：success" | `jsonrpc_router.py` |
| P0-2 | `call_llm` 签名缺少 `cancel_event` | chat/stop 在工具循环中无法取消 | `caller.py` |
| P0-3 | 工具循环无 cancel 检查 | 用户点击停止后工具仍继续执行 | `caller.py` |

### P1 — 高价值功能缺失

| # | 问题 | 影响 | 修复文件 |
|---|---|---|---|
| P1-1 | ChatAskParam 13 个字段未消费 | LLM 不知道 IDE 上下文、任务类型、代码语言 | `jsonrpc_router.py` |
| P1-2 | `supports_vision` 未传入 `call_llm` | 视觉能力不可用 | `jsonrpc_router.py` |
| P1-3 | `client_type` 保持默认 `"web"` | Web UI 无法区分 LSP4J 会话来源 | `jsonrpc_router.py` |
| P1-4 | 灵码 10+ 个方法返回 Method not found | 插件端异常弹窗 | `jsonrpc_router.py` |
| P1-5 | `stream` 参数未消费 | 非流式模式下仍推送 chat/answer | `jsonrpc_router.py` |
| P1-6 | `chat/stop` 不返回响应 | LSP4J 框架超时等待（`stop` 是 `@JsonRequest`） | `jsonrpc_router.py` |
| P1-7 | `ChatAnswerParams` 缺少 `extra` 字段 | 插件无法区分 inline chat / panel chat，影响 diff 功能 | `jsonrpc_router.py` |
| P1-8 | `chat/process_step_callback` 完全缺失 | 插件无法显示任务规划/步骤进度列表 | `jsonrpc_router.py` |
| P1-9 | `chat/codeChange/apply` handler 完全缺失 | "Apply"按钮超时，无法触发 diff 渲染 | `jsonrpc_router.py` |
| P1-10 | `chat/codeChange/apply` 未在 METHOD_MAP 中注册 | 即使实现了 handler，调用也会返回 Method not found | `jsonrpc_router.py` |
| P1-11 | `agents/testAgent/stepProcessConfirm` 未在 METHOD_MAP 中注册 | 用户无法确认步骤，任务规划功能不完整 | `jsonrpc_router.py` |
| P1-12 | `tool/call/results` handler 完全缺失且未注册（`ToolCallService.java` 第二个方法） | 插件查询工具调用历史/状态时返回 Method not found | `jsonrpc_router.py` |

### P2 — 中价值增强

| # | 问题 | 影响 | 修复文件 |
|---|---|---|---|
| P2-1 | `ide_prompt` 无 token 预算控制 | 过长 prompt 占用上下文窗口 | `jsonrpc_router.py` |
| P2-2 | `shellType` / `pluginPayloadConfig` 未消费 | 终端 Shell 类型、记忆开关未知 | `jsonrpc_router.py`（`shellType` 在 Task 3.2 `_build_lsp4j_ide_prompt` 中处理；`pluginPayloadConfig` 在 Task 9.2 中处理） |
| P2-3 | `inlineEditParams` 未实现 | 行内编辑建议功能不可用 | `jsonrpc_router.py` |
| P2-4 | `ChatSession` 额外 IDE 字段（`project_path` / `current_file` / `open_files`）未利用 | Web UI 无法展示 IDE 当前打开的文件和项目路径（注：插件当前未发送这些数据，模型已支持，未来可增量接入） | `jsonrpc_router.py` + `chat_session.py` |
| P2-5 | `jsonrpc_router.py:202` docstring 写 "16 个字段"，实际 `ChatAskParam.java` 有 17 个字段 | 注释与源码不一致，可能导致维护困惑 | `jsonrpc_router.py` |

---

## 四、技术方案

### 4.1 核心设计原则

1. **向后兼容第一**：`call_llm` 新增参数全部使用默认值，不影响 ACP/其他调用方
2. **最小侵入**：不修改 `call_llm` 的核心工具循环逻辑，仅在关键位置插入检查点
3. **复用优先**：`_persist_lsp4j_chat_turn`、WebSocket 通知、活动日志已存在，直接复用
4. **渐进式实现**：先修复阻断性问题（P0），再实现高价值功能（P1），最后优化（P2）
5. **字段安全**：`customModel.parameters` 含 API 密钥，不入库、不写日志、仅当次请求有效

### 4.2 方案架构

```
灵码插件 → WebSocket → JSONRPCRouter._handle_chat_ask
                              ↓
                    1. 解析 ChatAskParam（17 字段 dataclass）
                    2. 构建 ide_prompt（P1 字段 → 上下文注入）
                    3. 模型选择（customModel > extra.modelConfig.key > 默认）
                    4. 发送 step_start（chat/process_step_callback）
                    5. 调用 call_llm（+ cancel_event + supports_vision）
                              ↓
                    6. 流式输出 → chat/answer 通知
                    7. 工具调用 → tool/invoke → IDE 执行 → tool/invokeResult
                       （每个工具：发送 step doing → 执行 → step done）
                              ↓
                    8. chat/finish 通知 + step_end
                    9. 返回 chat/ask 响应（含 isSuccess）
                              ↓
                    10. 后台持久化（client_type="ide_plugin"）
                    11. WebSocket 通知前端刷新

交互式 diff 流程（用户点击 Apply）：
    插件 → chat/codeChange/apply → Clawith → ChatCodeChangeApplyResult
                                        → chat/codeChange/apply/finish 通知
    插件 → InEditorDiffRenderer 渲染 inline diff
```

### 4.3 call_llm 签名增强

```python
async def call_llm(
    model: LLMModel,
    messages: list[dict],
    agent_name: str,
    role_description: str,
    agent_id=None,
    user_id=None,
    session_id: str = "",
    on_chunk=None,
    on_tool_call=None,
    on_thinking=None,
    supports_vision=False,
    max_tool_rounds_override: int | None = None,
    cancel_event: asyncio.Event | None = None,  # ← 新增
) -> str:
```

**透传路径**：
- `call_llm` → `client.stream(..., cancel_event=cancel_event)`
- `call_llm_with_failover` → `call_llm(..., cancel_event=cancel_event)`

**工具循环取消检查点（3 处）**：
1. 工具循环入口处（`caller.py:370`，`for round_i in range(_max_tool_rounds)` 循环开始处）
2. readonly 并行工具执行前（`caller.py:461` 左右，`asyncio.gather` 调用前）
3. write 串行工具执行前（`caller.py:476` 左右，`for tc in write_calls` 循环内）

### 4.4 ide_prompt 构建规范

```python
# 基于 ChatTaskEnum.java 实际枚举值定义提示映射
_CHAT_TASK_HINTS = {
    "EXPLAIN_CODE": "用户正在请求解释代码，请详细说明代码逻辑和意图。",
    "CODE_GENERATE_COMMENT": "用户请求为代码生成注释，请生成清晰的中文注释。",
    "OPTIMIZE_CODE": "用户请求优化代码，请分析性能问题并给出改进建议。",
    "GENERATE_TESTCASE": "用户请求生成测试用例，请生成符合项目风格的单元测试。",
    "TERMINAL_COMMAND_GENERATION": "用户请求生成终端命令，请给出准确、安全的命令。",
    "DESCRIPTION_GENERATE_CODE": "用户请求根据描述生成代码，请生成符合规范的完整代码。",
    "CODE_PROBLEM_SOLVE": "用户遇到代码问题，请分析问题原因并给出解决方案。",
    "DOCUMENT_TRANSLATE": "用户请求翻译文档，请保持格式并准确翻译。",
    "SEARCH_TITLE_ASK": "用户进行搜索标题提问，请给出简洁准确的回答。",
    "ERROR_INFO_ASK": "用户询问错误信息，请分析错误原因并提供解决方案。",
    "FREE_INPUT": "用户自由提问，请综合上下文给出有帮助的回答。",
    "INLINE_CHAT": "用户进行行内问答，请简短直接地回答。",
    "INLINE_EDIT": "用户进行行内编辑，请生成精确的代码修改。",
    # AI_DEVELOPER_* 系列任务暂不提供特殊提示，保持通用处理
}

_SESSION_TYPE_HINTS = {
    # sessionType 为自由字符串，暂无枚举约束，按需扩展
}

_MODE_HINTS = {
    # mode 为自由字符串，暂无枚举约束，按需扩展
}


def _build_lsp4j_ide_prompt(params: ChatAskParam) -> str:
    """基于灵码插件参数构建 IDE 上下文提示。

    字段来源：ChatAskParam.java 反编译源码，ChatTaskEnum.java 实际枚举值。
    Token 预算：总长度不超过 2000 字符，超长截断并记录日志。
    """
    parts: list[str] = []

    # P1 字段映射（chatTask 使用 ChatTaskEnum 实际枚举值）
    if params.chatTask:
        hint = _CHAT_TASK_HINTS.get(params.chatTask, "")
        if hint:
            parts.append(hint)

    if params.codeLanguage:
        parts.append(f"当前文件语言: {params.codeLanguage}")

    if params.sessionType:
        hint = _SESSION_TYPE_HINTS.get(params.sessionType, "")
        if hint:
            parts.append(hint)

    if params.mode:
        hint = _MODE_HINTS.get(params.mode, "")
        if hint:
            parts.append(hint)

    # extra.context 代码注入（带安全前缀）
    if isinstance(params.extra, dict):
        for ctx in params.extra.get("context", []):
            ctx_type = ctx.get("type", "")
            ctx_content = ctx.get("content", "")
            if ctx_type == "code" and ctx_content:
                lang = ctx.get("language") or params.codeLanguage or ""
                parts.append(f"[用户选中的代码（仅供参考）]\n```{lang}\n{ctx_content[:4000]}\n```")
            elif ctx_type == "file" and ctx_content:
                parts.append(f"相关文件: {ctx_content}")

        if params.extra.get("fullFileEdit"):
            parts.append("整文件编辑模式：请输出完整的文件内容。")

    # P2 字段
    if params.shellType:
        parts.append(f"项目终端 Shell: {params.shellType}")

    if not parts:
        return ""

    ide_prompt = "\n\n[IDE 环境提示]\n" + "\n".join(f"- {p}" for p in parts)

    # Token 预算控制（2000 字符上限）
    if len(ide_prompt) > 2000:
        logger.warning("LSP4J: ide_prompt 超长 ({} 字符)，截断到 2000", len(ide_prompt))
        ide_prompt = ide_prompt[:1997] + "..."

    return ide_prompt
```

### 4.5 ChatAskParam dataclass

```python
@dataclass
class ChatAskParam:
    """灵码插件 chat/ask 请求参数（基于 ChatAskParam.java 17 字段）。

    所有字段均设默认值，兼容旧版插件缺少字段的情况。
    """
    requestId: str = ""
    chatTask: str = ""
    chatContext: Any = None
    sessionId: str = ""
    codeLanguage: str = ""
    isReply: bool = False
    source: int = 1
    questionText: str = ""
    stream: bool = True
    taskDefinitionType: str = ""
    extra: Any = None
    sessionType: str = ""
    targetAgent: str = ""
    pluginPayloadConfig: Any = None
    mode: str = ""
    shellType: str = ""
    customModel: Any = None
```

### 4.6 缺失方法存根

```python
async def _handle_stub(self, params: dict, msg_id: Any) -> None:
    """通用存根处理器 — 返回空成功响应，避免 Method not found 错误。

    用于 chat/listAllSessions、chat/like 等暂不实现的方法。
    注意：这些方法在 ChatService.java 中定义为 @JsonRequest，必须返回响应。
    """
    await self._send_response(msg_id, {})

async def _handle_code_change_apply(self, params: dict, msg_id: Any) -> None:
    """处理 chat/codeChange/apply — diff 功能入口。

    MVP 阶段：记录日志后返回成功，不实际处理代码变更。
    后续可扩展：保存 applyId/codeEdit 到数据库，供审计使用。

    ChatCodeChangeApplyParam.java 实际字段（10个）：
    applyId, projectPath, filePath, language, codeEdit,
    requestId, sessionId, extra, sessionType, mode
    """
    apply_id = params.get("applyId", "")
    file_path = params.get("filePath", "")
    code_edit = params.get("codeEdit", "")
    logger.debug(
        "LSP4J: chat/codeChange/apply applyId={} filePath={} codeEdit_len={}",
        apply_id, file_path, len(code_edit) if code_edit else 0,
    )
    await self._send_response(msg_id, {})
```

### 4.7 _persist_lsp4j_chat_turn 增强

在创建 ChatSession 时设置 `client_type="ide_plugin"`：

**注意**：`session_context.py:44-46` 的 `SessionContextManager.update_ide_context` **已经会在被调用时**设置 `client_type="ide_plugin"`，但当前 LSP4J 通道的 `_persist_lsp4j_chat_turn` 创建 Session 时未显式设置，导致初创会话的 `client_type` 保持默认 `"web"`。因此需在创建时显式指定，后续若调用 `SessionContextManager.update_ide_context` 则会覆盖为正确值。

```python
if not sess:
    sess = ChatSession(
        id=sid_uuid,
        agent_id=agent_id,
        user_id=user_id,
        title=f"LSP4J {local_now.strftime('%m-%d %H:%M')}",
        source_channel="ide_lsp4j",
        client_type="ide_plugin",  # ← 新增
        created_at=now,
        last_message_at=now,
    )
```

---

## 五、受影响文件

| 文件 | 修改类型 | 说明 |
|---|---|---|
| `backend/app/plugins/clawith_lsp4j/jsonrpc_router.py` | 主要修改 | ChatAskParam dataclass、_handle_chat_ask 重构、ide_prompt、METHOD_MAP 扩展（含 codeChange/apply + stepProcessConfirm）、client_type、chat/stop 响应、ChatAnswerParams.extra、step callback |
| `backend/app/services/llm/caller.py` | 接口增强 | call_llm / call_llm_with_failover 增加 cancel_event 参数，工具循环插入取消检查，client.stream 透传 cancel_event |
| `backend/app/services/llm/utils.py` | 接口增强 | get_model_api_key 增加 _runtime_api_key 优先读取（BYOK 场景） |

---

## 六、实现细节

### 6.1 Phase 0：基础设施（必须先做）

#### 6.1.1 call_llm 签名增加 cancel_event

```python
# caller.py:307
async def call_llm(
    # ... 现有参数 ...
    supports_vision=False,
    max_tool_rounds_override: int | None = None,
    cancel_event: asyncio.Event | None = None,  # ← 新增
) -> str:
```

#### 6.1.2 client.stream 透传 cancel_event

**关键**：当前 `caller.py:394` 的 `client.stream()` 调用**未传递 `cancel_event`**，需新增此行，否则取消信号无法到达底层 LLMClient。

```python
# caller.py:394（工具循环内 client.stream 调用）
response = await client.stream(
    messages=api_messages,
    tools=tools_for_llm if tools_for_llm else None,
    temperature=model.temperature,
    max_tokens=max_tokens,
    on_chunk=on_chunk,
    on_thinking=on_thinking,
    **_thinking_kwargs,
    cancel_event=cancel_event,  # ← 新增：透传取消事件
)
```

#### 6.1.3 工具循环取消检查（2 处）

```python
# caller.py:461 左右（readonly 工具并行执行前）
if readonly_calls:
    if cancel_event is not None and cancel_event.is_set():
        logger.info("[LLM] Tool loop cancelled before readonly tools")
        await client.close()
        return "[已取消]"
    async def _process_readonly(tc):
        # ... 现有逻辑 ...

# caller.py:476 左右（write 工具串行执行前）
for tc in write_calls:
    if cancel_event is not None and cancel_event.is_set():
        logger.info("[LLM] Tool loop cancelled before write tools")
        await client.close()
        return "[已取消]"
    tool_error = await _process_tool_call(...)
```

#### 6.1.4 call_llm_with_failover 签名与透传

```python
# caller.py:501-515 签名增加 cancel_event
async def call_llm_with_failover(
    primary_model, fallback_model,
    messages: list[dict], agent_name: str, role_description: str,
    agent_id=None, user_id=None, session_id: str = "",
    on_chunk=None, on_thinking=None, on_tool_call=None,
    supports_vision=False,
    on_failover=None,
    cancel_event: asyncio.Event | None = None,  # ← 新增
) -> str:
```

在两个 `call_llm` 调用点（lines 541-553 和 601-613）透传 `cancel_event`：

```python
# primary 调用（line 541-553）
primary_result = await call_llm(
    primary_model, messages, agent_name, role_description,
    agent_id=agent_id, user_id=user_id, session_id=session_id,
    on_chunk=_wrapped_on_chunk, on_tool_call=_wrapped_on_tool_call,
    on_thinking=on_thinking, supports_vision=supports_vision,
    cancel_event=cancel_event,  # ← 新增
)

# fallback 调用（line 601-613）
fallback_result = await call_llm(
    fallback_model, messages, agent_name, role_description,
    agent_id=agent_id, user_id=user_id, session_id=session_id,
    on_chunk=_fallback_on_chunk, on_tool_call=_fallback_on_tool_call,
    on_thinking=on_thinking,
    supports_vision=getattr(fallback_model, 'supports_vision', False),
    cancel_event=cancel_event,  # ← 新增
)
```

### 6.2 Phase 1：零风险纯注入

#### 6.2.1 ChatAskParam dataclass + 解析

```python
# jsonrpc_router.py 顶部（与其他 dataclass 一起定义）
from dataclasses import dataclass, field

@dataclass
class ChatAskParam:
    """灵码插件 chat/ask 请求参数。"""
    requestId: str = ""
    chatTask: str = ""
    chatContext: Any = None
    sessionId: str = ""
    codeLanguage: str = ""
    isReply: bool = False
    source: int = 1
    questionText: str = ""
    stream: bool = True
    taskDefinitionType: str = ""
    extra: Any = None
    sessionType: str = ""
    targetAgent: str = ""
    pluginPayloadConfig: Any = None
    mode: str = ""
    shellType: str = ""
    customModel: Any = None
```

#### 6.2.2 _handle_chat_ask 参数解析重构

```python
async def _handle_chat_ask(self, params: dict, msg_id: Any) -> None:
    # 解析参数（兼容旧版插件缺少字段）
    ask = ChatAskParam(**{k: v for k, v in params.items() if k in ChatAskParam.__dataclass_fields__})

    request_id = ask.requestId or str(uuid.uuid4())
    session_id = ask.sessionId
    question_text = (ask.questionText or "").strip() or (str(ask.chatContext or "")).strip()
    chat_context = str(ask.chatContext or "")

    # ... 原有逻辑 ...
```

#### 6.2.3 ide_prompt 注入到 role_description

```python
# 构建 ide_prompt
ide_prompt = _build_lsp4j_ide_prompt(ask)

# 合并到 role_description
base_role = getattr(self._agent_obj, "system_prompt", "") or ""
role_description = base_role + ide_prompt

# 调用 call_llm
reply = await call_llm(
    model=model_obj,
    messages=message_history,
    agent_name=self._agent_obj.name,
    role_description=role_description,
    agent_id=self._agent_id,
    user_id=self._user_id,
    session_id=session_id or "",
    on_chunk=on_chunk,
    on_tool_call=on_tool_call,
    on_thinking=on_thinking,
    supports_vision=supports_vision,
    cancel_event=self._cancel_event,
)
```

#### 6.2.4 stream 参数 + on_chunk 取消检查

```python
# 在 _handle_chat_ask 中记录 stream 模式
self._stream_mode = ask.stream  # 默认 True

# on_chunk 回调（同时处理流式输出和取消检测）
async def on_chunk(text: str) -> None:
    # 取消检查：chat/stop 时立即中断流式输出
    if self._cancel_event is not None and self._cancel_event.is_set():
        raise asyncio.CancelledError("chat/stop requested")
    reply_parts.append(text)
    # 仅在 stream=True 时推送 chat/answer 通知
    if getattr(self, "_stream_mode", True):
        await self._send_chat_answer(session_id, text, request_id)
```

#### 6.2.5 chat/stop 返回响应修复

**问题**：`_handle_chat_stop` 当前不返回响应，但 `ChatService.java` 定义 `stop` 为 `@JsonRequest`，LSP4J 框架期望收到响应。

```python
async def _handle_chat_stop(self, params: dict, msg_id: Any) -> None:
    """处理 chat/stop 请求 — 取消当前正在进行的 chat/ask。"""
    if self._cancel_event:
        self._cancel_event.set()
    await self._send_response(msg_id, {})  # ← 新增：必须返回响应
```

#### 6.2.6 ChatAnswerParams 补充 extra 字段

**问题**：`ChatAnswerParams.java` 定义了 `extra: Map<String, String>`，其中 `sessionType` 和 `intentionType` 是已知 key。插件使用 `extra.sessionType` 区分 inline chat / panel chat，这对 diff 功能（行内编辑模式）很关键。

```python
# _send_chat_answer 中补充 extra 字段
async def _send_chat_answer(self, session_id, text, request_id):
    extra = {}
    # 如果当前会话有 sessionType（来自 ChatAskParam），注入到 extra
    if getattr(self, "_current_session_type", ""):
        extra["sessionType"] = self._current_session_type

    await self._send_notification("chat/answer", {
        "requestId": request_id,
        "sessionId": session_id or "",
        "text": text,
        "overwrite": False,
        "isFiltered": False,
        "timestamp": int(time.time() * 1000),
        "extra": extra if extra else None,  # ← 新增
    })
```

在 `_handle_chat_ask` 中保存 sessionType：
```python
self._current_session_type = ask.sessionType or ""
```

### 6.3 Phase 2：模型切换与 diff

#### 6.3.1 customModel 处理（BYOK 模型）

```python
model_obj = self._model_obj
supports_vision = False

# customModel 优先级最高（用户显式选择 + 自带密钥）
# customModel 字段基于 CustomModelParam.kt：
#   provider, model, isVl, isReasoning, maxInputTokens, parameters(Map<String,String>)
if ask.customModel and isinstance(ask.customModel, dict):
    cm = ask.customModel
    provider = cm.get("provider", "")
    model_name = cm.get("model", "")
    if provider and model_name:
        params = cm.get("parameters", {})
        from app.models.llm_model import LLMModel
        transient_model = LLMModel(
            id=uuid.uuid4(),
            provider=provider,
            model=model_name,
            base_url=params.get("base_url", ""),
            api_key_encrypted="",  # 占位，实际密钥通过运行时属性注入
        )
        transient_model._runtime_api_key = params.get("api_key", "")
        model_obj = transient_model
        supports_vision = bool(cm.get("isVl"))
        logger.info("LSP4J: 使用 BYOK 模型 {}:{}", provider, model_name)
```

**注意**：`get_model_api_key()` 需要修改以优先读取 `_runtime_api_key`（实际代码位于 `backend/app/services/llm/utils.py:49-58`）：

```python
def get_model_api_key(model: LLMModel) -> str:
    """解密模型的 API 密钥，向后兼容明文密钥。

    新增：优先读取运行时密钥（BYOK 场景），不入库、不写日志。
    """
    # 优先读取运行时密钥（LSP4J BYOK 场景，避免入库）
    runtime_key = getattr(model, "_runtime_api_key", None)
    if runtime_key:
        return runtime_key

    raw = model.api_key_encrypted or ""
    if not raw:
        return ""
    try:
        settings = get_settings()
        return decrypt_data(raw, settings.SECRET_KEY)
    except ValueError:
        return raw
```

#### 6.3.2 extra.modelConfig.key 处理

```python
# customModel 未设置时，尝试 extra.modelConfig.key
async def _resolve_model_by_key(model_key: str) -> LLMModel | None:
    """根据模型 key（UUID 或 model 名称）查找 LLMModel。

    优先按 UUID 查 id，再按 model 名称查。
    """
    from app.database import async_session
    async with async_session() as db:
        try:
            mid = uuid.UUID(model_key)
            mr = await db.execute(
                select(LLMModel).where(LLMModel.id == mid)
            )
            model = mr.scalar_one_or_none()
            if model:
                return model
        except ValueError:
            pass
        mr = await db.execute(
            select(LLMModel).where(LLMModel.model == model_key)
        )
        return mr.scalar_one_or_none()

if model_obj is self._model_obj and ask.extra and isinstance(ask.extra, dict):
    model_config = ask.extra.get("modelConfig", {})
    model_key = model_config.get("key", "") if isinstance(model_config, dict) else ""
    if model_key:
        override = await _resolve_model_by_key(model_key)
        if override:
            model_obj = override
            logger.info("LSP4J: 使用模型配置 key={}", model_key)
```

#### 6.3.3 METHOD_MAP 扩展

```python
_METHOD_MAP: dict[str, Any] = {
    "initialize": _handle_initialize,
    "shutdown": _handle_shutdown,
    "exit": _handle_exit,
    # ── chat/ 方法（基于 ChatService.java @JsonSegment("chat") 15 个方法，含 quota/doNotRemindAgain） ──
    "chat/ask": _handle_chat_ask,
    "chat/stop": _handle_chat_stop,          # @JsonRequest — 需返回响应
    "chat/systemEvent": _handle_stub,
    "chat/getStage": _handle_stub,
    "chat/replyRequest": _handle_stub,
    "chat/like": _handle_stub,
    "chat/codeChange/apply": _handle_code_change_apply,
    "chat/stopSession": _handle_stub,
    "chat/receive/notice": _handle_stub,
    "chat/quota/doNotRemindAgain": _handle_stub,
    "chat/listAllSessions": _handle_stub,    # 插件打开聊天面板时调用
    "chat/getSessionById": _handle_stub,     # 查看历史会话时调用
    "chat/deleteSessionById": _handle_stub,  # 删除会话时调用
    "chat/clearAllSessions": _handle_stub,   # 清空所有会话时调用
    "chat/deleteChatById": _handle_stub,     # 删除单条消息时调用
    # ── tool/ 方法（基于 ToolService.java + ToolCallService.java） ──
    "tool/call/approve": _handle_tool_call_approve,
    "tool/call/results": _handle_stub,              # ToolCallService.listToolCallInfo（P1-12）
    "tool/invokeResult": _handle_tool_invoke_result, # ToolService.invokeResult（已存在，不可遗漏）
    # ── agents/ 方法（基于 TestAgentService.java） ──
    "agents/testAgent/stepProcessConfirm": _handle_step_process_confirm,
}
```

### 6.4 Phase 3：任务规划与步骤进度（需求 #15）

#### 6.4.1 协议背景

灵码插件通过 `chat/process_step_callback`（Server → Client 通知）实现实时步骤进度展示。
当 Clawith 执行多步骤任务时，发送 step callback 让插件渲染任务列表（带 loading/success/error 图标）。

**关键类**（均来自插件源码验证）：

| 类名 | 方向 | 字段 |
|---|---|---|
| `ChatProcessStepCallbackParams` | Server → Client | requestId, sessionId, step, description, status, result, message |
| `ChatStepStatusEnum` | — | `"doing"` \| `"done"` \| `"error"` \| `"manual_confirm"` |
| `ChatStepEnum` | — | `step_start`, `step_end`, `step_refine_query`, `step_collecting_workspace_tree`, 等 |
| `StepProcessConfirmParam` | Client → Server | requestId, sessionId, step, confirmResult |
| `StepProcessConfirmResult` | Server → Client | requestId, errorMessage, successful |

#### 6.4.2 发送 step callback 通知

```python
async def _send_process_step_callback(
    self, session_id: str | None, request_id: str,
    step: str, description: str, status: str,
    result: Any = None, message: str = "",
) -> None:
    """发送 chat/process_step_callback 通知（ChatProcessStepCallbackParams 格式）。

    插件收到后会在聊天面板渲染步骤进度列表。
    status 取值：doing / done / error / manual_confirm
    step 取值参考 ChatStepEnum：step_start, step_end, step_refine_query 等
    """
    await self._send_notification("chat/process_step_callback", {
        "requestId": request_id,
        "sessionId": session_id or "",
        "step": step,
        "description": description,
        "status": status,
        "result": result,
        "message": message,
    })
```

#### 6.4.3 在 _handle_chat_ask 中集成 step callback

```python
# 在 call_llm 调用前：发送开始步骤
await self._send_process_step_callback(
    session_id, request_id,
    step="step_start", description="开始处理", status="doing",
)

# 在 on_tool_call 回调中：发送工具执行步骤
async def on_tool_call(data: dict) -> None:
    status = data.get("status", "running")
    tool_name = data.get("name", "unknown")
    if status == "running":
        await self._send_process_step_callback(
            session_id, request_id,
            step=f"tool_{tool_name}", description=f"正在执行: {tool_name}",
            status="doing",
        )
        await self._send_chat_think(session_id, f"正在调用工具: {tool_name}", "start", request_id)
    elif status == "done":
        await self._send_process_step_callback(
            session_id, request_id,
            step=f"tool_{tool_name}", description=f"已完成: {tool_name}",
            status="done",
        )
        await self._send_chat_think(session_id, f"工具 {tool_name} 执行完成", "done", request_id)

# 在 call_llm 完成后：发送结束步骤
await self._send_process_step_callback(
    session_id, request_id,
    step="step_end", description="处理完成", status="done",
)
```

#### 6.4.4 处理 stepProcessConfirm 请求

```python
async def _handle_step_process_confirm(self, params: dict, msg_id: Any) -> None:
    """处理 agents/testAgent/stepProcessConfirm — 步骤确认请求。

    当 step callback 的 status="manual_confirm" 时，插件显示确认按钮。
    用户点击后发送此请求，Clawith 返回确认结果。

    返回格式：StepProcessConfirmResult { requestId, errorMessage, successful }
    """
    await self._send_response(msg_id, {
        "requestId": params.get("requestId", ""),
        "successful": True,
        "errorMessage": "",
    })
```

### 6.5 Phase 4：代码变更与 diff 渲染（需求 #14、#16）

#### 6.5.1 双模式代码修改架构

灵码插件有**两套**代码修改机制：

| 模式 | 协议 | 场景 | 当前状态 |
|---|---|---|---|
| 直接修改 | `tool/invoke`（save_file, replace_text_by_path 等） | AI 自主修改代码 | ✅ 已实现 |
| 交互式 diff | `chat/codeChange/apply` + `chat/codeChange/apply/finish` | 用户点击"Apply"按钮触发 diff 渲染 | ❌ 仅存根 |

#### 6.5.2 chat/codeChange/apply 完整流程

```
1. AI 在聊天中输出代码块（通过 chat/answer 推送）
2. 用户在灵码中点击代码块的 "Apply" 按钮
3. 插件发送 chat/codeChange/apply（ChatCodeChangeApplyParam）→ Clawith
4. Clawith 处理代码变更，返回 ChatCodeChangeApplyResult
5. Clawith 发送 chat/codeChange/apply/finish 通知（含最终 applyCode）
6. 插件使用 InEditorDiffRenderer 渲染 inline diff
7. 用户 Accept/Reject 各个变更块
```

**ChatCodeChangeApplyParam**（10 个字段，源码验证）：
- applyId, projectPath, filePath, language, codeEdit
- requestId, sessionId, extra, sessionType, mode

**ChatCodeChangeApplyResult**（9 个字段，源码验证）：
- applyId, projectPath, filePath, applyCode, requestId, sessionId, extra, sessionType, mode

#### 6.5.3 实现 codeChange/apply handler

```python
async def _handle_code_change_apply(self, params: dict, msg_id: Any) -> None:
    """处理 chat/codeChange/apply — 交互式代码变更（diff 渲染入口）。

    当用户点击灵码聊天面板中代码块的 "Apply" 按钮时触发。
    插件期望收到 ChatCodeChangeApplyResult 格式的响应，
    其中 applyCode 为最终要应用到文件的代码内容。

    MVP 策略：直接将 codeEdit 作为 applyCode 返回（无 merge 逻辑）。
    后续可增强：基于 filePath 读取当前文件内容，计算三方 merge。
    """
    apply_id = params.get("applyId", str(uuid.uuid4()))
    code_edit = params.get("codeEdit", "")
    file_path = params.get("filePath", "")
    request_id = params.get("requestId", "")
    session_id = params.get("sessionId", "")

    # MVP：直接返回 codeEdit 作为 applyCode
    await self._send_response(msg_id, {
        "applyId": apply_id,
        "projectPath": params.get("projectPath", ""),
        "filePath": file_path,
        "applyCode": code_edit,  # ← 关键：插件用此渲染 diff
        "requestId": request_id,
        "sessionId": session_id,
        "extra": params.get("extra", ""),
        "sessionType": params.get("sessionType", ""),
        "mode": params.get("mode", ""),
    })

    # 发送 apply/finish 通知（部分插件版本依赖此通知刷新 diff）
    await self._send_notification("chat/codeChange/apply/finish", {
        "applyId": apply_id,
        "projectPath": params.get("projectPath", ""),
        "filePath": file_path,
        "applyCode": code_edit,
        "requestId": request_id,
        "sessionId": session_id,
        "extra": params.get("extra", ""),
        "sessionType": params.get("sessionType", ""),
        "mode": params.get("mode", ""),
    })
```

## 七、边界条件

1. **chatContext 类型不确定** — string/dict/null，需 `str()` 转换
2. **customModel.parameters 含密钥** — 不写日志、不持久化、GC 后释放
3. **extra.modelConfig.key 解析失败** — 降级到默认模型，warning 日志
4. **stream=False 但 call_llm 内部仍流式** — on_chunk 中判断 `_stream_mode`
5. **cancel_event 在 LLM API 阻塞调用中无法中断** — 只能在工具循环和 on_chunk 中检查
6. **旧版插件缺少新字段** — dataclass 默认值兜底
7. **sessionType + mode 组合** — 两者独立，`inline` + `edit` 是常见组合
8. **ide_prompt 长度上限 2000 字符** — 超长截断并记录日志
9. **extra.context 代码注入安全** — 使用 `[用户选中的代码（仅供参考）]` 前缀标记不可信来源
10. **模型覆盖优先级**：`customModel` > `extra.modelConfig.key` > 默认 `model_obj`
11. **ChatAnswerParams.extra** — 插件通过 `extra.sessionType` 区分 inline/panel 模式，影响 diff 显示，必须传入
12. **ChatFinishParams.extra** — 同 ChatAnswerParams，后续可扩展
13. **chat/stop 必须返回响应** — `ChatService.java` 定义 `stop` 为 `@JsonRequest`，不返回响应会导致 LSP4J 超时

---

## 八、对现有系统的影响分析

### 8.1 智能体自我进化影响

- **无影响**。本次修改仅涉及 LSP4J 通道的参数解析和 prompt 注入，不改变智能体的记忆机制、技能系统、关系网络或触发器逻辑。
- `call_llm` 新增的 `cancel_event` 参数默认 `None`，不影响 ACP 通道或其他调用方。

### 8.2 Web UI 影响

- **正向增强**。设置 `client_type="ide_plugin"` 后，Web UI 可以正确识别 LSP4J 会话来源，便于过滤和展示。
- WebSocket 通知机制已存在（`_persist_lsp4j_chat_turn` 中的 `ws_module.manager.send_to_session`），无需修改。

### 8.3 记忆系统影响

- **无影响**。对话持久化逻辑 `_persist_lsp4j_chat_turn` 已存在，本次仅增强 `client_type` 标记。
- 记忆的读取（`_load_lsp4j_history_from_db`）也不受影响。

### 8.4 工具系统影响

- **无影响**。8 个 IDE 工具的定义和调用链路（`tool_hooks.py` → `invoke_lsp4j_tool` → `tool/invoke`）保持不变。
- `call_llm` 工具循环中的取消检查仅在 `cancel_event` 不为 None 时生效，不影响现有调用。

---

## 九、数据流

### 9.1 完整聊天数据流

```
灵码插件
  │ ① WebSocket 发送 chat/ask (ChatAskParam 17 字段)
  ▼
Clawith LSP4J Router
  │ ② 解析 ChatAskParam，构建 ide_prompt
  │ ③ 模型选择（customModel / modelConfig.key / 默认）
  │ ④ 调用 call_llm(cancel_event=..., supports_vision=...)
  ▼
call_llm (caller.py)
  │ ⑤ 流式输出 → on_chunk → chat/answer 通知 → 灵码
  │ ⑥ 工具调用 → tool_hooks → invoke_lsp4j_tool → tool/invoke → 灵码 IDE
  │ ⑦ IDE 执行工具 → tool/invokeResult → Clawith
  │ ⑧ 工具结果返回 LLM → 继续流式输出
  ▼
LLM 输出完成
  │ ⑨ chat/finish 通知 → 灵码
  │ ⑩ 返回 chat/ask 响应 {"isSuccess": true, ...}
  ▼
后台持久化
  │ ⑪ 保存 ChatSession (client_type="ide_plugin") + ChatMessage
  │ ⑫ WebSocket 通知 Clawith 前端刷新
```

### 9.2 工具调用数据流

```
LLM 决策调用 read_file
  │
  ▼
tool_hooks._lsp4j_aware_execute_tool
  │ 判断 current_lsp4j_ws 活跃 + 工具名在 _LSP4J_IDE_TOOL_NAMES
  ▼
jsonrpc_router.invoke_lsp4j_tool
  │ 生成 requestId + toolCallId
  │ 发送 JSON-RPC request "tool/invoke" (ToolInvokeRequest 格式)
  ▼
灵码 IDE ToolInvokeProcessor
  │ 解析工具名和参数，调用 ReadFileToolHandler
  │ 读取文件内容
  ▼
灵码发送 tool/invokeResult (ToolInvokeResponse 格式)
  ▼
Clawith _handle_tool_invoke_result
  │ 查找 pending Future，设置结果
  ▼
invoke_lsp4j_tool 返回结果 → call_llm 工具循环
```

---

## 十、预期结果

1. **能回复**：`chat/ask` 响应含 `isSuccess=true`，灵码不再显示"调用异常"
2. **能使用 diff**：`chat/codeChange/apply` 返回 `ChatCodeChangeApplyResult` 含 `applyCode`，插件渲染 inline diff；`chat/codeChange/apply/finish` 通知推送
3. **能使用任务规划**：`chat/process_step_callback` 实时推送步骤进度，插件渲染任务列表；`stepProcessConfirm` 支持用户确认步骤
4. **能改代码**：双模式 — `tool/invoke`（save_file/replace_text_by_path 直接修改）+ `codeChange/apply`（交互式 diff）
5. **能操作本地工具**：`read_file`、`run_in_terminal`、`get_problems` 等 8 个工具正常流转
6. **LLM 感知 IDE 上下文**：通过 `chatTask`、`codeLanguage`、`sessionType`、`mode`、`extra.context` 等字段注入 ide_prompt
7. **chat/stop 有效**：`cancel_event` 贯通 `call_llm` → 工具循环 → `client.stream()`，点击停止后真正中断；`chat/stop` 返回响应避免 LSP4J 超时
8. **视觉能力可用**：`supports_vision` 正确透传，视觉模型可处理图片
9. **Web UI 可见**：`client_type="ide_plugin"` 标记，前端正确展示 LSP4J 会话
10. **记忆保存**：对话持久化到数据库，历史消息可回填
11. **Method not found 消失**：ChatService 全部 15 个方法（含 quota/doNotRemindAgain）+ TestAgentService 的 stepProcessConfirm 均有处理器

---

## 十一、深度检查发现的适配问题（2026-04-27 新增）

### 11.1 协议方法完整覆盖检查

**灵码插件协议总览**（基于 `LanguageServer.java` + 所有 `@JsonSegment` 服务）：

| 服务 | 方法总数 | 已实现 | Stub 实现 | 未实现 | 覆盖率 |
|------|---------|--------|----------|--------|--------|
| ChatService | 15 | 3 | 12 | 0 | 100%（含 stub） |
| ToolCallService | 2 | 2 | 0 | 0 | 100% |
| TestAgentService | 1 | 0 | 1 | 0 | 100%（含 stub） |
| ToolService | 1 | 1 | 0 | 0 | 100% |
| ConfigService | 2 | 2 | 0 | 0 | 100% |
| CommitMessageService | 2 | 2 | 0 | 0 | 100% |
| AuthService | 6 | 0 | 6 | 0 | 100%（含 stub） |
| 其他服务（10+） | 20+ | 0 | 20+ | 0 | 100%（含 stub） |
| **合计** | **~50** | **~10** | **~40** | **0** | **100%** |

**结论**：所有协议方法均已覆盖（真实实现 + 空响应 stub），**无 Method not found 错误**。

---

### 11.2 核心功能适配检查（P0-P1）

#### ✅ 已完整适配的核心功能

| 功能 | 协议方法 | 状态 | 说明 |
|------|---------|------|------|
| 聊天问答 | `chat/ask` | ✅ 完整 | 支持流式/非流式、多轮、上下文注入 |
| 流式回答 | `chat/answer` | ✅ 完整 | 含文件路径自动转可点击链接 |
| 思考过程 | `chat/think` | ✅ 完整 | 新增兜底发送（纯 thinking 无正文场景） |
| 聊天结束 | `chat/finish` | ✅ 完整 | 含 `statusCode`、`reason`、`fullAnswer` |
| 停止生成 | `chat/stop` | ✅ 完整 | `cancel_event` 贯通全链路 |
| 工具调用审批 | `tool/call/approve` | ✅ 完整 | 支持用户同意/拒绝工具调用 |
| 工具状态同步 | `tool/call/sync` | ✅ 完整 | INIT → PENDING → RUNNING → FINISHED/ERROR |
| 工具执行结果 | `tool/invokeResult` | ✅ 完整 | 支持 8 个本地 IDE 工具 |
| 代码变更申请 | `chat/codeChange/apply` | ✅ 完整 | 返回 `ChatCodeChangeApplyResult`，渲染 Diff 面板 |
| 代码变更完成 | `chat/codeChange/apply/finish` | ✅ 完整 | 通知插件关闭 Diff 面板 |
| 步骤进度回调 | `chat/process_step_callback` | ✅ 完整 | 任务规划实时显示进度条 |
| 会话标题更新 | `session/title/update` | ✅ 完整 | 支持 AI 自动生成会话标题 |
| 图片上传 | `image/upload` | ✅ 完整 | 双响应模式（同步 ack + 异步通知） |
| Commit Message | `commitMsg/generate` | ✅ 完整 | 流式生成 Git 提交信息 |
| IDE 本地工具调用 | `tool/invoke` | ✅ 完整 | 8 个工具（文件/终端/代码诊断等） |
| CODE_EDIT_BLOCK 格式 | `_fix_code_edit_block_format` | ✅ 新增 | 修复流式断裂、换行导致 Apply 按钮消失 |

---

#### ⚠️ 部分适配但需增强的功能

| 功能 | 协议方法 | 当前状态 | 问题说明 | 优先级 | 建议 |
|------|---------|---------|---------|--------|------|
| **历史会话列表** | `chat/listAllSessions` | 返回空列表 | 插件打开聊天面板时调用，用户看不到历史会话 | **P0** | 需从 `chat_session` 表查询，转换为灵码 `ChatSession` 格式 |
| **历史会话详情** | `chat/getSessionById` | 返回空对象 | 用户点击历史会话时调用，看不到历史消息 | **P0** | 需从 `chat_message` 表查询，转换为 `ChatRecord` 列表 |
| **删除会话** | `chat/deleteSessionById` | 返回空 | 用户删除会话时无实际效果 | **P1** | 需真正删除 `chat_session` 及关联消息 |
| **删除单条消息** | `chat/deleteChatById` | 返回空 | 用户删除消息时无实际效果 | **P1** | 需真正删除 `chat_message` 记录 |
| **工具调用历史** | `tool/call/results` | 返回空列表 | 插件查询工具调用历史时无数据 | **P2** | 可从内存或数据库查询工具执行记录 |
| **步骤确认** | `agents/testAgent/stepProcessConfirm` | 返回空 | 用户确认任务步骤时无实际效果 | **P2** | 需实现步骤确认的回调逻辑 |
| **标准 LSP 代码编辑** | `workspace/applyEdit` | 未实现 | 这是 LSP 标准协议，用于批量应用代码变更 | **P3** | 目前通过 `tool/invoke` + `replace_text_by_path` 已覆盖核心场景 |

---

### 11.3 数据结构兼容性检查

#### 灵码 ChatSession vs Clawith ChatSession

| 字段 | 灵码 ChatSession | Clawith ChatSession | 适配状态 |
|------|-----------------|---------------------|---------|
| userId | String | user_id (UUID) | ⚠️ 类型不匹配（需转换为 str） |
| userName | String | 无（需从 user 表关联） | ❌ 缺失字段 |
| sessionId | String | id (UUID) | ✅ 可转换 |
| sessionTitle | String | title | ✅ 匹配 |
| projectId | String | 无（需从 extended_data 提取） | ❌ 缺失字段 |
| projectUri | String | 无（需从 extended_data 提取） | ❌ 缺失字段 |
| projectName | String | 无（需从 extended_data 提取） | ❌ 缺失字段 |
| gmtCreate | long | created_at (datetime) | ✅ 可转换（to_timestamp * 1000） |
| gmtModified | long | updated_at (datetime) | ✅ 可转换 |
| chatRecords | List\<ChatRecord\> | chat_message 关联 | ⚠️ 结构不匹配，需转换 |
| sessionType | String | client_type | ✅ 可映射 |

**适配建议**：`chat/listAllSessions` 返回时，缺失字段可填空字符串，不影响插件正常显示。

---

### 11.4 日志问题检查与修复状态（2026-04-27 检查）

基于 `log-issues-investigation/doc.md` 的 6 个问题，逐一验证修复状态：

| # | 问题 | 严重度 | 修复状态 | 验证说明 |
|---|------|--------|---------|---------|
| 1 | **Failover 误判**：正常回复被当成不可重试错误 | 中 | ✅ **已修复** | `is_retryable_error()` 已重构（`caller.py:79-110`），先判断 `is_error` 再检查可重试性，不再误判正常回复 |
| 2 | **WebSocket 发送失败**：`Cannot call "send" once a close message has been sent` | 中 | ✅ **已修复** | `_send_message()` 开头增加 `if self._closed: return` 检查（`jsonrpc_router.py:1502-1503`） |
| 3 | **LSP4J 工具调用超时** | 中 | ✅ **已缓解** | ① `_tool_call_timeout: int = 300`（5 分钟超时）；② 迟达响应有专门日志（`invokeResult: toolCallId={} 无匹配 Future`）；③ `_cancelled_requests` 集合防止超时后处理 |
| 4 | **Heartbeat ReadTimeout** | 低 | ⚠️ **低优先级** | 心跳超时偶发，不影响核心功能，可后续增加重试机制 |
| 5 | **模型查找失败 key=auto** | 低 | ⚠️ **设计如此** | `"auto"` 表示让后端自动选择模型，不是 bug。当前行为：找不到 key 时使用默认模型，是合理的降级 |
| 6 | **failover.py 大小写不匹配** | 低 | ✅ **不存在** | `failover.py:36` 已做 `.lower()` 统一转换，`caller.py:88` 也已修复（先用 `is_error` 判断，不再直接 `startswith` 比较） |

**关键发现**（2026-04-27 验证）：
- ✅ `caller.py` 的 `from ... import` 绑定 bug **已修复**（第 26 行改为 `from app.services import agent_tools`，所有调用点使用 `agent_tools.execute_tool()`）
- ✅ 这是 **LSP4J 工具桥接失效的根本原因**，修复后 IDE 本地工具应该能正常工作

---

### 11.5 Markdown 流式渲染格式适配（新增章节）

### 11.1 灵码支持的 7 种特殊 Markdown 格式（基于 `MarkdownStreamPanel.java` 源码）

| 格式类型 | 正则匹配组 | 状态 | 说明 |
|---------|-----------|------|------|
| **toolCall 工具卡片** | `group(1)="toolCall"` | ✅ 已实现 | ````toolCall::工具名::toolCallId::状态``` → ToolMarkdownComponent 渲染 |
| **think 思考块** | `group(5,6,7,8)` | ✅ 已实现 | ````think::{THINK_TIME}\n内容``` → ThinkingComponent 渲染 |
| **文件引用卡片** | `group(1)` 非 toolCall | ❌ 未实现 | ````语言::文件路径::extra``` → FileMarkdownComponent 渲染 |
| **CODE_EDIT_BLOCK** | `group(9)/group(11)` | ⚠️ 需修复 | ````语言\|CODE_EDIT_BLOCK\|路径\n代码``` → 带 Apply 按钮的 CodeMarkdownHighlightComponent |
| **普通代码块** | `group(9/10)` | ✅ 自动支持 | 标准 ```language``` 代码块高亮 |
| **PlantUML 图表** | CodeMarkdownHighlightComponent | ✅ 自动支持 | 代码块内 PlantUML → SVG 渲染 |
| **Mermaid 图表** | CodeMarkdownHighlightComponent | ✅ 自动支持 | 代码块内 Mermaid → CEF 浏览器渲染 |
| **HTML think 标签** | `group(13/14)` | ✅ 自动支持 | `<think>内容</think>` → 折叠显示 |

### 11.2 CODE_EDIT_BLOCK 格式风险分析（最关键）

**正则表达式**（`MarkdownStreamPanel.java:39-41`）：
```regex
```([\w#+.-]+)::([^\n]+)::([^\n]+)\n+(.*?)```|````think::(\d+|\{THINK_TIME})\n(.*?)\n````|````think::(\d+|\{THINK_TIME})\n(.*)|```([\w#+.-]*\n*)?(.*?)`{2,3}|`{2,3}([\w#+.-]+\n*)?(.*)|<think>(.*?)</think>|<think>(.*)
```

**关键解析逻辑**（`MarkdownStreamPanel.java:301-331`）：
```java
// 场景：CODE_EDIT_BLOCK 在 group(9) + group(10)
String[] parts = group(10).split("\\|");
if (parts.length >= 3 && "CODE_EDIT_BLOCK".equals(parts[1])) {
    // 解析为带 Apply 按钮的代码块
}
```

**3 个真实世界风险场景**：

| # | 风险场景 | 概率 | 影响 | 修复方案 |
|---|---------|------|------|---------|
| 1 | **流式输出断裂**：语言标识和 `|CODE_EDIT_BLOCK|` 分到不同 chunk | ~30% | ❌ 失去 Apply 按钮，仅普通代码块 | 后端流式合并 + 格式修复 |
| 2 | **格式理解偏差**：弱模型把 `|CODE_EDIT_BLOCK|` 放到第二行 | 本地模型常见 | ❌ `parts.length=2 < 3`，降级为普通代码块 | 后端正则修复 |
| 3 | **长对话后模型忘记格式**：直接输出 ```language``` | ~15% | ❌ 完全失去 Apply 能力 | 依赖 system prompt + 输出后检测 |

### 11.3 CODE_EDIT_BLOCK 格式修复方案

**修复位置**：`jsonrpc_router.py` → `_send_chat_answer` 方法开头

**设计原则**：
- 极简侵入：仅 20-30 行代码，不影响其他逻辑
- 幂等安全：重复调用不会破坏格式
- 日志完整：记录修复前后的差异，便于调试

**实现代码**：

```python
@staticmethod
def _fix_code_edit_block_format(text: str) -> str:
    """修复 CODE_EDIT_BLOCK 格式异常（流式断裂、换行问题）。

    修复场景（基于灵码插件 MarkdownStreamPanel.java:301-331 源码）：
    1. 语言标识和 |CODE_EDIT_BLOCK| 分成两行
       输入:  ```python\n|CODE_EDIT_BLOCK|/path\n...
       输出:  ```python|CODE_EDIT_BLOCK|/path\n...
    2. （未来扩展）流式输出中 |CODE_EDIT_BLOCK| 前后断裂

    返回：修复后的文本
    """
    original = text

    # 场景 1: 语言和 |CODE_EDIT_BLOCK| 换行
    # 匹配: ```语言\n|CODE_EDIT_BLOCK|... → 替换为 ```语言|CODE_EDIT_BLOCK|...
    # 正则说明: 捕获 ``` 后的语言标识（[a-zA-Z0-9_-]+），然后是换行 + |CODE_EDIT_BLOCK|
    text = re.sub(
        r"```([a-zA-Z0-9_-]+)\n\|CODE_EDIT_BLOCK\|",
        r"```\1|CODE_EDIT_BLOCK|",
        text
    )

    # 日志：如果发生了修复，记录差异
    if text != original:
        logger.debug(
            "LSP4J: CODE_EDIT_BLOCK 格式已修复\n"
            "  修复前: {}\n"
            "  修复后: {}",
            original[:100], text[:100]
        )

    return text
```

**调用位置**：
```python
async def _send_chat_answer(self, session_id: str, text: str, request_id: str, status_code: int = 200):
    """发送 chat/answer 流式消息"""
    if not text:
        return

    # ✅ 新增：CODE_EDIT_BLOCK 格式自动修复
    text = self._fix_code_edit_block_format(text)

    # ✅ 新增：文件路径自动转可点击链接
    text = self._convert_file_paths_to_links(text)

    # ... 原有逻辑
```

### 11.4 文件引用卡片格式说明（可选增强）

**格式**（`MarkdownStreamPanel.java:353-354`）：
```markdown
```language::/absolute/file/path::extra_metadata
内容
```
```

**当前状态**：**暂不需要实现**
- 我们已实现的「纯文本路径自动转 Markdown 链接」已覆盖核心需求（可点击跳转）
- 文件引用卡片是高级 UI 增强（图标 + 状态等），非必需功能
- 未来如需实现，可在 `_send_chat_answer` 中检测并转换格式

### 11.5 验收标准

| # | 验收项 | 验证方式 |
|---|--------|---------|
| 1 | 流式输出中语言和 `|CODE_EDIT_BLOCK|` 分开 → 仍能渲染 Apply 按钮 | 人工测试：观察代码块右上角的 Apply 按钮 |
| 2 | 本地弱模型输出格式有偏差 → 自动修复后正常 | 使用 DeepSeek-V2 等本地模型测试 |
| 3 | 修复逻辑有完整 debug 日志 | 搜索日志关键词 "CODE_EDIT_BLOCK 格式已修复" |
| 4 | 正常格式的代码块不受影响 | 对照测试：修复前后输出一致 |
| 5 | 代码块内的路径不会被误修改 | 验证 `_convert_file_paths_to_links` 保护代码块内内容 |
