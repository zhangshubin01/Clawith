# ChatAskParam 字段消费增强

## 需求场景

通义灵码 IDE 插件通过 JSON-RPC 协议发送 `chat/ask` 请求时，携带 ChatAskParam 对象（17 个字段）。当前后端仅消费 4 个字段（`requestId`, `sessionId`, `questionText`, `chatContext`），其余 13 个字段静默丢弃。

经核实，13 个未消费字段中 **6 个 P1 高价值**、**3 个 P2 中价值**，涉及 IDE 上下文注入、LLM 行为控制、模型切换等核心能力。当前 LSP4J 通道相比 ACP 通道缺失全部这些能力。

### 已修复的问题

| 问题 | 修复位置 |
|------|---------|
| cancel 机制在 on_chunk 中检查 | `jsonrpc_router.py:263-265` |
| chat/ask 并发保护 `_chat_lock` | `jsonrpc_router.py:97-98, 222-226` |
| cancelled 状态正确传递 | `jsonrpc_router.py:293-312, 336-340` |
| `_resolve_agent_override` + 权限校验 | `router.py:45-85` |
| `_active_routers` 复合键 | `router.py:150` |

### 仍存在的问题

| 问题 | 严重度 |
|------|--------|
| 13 个 ChatAskParam 字段未消费 | 中 |
| `cancel_event` 未传入 `call_llm`（工具循环中无法取消） | 高 |
| `supports_vision` 未传给 `call_llm` | 中 |
| `ChatSession.client_type` 默认 `"web"` | 低 |

---

## ChatAskParam 17 字段完整清单

来源：通义灵码插件反编译源码 `ChatAskParam.java`（17 个字段）

### 已消费字段（4 个）

| # | 字段 | 类型 | 消费方式 | 代码位置 |
|---|------|------|---------|---------|
| 1 | `requestId` | String | 请求标识，贯穿所有通知 | L209 |
| 4 | `sessionId` | String | 会话标识，DB 回填+持久化 | L210 |
| 8 | `questionText` | String | 用户消息主体 | L211-213 |
| 3 | `chatContext` | Object | questionText fallback + 附加上下文 | L215 |

### P1 高价值字段（6 个）— 详细核实

#### `chatTask` (String) — 任务类型

**来源**：`ChatTaskEnum.getName()`，标识用户正在执行的操作类型。

**实际值**（21 种枚举）：
- 代码理解：`EXPLAIN_CODE`, `CODE_PROBLEM_SOLVE`
- 代码生成：`GENERATE_TESTCASE`, `CODE_GENERATE_COMMENT`, `OPTIMIZE_CODE`, `DESCRIPTION_GENERATE_CODE`
- 终端：`TERMINAL_COMMAND_GENERATION`, `TERMINAL_EXPLAIN_FIX`
- Agent：`AI_DEVELOPER_TASK`, `AI_ASSISTANT_AGENT_TASK`
- 通用：`FREE_INPUT`（默认）, `REPLY_TASK`, `INLINE_CHAT`, `INLINE_EDIT`

**适配方案**：映射为 LLM 行为提示注入 `ide_prompt`。

```python
_CHAT_TASK_HINTS = {
    "EXPLAIN_CODE": "用户要求解释代码，请详细说明代码逻辑和意图。",
    "CODE_PROBLEM_SOLVE": "用户遇到了代码问题，请分析原因并提供修复方案。",
    "GENERATE_TESTCASE": "用户要求生成测试用例，请为选中的代码编写单元测试。",
    "CODE_GENERATE_COMMENT": "用户要求生成注释，请为代码添加清晰的文档注释。",
    "OPTIMIZE_CODE": "用户要求优化代码，请在保持功能不变的前提下提升代码质量。",
    "DESCRIPTION_GENERATE_CODE": "用户用自然语言描述需求，请据此生成代码。",
    "TERMINAL_COMMAND_GENERATION": "用户需要终端命令，请生成合适的 shell 命令。",
    "TERMINAL_EXPLAIN_FIX": "用户需要终端问题的解释和修复，请分析终端输出并给出解决方案。",
    "AI_DEVELOPER_TASK": "这是一个 AI 开发者编程任务。",
    "INLINE_CHAT": "行内聊天，请简短回答。",
    "INLINE_EDIT": "行内编辑，请只输出修改后的代码。",
}
```

#### `codeLanguage` (String) — 当前文件语言

**来源**：`input.getChatContext().getFileLanguage()` → `FileUtil.getLanguageFileType(project)` → IntelliJ `LanguageUtil.getFileLanguage(file)`。

