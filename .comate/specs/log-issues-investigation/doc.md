# 运行日志问题调研报告

## 概述

基于 2026-04-26 运行日志，排查发现 6 类问题。按严重度排序：

| # | 问题 | 严重度 | 根因类型 | 影响范围 |
|---|------|--------|----------|----------|
| 1 | Failover 误判 | 中 | 逻辑缺陷 | 所有 LLM 调用 |
| 2 | LSP4J WebSocket 发送失败 | 中 | 竞态条件 | IDE 插件用户 |
| 3 | LSP4J 工具调用超时 | 中 | 超时+竞态 | IDE 插件工具调用 |
| 4 | Heartbeat ReadTimeout | 低 | 超时+无重试 | 心跳功能 |
| 5 | LSP4J 模型查找失败 key=auto | 低 | 语义缺失 | IDE 插件模型选择 |
| 6 | failover.py 大小写不匹配 | 低 | Bug | 错误分类 |

---

## 问题 1: Failover 误判 — 正常回复被当成错误

### 现象
```
WARNING [Failover] Canceled: Primary model returned a non-retryable error: 我是 **WL4**，ListoCredi Android 贷款项目的首席工程专家。
```
LLM 正常回复的内容被当作 "non-retryable error" 打出 WARNING。

### 根因
`caller.py:578` 的逻辑缺陷：`call_llm_with_failover` 无法区分「成功回复」和「不可重试错误」。

**调用链：**
1. `call_llm()` 成功返回纯文本（如 `"我是 WL4..."`)，不加任何前缀
2. `call_llm_with_failover()` 调用 `is_retryable_error(result)` 判断
3. `is_retryable_error()` (`caller.py:79-92`) 对非错误前缀的字符串直接返回 `False`
4. `caller.py:578`：`if not is_retryable_error(result):` → `if not False:` → `True`
5. 进入分支，打出 WARNING 日志，但实际返回正确结果

**关键代码 (`caller.py:79-92`)：**
```python
def is_retryable_error(result: str) -> bool:
    result_lower = result.lower()
    if any(code in result_lower for code in ["429", "500", "502", "503", "504", "rate limit", "too many requests"]):
        return True
    # ↓ 正常回复不走这里，但返回 False 和"不可重试错误"含义相同
    if not (result.startswith("[LLM Error]") or result.startswith("[LLM call error]") or result.startswith("[Error]")):
        return False  # ← BUG: 正常回复和不可重试错误都返回 False
    return classify_error(Exception(result)) != FailoverErrorType.NON_RETRYABLE
```

**影响：** 功能不受影响（结果正确返回），但日志误导性强，可能掩盖真实错误。

### 附带发现：`caller.py` 大小写敏感风险（failover.py 无此问题）

**✅ `failover.py:75` — 正确，无 Bug**：
```python
error_msg = str(error).lower()  # line36: 已统一转为小写
if error_msg.startswith("[llm error]") or ...:  # line75: 小写比较，正确匹配
```
`failover.py:36` 已做 `.lower()` 统一转换，不存在大小写不匹配问题。

**⚠️ `caller.py:89` — 存在大小写敏感风险**：
```python
if not (result.startswith("[LLM Error]") or result.startswith("[LLM call error]") or result.startswith("[Error]")):
    return False
```
这里使用的是 `result.startswith()` (**大小写敏感**)，而不是 `result_lower.startswith()`。如果有任何代码生成小写前缀 `"[llm error]"`，将不会被识别为错误。

**风险等级**：低（当前代码生成的前缀都是大写）
**修复建议**：统一改为 `result_lower.startswith()` 比较。

### 附带发现：HTTP 状态码误匹配
`is_retryable_error` 中 `"429" in result_lower` 可能误匹配正常回复中包含的数字（如讨论定价时提到 "429元"）。

---

## 问题 2: LSP4J WebSocket 发送失败

### 现象
```
ERROR LSP4J: WebSocket 发送失败: Cannot call "send" once a close message has been sent.
```
15:14 时段集中出现 ~30 条。

### 根因
**竞态条件：** LLM 流式回调与 WebSocket 断连之间的竞争。

**时序：**
```
T1: chat/ask 到达 → call_llm 开始流式输出（on_chunk 回调持续调用）
T2: LLM 正在流式输出 → on_chunk → _send_chat_answer → _send_message → ws.send_text()
T3: 客户端断连（IDE 关闭/网络中断）
T4: 消息循环捕获 WebSocketDisconnect → cleanup() 执行
T5: 但 call_llm 仍在运行！on_chunk 再次触发 → ws.send_text() → 报错
```

