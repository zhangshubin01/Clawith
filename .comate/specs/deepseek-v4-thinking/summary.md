# DeepSeek V4 Thinking 模式适配 - 总结

## 完成内容

### 问题
使用 `deepseek-v4-pro` / `deepseek-v4-flash` 模型时，API 返回 400 错误：
```
The `reasoning_content` in the thinking mode must be passed back to the API.
```

### 根因
DeepSeek V4 思考模式需要：
1. 请求中传入 `thinking: {"type": "enabled"}` 开启思考模式
2. 有工具调用时，assistant 消息的 `reasoning_content` 必须完整回传

### 修改文件

1. **`backend/app/api/websocket.py:329`** — 历史消息 `thinking` → `reasoning_content` 转换
2. **`backend/app/services/llm/caller.py`** — 三处改动：
   - 新增 `_get_thinking_kwargs()` 辅助函数（检测 DeepSeek V4 模型名，返回 thinking 参数）
   - `call_llm()` 中 `client.stream()` 传入 `**_thinking_kwargs`
   - `call_agent_llm()` 中 `client.complete()` 传入 `**_thinking_kwargs`
3. **`backend/app/services/llm/caller.py:331`** — `LLMMessage` 接收 `reasoning_content` 字段

### 未修改的文件

- `client.py:_build_payload()` — `payload.update(kwargs)` 已能正确注入 thinking 参数，无需额外处理
- `client.py` 的 `LLMMessage.to_openai_format()` — 已有 `reasoning_content` 输出
- `client.py` 的流式解析 — 已有 `reasoning_content` 解析

### 设计决策

1. **按 model 名称检测**（而非 provider），因为同一 provider 下不同模型行为不同（deepseek-v4-pro vs deepseek-chat vs deepseek-reasoner），且第三方中转时 provider 可能不同
2. **提取辅助函数** `_get_thinking_kwargs()` 复用检测逻辑，保持 DRY
3. **不修改 `_build_payload()`**，`payload.update(kwargs)` 已足够，且 DeepSeek 不像 Anthropic 那样需要强制设置 temperature=1.0
4. **不处理 `deepseek-reasoner`**，其规则与 V4 完全相反（禁止传入 reasoning_content），按用户要求只适配 V4

### 部署状态

后端服务已重启，运行在端口 8000，日志无报错。
