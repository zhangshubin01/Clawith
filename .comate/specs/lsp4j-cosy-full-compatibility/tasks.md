# LSP4J 灵码插件全量兼容 — 任务计划

## 任务清单

- [x] Task 1：Phase 0 基础设施 — call_llm 签名增强与 cancel_event 透传
    - 1.1：在 `caller.py:call_llm` 签名末尾增加 `cancel_event: asyncio.Event | None = None` 参数
    - 1.2：在 `caller.py:call_llm` 的 `client.stream()` 调用处透传 `cancel_event=cancel_event`
    - 1.3：在 `caller.py` 工具循环 readonly 并行执行前（line 461 左右）插入 cancel 检查
    - 1.4：在 `caller.py` 工具循环 write 串行执行前（line 476 左右）插入 cancel 检查
    - 1.5：在 `caller.py:call_llm_with_failover` 签名末尾增加 `cancel_event` 参数
    - 1.6：在 `caller.py:call_llm_with_failover` 的 primary 和 fallback 两个 `call_llm` 调用点透传 `cancel_event`
    - 1.7：在 `caller.py` 工具循环的 `for round_i in range(_max_tool_rounds)` 循环入口处插入 cancel 检查（P0-3）

- [x] Task 2：Phase 1 零风险注入 — ChatAskParam dataclass 与参数解析重构
    - 2.1：在 `jsonrpc_router.py` 顶部导入 `dataclass` 并定义 `ChatAskParam` dataclass（17 字段，全部设默认值）
    - 2.2：重构 `_handle_chat_ask` 的参数解析逻辑，使用 `ChatAskParam` 替代原始 `dict` 取值
    - 2.3：在 `_handle_chat_ask` 中保存 `self._stream_mode = ask.stream` 和 `self._current_session_type = ask.sessionType or ""`
    - 2.4：在 `_send_chat_answer` 中补充 `extra` 字段（含 `sessionType`）
    - 2.5：在 `on_chunk` 回调中增加 `_stream_mode` 判断，仅在 `stream=True` 时推送 `chat/answer`
    - 2.6：验证 `chat/ask` 响应始终包含 `isSuccess=True`（P0-1，当前代码已包含，需确认不被破坏）

- [x] Task 3：Phase 1 零风险注入 — ide_prompt 构建与注入
    - 3.1：在 `jsonrpc_router.py` 中定义 `_CHAT_TASK_HINTS`（基于 `ChatTaskEnum.java` 实际枚举值）
    - 3.2：实现 `_build_lsp4j_ide_prompt(params: ChatAskParam)` 函数
    - 3.3：在 `_handle_chat_ask` 中调用 `_build_lsp4j_ide_prompt`，将结果追加到 `role_description`
    - 3.4：在 `_handle_chat_ask` 中计算并传递 `supports_vision` 到 `call_llm`（注意：若 Task 5 切换了模型，需在 Task 5 后重新计算）

- [x] Task 4：Phase 1 零风险注入 — client_type 标记与 chat/stop 响应修复
    - 4.1：在 `_persist_lsp4j_chat_turn` 创建 `ChatSession` 时增加 `client_type="ide_plugin"`
    - 4.2：修复 `_handle_chat_stop`（已存在，line 348-356），添加 `await self._send_response(msg_id, {})` 返回响应（P1-6）

- [x] Task 5：Phase 2 模型切换 — BYOK 与模型配置
    - 5.1：在 `_handle_chat_ask` 中实现 `customModel` 处理逻辑（创建 transient `LLMModel`，注入 `_runtime_api_key`，并根据 `isVl` 设置 `supports_vision`）
    - 5.2：修改 `utils.py:get_model_api_key`，优先读取 `_runtime_api_key`
    - 5.3：实现 `_resolve_model_by_key` 异步函数（按 UUID 或 model 名称查数据库）
    - 5.4：在 `_handle_chat_ask` 中集成 `extra.modelConfig.key` 模型切换逻辑

- [x] Task 6：Phase 2 存根扩展 — METHOD_MAP 补齐所有缺失方法
    - 6.1：添加 `_handle_stub` 通用存根处理器
    - 6.2：在 `_METHOD_MAP` 中注册所有缺失方法（listAllSessions、getSessionById 等 5 个历史管理方法 + quota/doNotRemindAgain + `tool/call/results` 等），**注意：只做 `.update()` / `.update()` 追加，勿整体替换 dict，确保已有的 `tool/invokeResult` 等现有条目不被覆盖**（P1-12 + 问题 A 修复）
    - 6.3：实现 `_handle_step_process_confirm` 方法（返回完整 `StepProcessConfirmResult`）并注册 `agents/testAgent/stepProcessConfirm`（P1-11）
    - 6.4：实现 `_handle_code_change_apply` 方法（Phase 4 MVP 存根版：返回空 `{}`），并注册 `chat/codeChange/apply`（P1-9 + P1-10）

- [x] Task 7：Phase 3 任务规划 — step callback 与确认处理
    - 7.1：实现 `_send_process_step_callback` 方法（发送 `chat/process_step_callback` 通知）
    - 7.2：在 `_handle_chat_ask` 的 `call_llm` 调用前后分别发送 `step_start` 和 `step_end`
    - 7.3：在 `on_tool_call` 回调中发送工具执行步骤的 `doing`/`done` callback

- [x] Task 8：Phase 4 代码变更 — codeChange/apply 完整实现
    - 8.1：重写 `_handle_code_change_apply`，返回完整 `ChatCodeChangeApplyResult`（9 个字段）
    - 8.2：在 `_handle_code_change_apply` 中发送 `chat/codeChange/apply/finish` 通知

- [x] Task 9：代码质量与边界补充
    - 9.1：为所有新增/修改的函数、关键分支补充中文注释（评审要求 #1）
    - 9.2：在 `_build_lsp4j_ide_prompt` 中处理 `shellType`（用于终端命令生成的 shell 类型提示）和 `pluginPayloadConfig` 字段（若存在则记录日志，P2-2 完整补充）
    - 9.3：在 Task 6.2 的 `_METHOD_MAP` 中确认 `inlineEditParams` 相关方法（若灵码调用则返回存根，P2-3 补充）
    - 9.4：验证扩展后的 `_METHOD_MAP` 未遗漏现有条目（特别是 `tool/invokeResult`，问题 A 修复）
    - 9.5：修复 `jsonrpc_router.py:202` docstring，将 "16 个字段" 改为 "17 个字段"（P2-5）
    - 9.6：在 `_persist_lsp4j_chat_turn` 的 ChatSession 创建逻辑中添加 TODO 注释，说明 `project_path` / `current_file` / `open_files` 字段已存在于模型中，待插件未来发送对应数据后可直接利用（P2-4 备注，当前不增加死代码）

- [x] Task 10：部署验证与回归测试
    - 10.1：运行 `./restart.sh` 重启 Clawith
    - 10.2：检查日志确认无异常
    - 10.3：使用灵码插件进行端到端测试（聊天、diff、工具调用、任务规划）
    - 10.4：验证 Web UI 能正确显示 LSP4J 会话
    - 10.5：验证 `chat/ask` 响应始终包含 `isSuccess=True`
    - 10.6：运行 `python3 -c "from graphify.watch import _rebuild_code; from pathlib import Path; _rebuild_code(Path('.'))"` 更新 graphify
