# DeepSeek V4 Thinking 模式适配任务

- [x] Task 1: 在 caller.py 中添加 DeepSeek V4 thinking 模式辅助函数
    - 1.1: 在 `_get_model_timeout()` 函数后新增 `_get_thinking_kwargs()` 辅助函数
    - 1.2: 函数逻辑：检测 `model.model` 是否包含 `deepseek-v4` 或 `deepseek_v4`，是则返回 `{"thinking": {"type": "enabled"}}`，否则返回空字典

- [x] Task 2: 修改 call_llm() 中的 client.stream() 调用
    - 2.1: 在 `client.stream()` 调用前，调用 `_get_thinking_kwargs(model)` 获取参数
    - 2.2: 将获取的参数通过 `**stream_kwargs` 传入 `client.stream()`

- [x] Task 3: 修改 call_agent_llm() 中的 client.complete() 调用
    - 3.1: 在 `client.complete()` 调用前，调用 `_get_thinking_kwargs(model)` 获取参数
    - 3.2: 将获取的参数通过 `**complete_kwargs` 传入 `client.complete()`

- [x] Task 4: 部署并验证
    - 4.1: 重启后端服务
    - 4.2: 查看运行日志确认无报错