**关键代码 (`jsonrpc_router.py:867-884`)：**
```python
async def _send_message(self, message: dict) -> None:
    try:
        frame = LSPBaseProtocolParser.format_message(message)
        await self._ws.send_text(frame)  # ← 无任何连接状态检查
    except Exception as e:
        logger.error("LSP4J: WebSocket 发送失败: {}", e)  # ← 仅日志，吞掉异常
```

**缺失：**
- 无 `_closed` / `_is_connected` 状态标记
- `cleanup()` 解析了 pending Futures 但**未取消正在运行的 call_llm 协程**
- `_handle_exit()` / `_handle_shutdown()` 什么都不做，不设标记
- 对比 ACP 插件有 `_closed` 标记（但也没在 `send_message` 中检查）

**影响：** 仅产生 ERROR 日志，不影响功能（消息发不出只是被 catch 住）。但大量 ERROR 日志影响问题排查。

---

## 问题 3: LSP4J 工具调用超时

### 现象
```
WARNING LSP4J: 工具调用超时 tool=read_file callId=xxx
WARNING LSP4J: 工具调用超时 tool=run_in_terminal callId=xxx
WARNING [LSP4J-TOOL] 收到未匹配的响应: id=1
```
14:39-15:23 时段出现 8+ 次超时，超时值 120s。

### 根因
**两层问题：超时本身 + 超时后响应处理的竞态。**

**超时机制 (`jsonrpc_router.py:709-765`)：**
```python
async def invoke_tool_on_ide(self, tool_name, arguments, timeout=120.0):
    tool_future = loop.create_future()
    self._pending_tools[tool_call_id] = tool_future   # 按 toolCallId 注册
    self._pending_responses[rpc_id] = tool_future     # 按 JSON-RPC id 注册

    await self._send_request("tool/invoke", {...}, rpc_id)

    try:
        return await asyncio.wait_for(tool_future, timeout=timeout)
    except asyncio.TimeoutError:
        return f"[超时] 工具 {tool_name} 执行超时（{timeout}s）"
    finally:
        self._pending_tools.pop(tool_call_id, None)   # 清理
        self._pending_responses.pop(rpc_id, None)     # 清理
```

**未匹配响应的原因：**
1. 超时后 `finally` 块从 `_pending_responses` 中 pop 掉了 `rpc_id`
2. IDE 之后终于返回了结果 → `_handle_response()` 找不到匹配的 Future
3. 日志打出 "收到未匹配的响应"，响应被静默丢弃

**为什么 IDE 响应慢：** 可能原因包括 IDE 侧工具执行本身耗时长、IDE 繁忙无法及时处理、网络延迟等。

**超时结果如何处理：**
- 超时错误字符串作为工具结果返回给 LLM
- LLM 看到超时信息后可以调整策略（如换工具、简化请求）
- 不中断 tool-calling 循环

**影响：** 工具调用失败导致 LLM 无法获取信息，但 LLM 会收到超时提示并可自适应。频繁超时影响用户体验（Agent 响应质量下降）。

---

## 问题 4: Heartbeat ReadTimeout

### 现象
```
ERROR [Heartbeat] LLM call error for agent 162a9d18: ReadTimeout
```
06:25 出现 2 次。

### 根因
**Heartbeat 自建 LLM 调用逻辑，未使用 `caller.py` 的 failover/streaming 能力。**

**关键发现 (`heartbeat.py:289-304`)：**
```python
# heartbeat 自己的 LLM 调用循环，没有用 call_llm / call_llm_with_failover
response = await client.complete(...)  # 非流式，需等待完整响应
```

**缺失能力对比：**

| 能力 | call_llm_with_failover | heartbeat 自建 |
|------|----------------------|---------------|
| Failover 备用模型 | ✅ | ❌ |
| 流式输出 | ✅ | ❌ (用 complete) |
| 取消中断 | ✅ (cancel_event) | ❌ |
| DeepSeek V4 thinking | ✅ (_get_thinking_kwargs) | ❌ |
| Token 限额检查 | ✅ | ❌ |

**ReadTimeout 触发原因：**
- 默认超时 120s（`model_request_timeout or 120.0`）
- `client.complete()` 非流式，必须等待完整响应
- 复杂工具调用链可能超过 120s

