# DeepSeek V4 Thinking 模式适配

## 需求场景

使用 `deepseek-v4-pro` / `deepseek-v4-flash` 模型时，API 返回 400 错误：
```
The `reasoning_content` in the thinking mode must be passed back to the API.
```

**根因**：DeepSeek V4 的 thinking 模式要求：
1. 请求中传入 `thinking: {"type": "enabled"}` 开启思考模式
2. 有工具调用时，assistant 消息的 `reasoning_content` 必须完整回传给 API
3. 思考模式下 `temperature` 应设为 1.0

## 官方文档规则

来源：https://api-docs.deepseek.com/zh-cn

### 模型与参数

- `deepseek-v4-pro` / `deepseek-v4-flash`：支持思考模式 + Function Calling
- `thinking: {"type": "enabled"}` — 开启思考模式
- `reasoning_effort` — 可选，控制思考强度（如 `"high"`），暂不实现

### reasoning_content 拼接规则

| 场景 | reasoning_content 处理 |
|------|----------------------|
| 无工具调用（纯对话） | API 会忽略，传不传都可以 |
| 有工具调用 | **必须完整回传**，否则返回 400 |

官方示例（工具调用场景）：
```python
messages.append(response.choices.message)
# 等价于：
messages.append({
    'role': 'assistant',
    'content': response.choices.message.content,
    'reasoning_content': response.choices.message.reasoning_content,  # 必须
    'tool_calls': response.choices.message.tool_calls,
})
```

### 温度参数

官方文档：思考模式下 `temperature`、`top_p` 不生效，DeepSeek 推荐设为 1.0。

## 当前代码状态

| 适配点 | 文件:行 | 状态 |
|--------|---------|------|
| 历史消息 thinking → reasoning_content | websocket.py:329 | ✅ 已修复 |
| LLMMessage 接收 reasoning_content | caller.py:331 | ✅ 已修复 |
| 工具调用轮次回传 reasoning_content | caller.py:421 | ✅ 已有 |
| 传入 thinking 参数 | caller.py:374,754 | ❌ 未适配 |
| 思考模式温度处理 | client.py:_build_payload | ❌ 未适配 |

## 技术方案

### 修改文件 1：`backend/app/services/llm/client.py`

**位置**：`OpenAICompatibleClient._build_payload()`（行 318-354）

**改动**：参照 AnthropicClient 的 `thinking` 处理模式（行 1547-1553），在 `_build_payload()` 中添加 `thinking` 参数的特殊处理：

```python
def _build_payload(self, messages, tools, temperature, max_tokens, stream=False, **kwargs):
    """构建请求负载。"""
    messages_payload = [m.to_openai_format() for m in messages]
    payload: dict[str, Any] = {
        "model": self.model,
        "messages": messages_payload,
        "stream": stream,
    }

    # DeepSeek V4 思考模式：提取 thinking 参数并处理温度
    thinking = kwargs.pop("thinking", None)
    if thinking:
        payload["thinking"] = thinking
        # 思考模式下 temperature 应为 1.0（DeepSeek 官方文档说明）
        if temperature is None:
            payload["temperature"] = 1.0

    if temperature is not None:
        payload["temperature"] = temperature

    # ... 其余逻辑不变 ...

    payload.update(kwargs)
    return payload
```

**要点**：
- 与 AnthropicClient 风格一致（pop thinking → 注入 payload → 处理温度）
- `kwargs.pop("thinking")` 确保不会重复写入 payload
- 只在 thinking 开启且未显式指定 temperature 时设为 1.0
- 如果用户显式设了 temperature，尊重用户设置

### 修改文件 2：`backend/app/services/llm/caller.py`

**位置 1**：`call_llm()` 中 `client.stream()` 调用（行 372-381）

**改动**：检测 DeepSeek V4 模型时传入 `thinking` 参数：

```python
try:
    # DeepSeek V4 思考模式：检测模型名称并传入 thinking 参数
    stream_kwargs = {}
    model_name = getattr(model, 'model', '') or ''
    if 'deepseek-v4' in model_name or 'deepseek_v4' in model_name:
        stream_kwargs["thinking"] = {"type": "enabled"}

    response = await client.stream(
        messages=api_messages,
        tools=tools_for_llm if tools_for_llm else None,
        temperature=model.temperature,
        max_tokens=max_tokens,
        on_chunk=on_chunk,
        on_thinking=on_thinking,
        **stream_kwargs,
    )
```

**位置 2**：`call_agent_llm_with_tools()` 中 `client.complete()` 调用（行 747-752）

**改动**：同上逻辑：

```python
# DeepSeek V4 思考模式：检测模型名称并传入 thinking 参数
complete_kwargs = {}
_model_name = getattr(model, 'model', '') or ''
if 'deepseek-v4' in _model_name or 'deepseek_v4' in _model_name:
    complete_kwargs["thinking"] = {"type": "enabled"}

for round_i in range(max_rounds):
    try:
        response = await client.complete(
            messages=api_messages,
            tools=tools_for_llm if tools_for_llm else None,
            temperature=model.temperature,
            max_tokens=max_tokens,
            **complete_kwargs,
        )
```

### 不需要修改的文件

- `websocket.py`：已修复 ✅
- `client.py` 的 `LLMMessage.to_openai_format()`：已有 `reasoning_content` 输出 ✅
- `client.py` 的流式解析：已有 `reasoning_content` 解析（行 421-423）✅

### 边界条件

1. **非 DeepSeek V4 模型**：`stream_kwargs`/`complete_kwargs` 为空，行为不变
2. **deepseek-v4-flash**：名称包含 `deepseek-v4`，自动覆盖
3. **第三方中转**（如硅基流动）：如果 model 名称包含 `deepseek-v4`，也能匹配
4. **所有调用路径**（heartbeat、feishu、gateway 等）：都通过 `call_llm()` 统一入口，自动受益
5. **temperature 显式配置**：如果数据库中模型配置了 temperature，优先使用用户配置

### 预期结果

- `deepseek-v4-pro` / `deepseek-v4-flash` 思考模式正常开启
- 有工具调用时 `reasoning_content` 正确回传，不再返回 400
- 非 DeepSeek V4 模型不受影响
- 与项目现有 Anthropic thinking 处理风格一致