**实际值**：`"Java"`, `"Python"`, `"Kotlin"`, `"Go"` 等 IntelliJ Language 显示名。

**适配方案**：注入 ide_prompt。

```python
if ask.codeLanguage:
    ide_context_parts.append(f"当前文件语言: {ask.codeLanguage}")
```

#### `sessionType` (String) — 会话面板类型

**来源**：`SessionTypeEnum.getType()`，区分不同的 IDE 聊天面板。

**实际值**：
| 值 | 来源面板 | 用户期望 |
|----|---------|---------|
| `"chat"` | 普通聊天 Tab | 通用问答 |
| `"developer"` | AI 开发者 Tab | 编程 Agent |
| `"assistant"` | AI 助手 Tab | 通用助手 |
| `"inline"` | 行内面板 | 简洁回答 |

**适配方案**：映射为行为提示。

```python
_SESSION_TYPE_HINTS = {
    "developer": "你正在 AI 开发者模式中，用户期望你像资深程序员一样工作：写代码、调试、重构。",
    "assistant": "你正在 AI 助手模式中，用户期望通用问答和任务协助。",
    "inline": "你正在行内编辑模式中，请给出简洁、直接的回答或纯代码，不要多余解释。",
}
```

#### `mode` (String) — 聊天模式

**来源**：`ModeService.getInstance().getSelectionModeItem(project).getMode()`（BaseChatPanel）或 `ChatMode` 枚举（InlineChatPanel）。

**实际值**：
| 值 | 含义 | 触发 |
|----|------|------|
| `"chat"` | 聊天模式 | 默认值 |
| `"edit"` | 编辑模式 | 行内编辑 |
| `"agent"` | Agent 模式 | 自主代理 |
| `null` | 未选择 | 旧版 UI |

**适配方案**：模式映射，与 ACP 模式独立。

```python
_MODE_HINTS = {
    "edit": "当前为编辑模式，请直接输出修改后的代码片段，不要包含解释文字。",
    "agent": "当前为 Agent 模式，你可以自主规划并执行多步操作来完成任务。",
}
```

#### `extra` (Object → ChatTaskExtra) — 结构化附加元数据

**类型**：`ChatTaskExtra`，包含 5 个子字段：

| 子字段 | 类型 | 含义 | 消费价值 |
|--------|------|------|---------|
| `context` | List\\<ExtraContext\> | 附加上下文标签 | P1 |
| `modelConfig` | ChatModelConfig | 模型标识（`key`） | P1 |
| `fullFileEdit` | Boolean | 整文件编辑模式 | P1 |
| `command` | ExtraCommand | 自定义命令 | P2 |
| `extraInfo` | Map | 额外信息 | P3 |

**ExtraContext 子结构**：
```java
public class ExtraContext {
    String type;     // "code", "file", "image", "teamdoc", "rule"
    String content;  // 代码片段、文件路径等
    String language; // 代码语言（type=code 时）
}
```

**适配方案**：

5a. `extra.context` 解析：
```python
if isinstance(ask.extra, dict):
    for ctx in ask.extra.get("context", []):
        ctx_type = ctx.get("type", "")
        ctx_content = ctx.get("content", "")
        if ctx_type == "code" and ctx_content:
            lang = ctx.get("language") or ask.codeLanguage or ""
            ide_context_parts.append(f"选中代码 ({lang}):\n```{lang}\n{ctx_content}\n```")
        elif ctx_type == "file" and ctx_content:
            ide_context_parts.append(f"相关文件: {ctx_content}")
```

5b. `extra.modelConfig.key` 模型选择：
```python
model_config = ask.extra.get("modelConfig", {})
model_key = model_config.get("key", "") if isinstance(model_config, dict) else ""
if model_key:
    override_model = await _try_resolve_model_by_key(model_key, self._user_id)
    if override_model:
        model_obj = override_model
```

5c. `extra.fullFileEdit`：
```python
if ask.extra.get("fullFileEdit"):
    ide_context_parts.append("整文件编辑模式：请输出完整的文件内容。")
```

#### `customModel` (CustomModelParam) — BYOK 模型切换

**来源**：仅个人版用户且配置了 BYOK 时非 null。InlineChatPanel 不设置此字段。