**ReadTimeout 后果：**
- `reply = ""`，心跳回复为空
- `break` 退出循环
- **但 `last_heartbeat_at` 仍被更新** → 下次心跳要等 4 小时
- 无重试机制

**影响：** 心跳偶尔超时属于正常现象，但当前处理有 2 个问题：(1) 失败后仍更新时间戳导致 4 小时内不再重试；(2) 缺少 failover 无法切换到备用模型。

---

## 问题 5: LSP4J 模型查找失败 key=auto

### 现象
```
WARNING [LSP4J] 模型查找失败: key=auto
```
14:44-15:21 出现 5 次。

### 根因
**通义灵码 IDE 插件发送 `key="auto"` 表示"自动选择模型"，但后端未做特殊处理。**

**模型选择优先级 (`jsonrpc_router.py:481-528`)：**
```
1. customModel (BYOK) → 最高优先级
2. extra.modelConfig.key → 第二优先级，调用 _resolve_model_by_key()
3. self._model_obj (Agent 默认模型) → 兜底
```

**`_resolve_model_by_key("auto")` 流程 (`jsonrpc_router.py:164-194`)：**
1. 尝试 UUID 解析 → `uuid.UUID("auto")` → ValueError → 跳过
2. 尝试 model 名称查找 → `SELECT ... WHERE model = 'auto'` → 无结果
3. 返回 `None` + WARNING 日志

**回退行为：** `model_obj` 保持为 `self._model_obj`（Agent 默认模型），请求正常执行。**功能不受影响**，仅有多余 WARNING 日志。

**`"auto"` 的语义：** IDE 插件表示"让服务端自动选择"，但后端没有实现 auto 语义的处理逻辑。

---

## 问题 6: LSP4J 路由器未找到

### 现象
```
WARNING [LSP4J-TOOL] 路由器未找到: agent_key=('410ab5c8...', '82e3e5d0...') active_keys=[]
```
15:16 出现 1 次。

### 根因
`tool_hooks.py` 通过 `_active_routers` 字典查找路由器，但 `active_keys=[]` 说明该用户+Agent 组合当前没有活跃的 LSP4J WebSocket 连接。

可能原因：
1. IDE 断连后 `_active_routers` 已清理，但 `call_llm` 的工具调用仍在进行
2. 与问题 2（WebSocket 断连竞态）相关

---

## 修复优先级建议

| 优先级 | 问题 | 修复思路 | 改动范围 |
|--------|------|----------|----------|
| P1 | Failover 误判 | `call_llm_with_failover` 在打 WARNING 前先检查结果是否为错误前缀 | `caller.py` 1 处 |
| P1 | failover.py 大小写 | `"[llm error]"` 改为 `.lower()` 比较或匹配实际大小写 | `failover.py` 1 处 |
| P2 | WebSocket 发送失败 | 添加 `_closed` 标记 + `_send_message` 中检查连接状态 + `cleanup` 取消运行中的 call_llm | `jsonrpc_router.py` 3 处 |
| P2 | 工具调用超时竞态 | 超时后标记已取消，`_handle_response` 检查并跳过 | `jsonrpc_router.py` 2 处 |
| P3 | Heartbeat ReadTimeout | (a) 失败时不更新 `last_heartbeat_at`；(b) 考虑增加超时或添加重试 | `heartbeat.py` 2 处 |
| P3 | key=auto 模型查找 | `_resolve_model_by_key` 对 `"auto"` 直接返回 `None` 不打 WARNING | `jsonrpc_router.py` 1 处 |
| P4 | Heartbeat 缺失能力 | 重构 heartbeat 使用 `call_llm_with_failover` | `heartbeat.py` 大改 |

---

## 涉及文件

| 文件 | 问题关联 |
|------|----------|
| `backend/app/services/llm/caller.py` | #1 Failover 误判 |
| `backend/app/services/llm/failover.py` | #6 大小写不匹配 |
| `backend/app/plugins/clawith_lsp4j/jsonrpc_router.py` | #2 #3 #5 WebSocket/超时/模型 |
| `backend/app/plugins/clawith_lsp4j/router.py` | #2 WebSocket 生命周期 |
| `backend/app/plugins/clawith_lsp4j/tool_hooks.py` | #6 路由器查找 |
| `backend/app/plugins/clawith_lsp4j/context.py` | #2 连接状态管理 |
| `backend/app/services/heartbeat.py` | #4 ReadTimeout |
