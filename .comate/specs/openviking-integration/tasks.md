# OpenViking 语义记忆集成任务计划

> 与 doc.md（v3）保持同步，所有任务均基于实际代码核查结果。

---

- [ ] Task 1: `openviking_client.py` — 超时拆分 + 新增 Auto-Capture 函数
    - 1.1: 在文件**顶部**添加 `import asyncio`（与现有 inline `import asyncio` 统一，消除 NameError 风险）
    - 1.2: 将 `OPENVIKING_TIMEOUT` 默认值从 `3.0` 改为 `1.0`（Auto-Recall 宁可超时降级，不可阻塞 LLM）
    - 1.3: 新增 `OPENVIKING_CAPTURE_TIMEOUT = float(os.environ.get("OPENVIKING_CAPTURE_TIMEOUT", "5.0"))`（Auto-Capture 是 fire-and-forget，允许宽松超时）
    - 1.4: 新增 `capture_session_memories(agent_id: str, turns: list[dict]) -> bool` 函数
        - 接收结构化 `turns`（每条 `{"role": "user"|"assistant", "content": "..."}`），逐条 POST，保留角色语义
        - 步骤顺序：创建 session → 逐条发送 turns（用 `OPENVIKING_CAPTURE_TIMEOUT`）→ `asyncio.create_task(_fire_extract(...))` fire-and-forget
        - `_fire_extract` 的 `finally` 块已负责 DELETE session，session 不泄漏
        - `done_callback` 中先判断 `task.cancelled()` 再取 `task.exception()`，避免 `CancelledError`
    - 1.5: 添加详细中文注释（说明 WHY）和 `[OpenViking]` 日志（含 session_id 前 8 位、turns 数量）

- [ ] Task 2: `openviking_watcher.py` — 线程安全修复 + 监控范围扩展
    - 2.1: `AgentMemoryWatcher.__init__` 增加 `loop: asyncio.AbstractEventLoop` 参数，保存为 `self._loop`
    - 2.2: 新增 `_WATCHED_NAMES = {"memory.md", "focus.md", "agenda.md"}` 和 `_WATCHED_PARENT = "skills"` 类变量
    - 2.3: 新增 `_should_index(self, path: Path) -> bool` 方法，判断文件是否需要触发索引
    - 2.4: `on_modified` 改为：判断 `_should_index` → 通过 `self._loop.call_soon_threadsafe(self._schedule_debounce, agent_id, path)` 调度（**必须 call_soon_threadsafe，不能直接 call_later**，因为 on_modified 在 watchdog 后台线程中执行）
    - 2.5: 新增 `_schedule_debounce(self, agent_id, path)` 方法，在 event loop 线程中执行 debounce 逻辑（cancel + call_later）
    - 2.6: `_do_index()` 闭包内使用 `asyncio.create_task(...)`（**禁止 `asyncio.ensure_future(..., loop=)`，Python 3.10 已移除该签名**）
    - 2.7: `start_watcher` 增加 `loop: asyncio.AbstractEventLoop | None = None` 参数，默认 `asyncio.get_running_loop()`
    - 2.8: 添加中文注释说明线程安全设计（watchdog 后台线程不能直接操作 asyncio 事件循环）

- [ ] Task 3: `caller.py` — Auto-Recall + Auto-Capture
    - 3.1: 新增 `_extract_query_from_messages(messages: list[dict], max_count: int = 3) -> str` 辅助函数
        - 从最新到最旧遍历，收集 `role == "user"` 的消息
        - 停止条件：总字符数 >= 50（约 25 个汉字）或已收集 `max_count` 条
        - 返回正序拼接，截断 500 chars
    - 3.2: Auto-Recall：在 `build_agent_context` 返回后（line 360）、创建 `api_messages` 前（line 366），调用 `search_memory` 追加到 `dynamic_prompt`
        - 用 `_extract_query_from_messages(messages)` 提取查询词（`messages` 是原始 `list[dict]` 参数）
        - 记录 latency（`time.monotonic()` 在 `is_available()` 通过后开始计时）
    - 3.3: Auto-Capture：在 `if not response.tool_calls:` 分支内，`record_token_usage` 之后、`return response.content` 之前触发
        - **正常完成路径只有一条**（循环内 `if not response.tool_calls: return response.content`），无需 `_ov_captured` 标志位
        - 从 `api_messages` 提取 turns：`role in ('user', 'assistant')`，过滤 system/tool 消息
        - 视觉消息 content 是 `list[dict]`，只提取 `type == "text"` 的块，忽略 `image_url`
        - 当前轮回复 `response.content` 作为最后一条 assistant turn 追加
        - `asyncio.create_task(capture_session_memories(...))` fire-and-forget
    - 3.4: 添加 `[OpenViking]` 日志（Auto-Recall 含 latency=...ms、snippets 数量、query 前 50 chars；Auto-Capture 含 turns 数量）
    - 3.5: 异常处理：任何 OV 失败不影响主流程，全文 Memory 已在 `build_agent_context` 中兜底

- [ ] Task 4: `main.py` — lifespan 启动 watcher
    - 4.1: 在 ss-local proxy task（约 line 253）之后、`yield` 之前，导入并调用 `start_watcher`
    - 4.2: **显式传入 event loop**：`_start_ov_watcher(loop=asyncio.get_running_loop())`（避免依赖 start_watcher 内部隐式获取）
    - 4.3: try/except 包裹，启动失败记录 `[startup] OpenViking watcher failed: {e}` warning 但不影响应用
    - 4.4: `yield` 后 shutdown：`_ov_observer.stop()` + `_ov_observer.join(timeout=5)`
    - 4.5: 添加 `[startup] OpenViking watcher started` 和 `[shutdown] OpenViking watcher stopped` 日志

- [ ] Task 5: `agent_tools.py` — 扩展索引触发点
    - 5.1: 在现有 `memory.md` 触发逻辑（line 2154-2162）后，新增 `focus.md` / `agenda.md` 触发
    - 5.2: 新增 `skills/*.md` 触发，**统一使用 `str(agent_id)` 作为 agent scope**（与 caller.py 的 search_memory 使用的 scope 一致，保证检索命中）
    - 5.3: 添加 `[OpenViking]` 索引触发日志（含 agent_id 前 8 位、type=memory/focus/skills）
    - 5.4: 保留现有 try/except 包裹，索引失败不影响写入结果

- [ ] Task 6: 验证与清理
    - 6.1: 确认 `build_agent_context` 签名未被修改（其他 6 个调用点零影响）
    - 6.2: 确认日志前缀统一为 `[OpenViking]`（与现有 `[LSP4J]`、`[A2A]` 风格一致）
    - 6.3: 确认 Auto-Capture **只在** `if not response.tool_calls: return response.content` 分支触发，token 限制返回、cancel_event 中断、LLMError、"Too many rounds" 均不触发
    - 6.4: 确认 heartbeat/scheduler/task_executor/supervision_reminder 等其他 `build_agent_context` 调用点无 OV 相关日志
    - 6.5: 确认 `openviking_client.py` 顶部已有 `import asyncio`
    - 6.6: 确认 watcher 监控范围覆盖 memory.md / focus.md / agenda.md / skills/*.md
    - 6.7: 运行 graphify rebuild 更新知识图谱
    - 6.8: 运行项目现有测试确保未破坏（`pytest backend/` 或现有 CI）