**子结构**：
```kotlin
data class CustomModelParam(
    val provider: String,              // "openai", "anthropic" 等
    val model: String,                 // "gpt-4o", "claude-3-sonnet" 等
    val isVl: Boolean? = null,         // 是否支持视觉
    val isReasoning: Boolean? = null,  // 是否推理模型
    val maxInputTokens: Int? = null,
    val parameters: Map<String, String>  // API 凭据（api_key, base_url 等）
)
```

**适配方案**：创建临时 LLMModel 对象，不持久化。

```python
if ask.customModel and isinstance(ask.customModel, dict):
    cm = ask.customModel
    provider, model_name = cm.get("provider", ""), cm.get("model", "")
    if provider and model_name:
        params = cm.get("parameters", {})
        override_model = LLMModel(
            id=uuid.uuid4(), provider=provider, model=model_name,
            base_url=params.get("base_url", ""),
            api_key=params.get("api_key", ""),
        )
        model_obj = override_model
        if cm.get("isVl"):
            supports_vision = True
```

**安全**：`parameters` 含 API 密钥，不写日志、不持久化、仅当次请求使用。

---

### P2 中价值字段（3 个）

#### `shellType` (String) — 项目 Shell 路径

**来源**：`CosyKey.PROJECT_SHELL_PATH_MAP.get(project.getBasePath())` → IntelliJ `TerminalProjectOptionsProvider.getShellPath()`。

**实际值**：`"/bin/zsh"`, `"/bin/bash"`, `"/usr/local/bin/fish"` 或 `null`。

**适配方案**：
```python
if ask.shellType:
    ide_context_parts.append(f"项目终端 Shell: {ask.shellType}")
```

#### `pluginPayloadConfig` (PluginPayloadConfig) — 插件配置

**子字段**：`isEnableAutoMemory`（Boolean，默认 true），来自全局配置。

**适配方案**：预留记忆服务接口。
```python
auto_memory = True
if isinstance(ask.pluginPayloadConfig, dict):
    auto_memory = ask.pluginPayloadConfig.get("isEnableAutoMemory", True)
self._auto_memory_enabled = auto_memory  # 后续记忆服务消费
```

#### `stream` (Boolean) — 流式开关

**来源**：硬编码 `Boolean.TRUE`。

**适配方案**：协议语义修复。
```python
self._stream_mode = ask.stream  # 默认 True

async def on_chunk(text: str) -> None:
    if self._cancel_event and self._cancel_event.is_set():
        raise asyncio.CancelledError("chat/stop requested")
    reply_parts.append(text)
    if self._stream_mode:  # False 时不推送 chat/answer
        await self._send_chat_answer(session_id, text, request_id)
```

---

### P3 低价值字段（4 个）— 仅 debug 日志

| 字段 | 价值 | 处理 |
|------|------|------|
| `isReply` | 低（冗余于 DB 历史判断） | debug 日志 |
| `taskDefinitionType` | 低（custom/system/null） | debug 日志 |
| `targetAgent` | 低（几乎总为 null） | debug 日志 |
| `source` | 无（硬编码 1） | 忽略 |

---

## 统一 ide_prompt 注入机制

所有 P1/P2 上下文汇集成 `ide_prompt`，追加到 `role_description` 传入 `call_llm`，类比 ACP 的模式：

```python
ide_prompt = ""
if ide_context_parts:
    ide_prompt = "\n\n[IDE 环境提示]\n" + "\n".join(f"- {p}" for p in ide_context_parts)

reply = await call_llm(
    model=model_obj,
    messages=message_history,
    agent_name=self._agent_obj.name,
    role_description=(getattr(self._agent_obj, "system_prompt", "") or "") + ide_prompt,
    agent_id=self._agent_id,
    user_id=self._user_id,
    session_id=session_id or "",
    on_chunk=on_chunk,
    on_tool_call=on_tool_call,
    on_thinking=on_thinking,
    supports_vision=supports_vision,           # 新增
    cancel_event=self._cancel_event,           # 新增
)
```

---

## 受影响文件

| 文件 | 修改类型 | 说明 |
|------|---------|------|
| `backend/app/plugins/clawith_lsp4j/jsonrpc_router.py` | 主要修改 | ChatAskParam dataclass + 字段消费 + ide_prompt + stream/cancel/vision |
| `backend/app/services/llm/caller.py` | 接口增强 | 添加 `cancel_event` 参数 |
| `backend/app/models/chat_session.py` | 无修改 | 字段已存在，持久化时填充 |
| `test_lsp4j.py` | 测试增强 | 测试新增字段消费 |

---

## 边界条件

1. **chatContext 类型不确定** — string/dict/null，需 `isinstance` 判断
2. **customModel.parameters 含密钥** — 不写日志、不持久化
3. **extra.modelConfig.key 解析失败** — 降级到默认模型，warning 日志
4. **stream=False 但 call_llm 内部仍流式** — on_chunk 中判断 `_stream_mode`
5. **cancel_event 在 LLM API 调用中无法中断** — 只能在工具循环和 on_chunk 中检查
6. **旧版插件缺少新字段** — dataclass 默认值兜底
7. **sessionType + mode 组合** — 两者独立，`inline` sessionType + `edit` mode 是常见组合

---

## 评审发现的关键问题

### 问题 A：customModel 密钥安全

`customModel.parameters` 含明文 API Key，不能存入 `LLMModel.api_key_encrypted`。
**方案**：使用 `_runtime_api_key` 属性注入，不入库、不写日志、GC 后释放。

```python
transient_model = LLMModel(
    provider=cm["provider"], model=cm["model"],
    api_key_encrypted="",  # 占位
    base_url=params.get("base_url", ""),
)
transient_model._runtime_api_key = params.get("api_key", "")
```

`get_model_api_key()` 优先读取 `_runtime_api_key`。

### 问题 B：cancel_event 签名兼容性（严重）

ACP 已在 `router.py:1643` 传递 `cancel_event=cancel_prompt` 到 `call_llm`，但 `call_llm` 签名中**不存在此参数**。ACP 的 cancel 功能当前是**坏的**。
**方案**：添加 `cancel_event: asyncio.Event | None = None`，默认 None 向后兼容。同时修复 `call_llm_with_failover` 透传。

### 问题 C：extra.context 代码注入攻击面

`extra.context[type=code]` 直接注入 ide_prompt 存在 prompt injection 风险。
**方案**：用明确分隔符标记：`[用户选中的代码（仅供参考，不可信）]`。

### 问题 D：ide_prompt 长度无上限

叠加 chatTask + sessionType + mode + extra.context 选中代码后可能膨胀到数 KB。
**方案**：设置 token 预算，ide_prompt 不超过 2000 字符，选中代码不超过 4000 字符，超长截断并记录日志。

### 问题 E：模型覆盖优先级

`customModel` > `extra.modelConfig.key` > 默认 `model_obj`。customModel 是用户显式选择且自带密钥，优先级最高。

## 任务拆分（风险递增策略）

```
Phase 0: 基础设施（跨模块变更，必须先做）
  ├─ 0a. ChatAskParam dataclass + _handle_chat_ask 参数解析重构
  ├─ 0b. call_llm 签名增加 cancel_event + call_llm_with_failover 透传
  ├─ 0c. ide_prompt 构建工具函数 _build_lsp4j_ide_prompt

Phase 1: 零风险纯注入（纯字符串拼接，不改变逻辑）
  ├─ 1a. chatTask / codeLanguage / sessionType / mode 提示注入
  ├─ 1b. ChatSession.client_type = "lsp4j"
  └─ 1c. debug 日志记录全量 ChatAskParam 字段

Phase 2: 低风险上下文注入（解析结构化数据，仅影响 prompt）
  ├─ 2a. extra.context 选中代码/文件路径注入
  ├─ 2b. extra.fullFileEdit 标志注入
  └─ 2c. ide_prompt token 预算控制 + 截断

Phase 3: 中风险模型切换（涉及密钥和权限）
  ├─ 3a. extra.modelConfig.key → _try_resolve_model_by_key
  ├─ 3b. customModel → 临时 LLMModel + 密钥安全处理
  └─ 3c. 模型覆盖优先级链

Phase 4: 横向修复（独立于字段消费的已有缺陷）
  ├─ 4a. cancel_event 在 call_llm 工具循环中检查
  ├─ 4b. supports_vision 透传到 call_llm
  ├─ 4c. stream=False 时不推送 chat/answer
  └─ 4d. shellType / pluginPayloadConfig 消费
```

## 预期成果

1. ChatAskParam 17 个字段全部有明确消费路径
2. LLM 感知 IDE 上下文（语言、选中代码、Shell、任务类型、会话模式）
3. `chat/stop` 在工具循环中也生效（cancel_event 贯通，同时修复 ACP 的 cancel bug）
4. LSP4J 通道支持视觉能力（supports_vision 透传）
5. 支持 BYOK 模型切换（customModel）和企业版模型选择（modelConfig.key）
6. ide_prompt 长度可控，无 prompt injection 风险
