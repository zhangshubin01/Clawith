# OpenViking 语义记忆集成方案（v3）

## 需求场景

OpenViking 是火山引擎开源的语义记忆系统（https://github.com/volcengine/OpenViking），支持分层记忆存储（L0/L1/L2）、基于会话的自动提取（Auto-Capture）和语义检索（Auto-Recall）。当前 Clawith 项目已有 OpenViking 的 HTTP 客户端代码，但核心功能未启用：

1. `search_memory()` 从未被调用 — 语义检索功能实际不可用
2. `start_watcher()` 从未启动 — 文件变更无法自动触发索引
3. 仅通过 `write_file` 工具触发索引 — 索引触发点单一
4. Auto-Capture 完全缺失 — 会话结束后不自动提取记忆
5. `openviking_watcher.py` 线程安全隐患 — watchdog 线程中无法安全调度 asyncio 任务

## 正面收益

| 收益 | 量化 | 说明 |
|------|------|------|
| **token 成本降低** | ~91% 输入 token 减少 | OV 官方测试：语义检索替代全文注入，减少无关上下文 |
| **任务完成率提升** | ~43% | OV 官方测试：精准记忆检索帮助 Agent 更好利用历史经验 |
| **自我进化自动化** | 从"手动写 memory.md"到"系统自动提取" | Auto-Capture 弥补 LLM 遗漏写入的损失 |
| **记忆检索精准度** | 从"全文 2000 chars 截断"到"按相关性检索 top-5" | 长期记忆超 2000 chars 时全文截断丢失信息，语义检索保留最相关的 |
| **多源知识整合** | 企业文档+技能定义+对话记忆统一检索 | 当前只有 memory.md，OV 可索引 focus.md、skills/ 等 |
| **智能体持久性** | 记忆不随 workspace 文件丢失 | OV 服务端存储，即使 agent workspace 被清理，记忆仍可检索 |

## 根因分析

1. **调用链缺失**：`caller.py` 未在 `build_agent_context` 之后调用 `search_memory()`
2. **生命周期缺失**：`main.py` lifespan 未启动 watcher
3. **Auto-Capture 缺失**：会话结束后未调用 OV 的 `session/extract` 管线自动提取记忆
4. **与现有 memory.md 注入冲突**：`agent_context.py:259-262` 全文注入 `memory.md`（max 2000 chars），若同时注入语义检索结果会重复
5. **watcher 线程安全**：`openviking_watcher.py:38` 在 watchdog 线程调用 `asyncio.get_running_loop()`，会抛 `RuntimeError` 被静默吞掉

## 架构与技术方案

### 核心理念：双通道记忆（对标 OpenViking 官方实践）

| 能力 | 说明 | 对标官方实践 |
|------|------|-------------|
| **Auto-Recall** | 每次用户提问前自动检索相关记忆 | Claude Code Plugin 的 `UserPromptSubmit` hook |
| **Auto-Capture** | 会话结束后自动提取有价值信息 | Claude Code Plugin 的 `Stop` hook |

### 方案选择：caller.py 层注入（不改 build_agent_context 签名）

**为什么不在 `build_agent_context` 内注入？**

`build_agent_context` 有 **7 个生产调用点**（caller.py、agent_tools.py×2、heartbeat.py×2、scheduler.py、task_executor.py、supervision_reminder.py×2），其中只有 `caller.py` 有用户消息可做查询词。修改签名虽不会破坏现有调用（默认值 `query=""`），但其他调用点每次都会执行 `is_available()` 健康检查，浪费请求。

**选定方案**：在 `caller.py:360` 之后、`line 366` 之前，单独调用 `search_memory`，将结果追加到 `dynamic_prompt`。

- 改动范围最小（仅 caller.py 一个文件修改签名相关的部分）
- 其他 6 个调用点零影响
- 语义检索只在实际有用户消息时触发

### 记忆注入策略：叠加+提示（非替代）

**现状**：`build_agent_context()` 全文注入 `memory.md`（max 2000 chars）到 `dynamic_parts` 中的 `## Memory` 段落

**改进**：保留全文 `## Memory`，追加 `## Relevant Memories (semantic)` 作为补充

**为什么不用"替代"策略？**
- `dynamic_prompt` 是拼接好的字符串，无法精准移除 `## Memory` 段落
- `index_memory_file()` 是异步 fire-and-forget，索引可能延迟或失败
- 全文注入保证智能体刚写入的知识**一定可见**（自我进化链条不断）
- 语义检索结果可能遗漏当前任务高度相关但语义距离较远的记忆

**叠加策略**虽然可能有少量重复，但最安全。未来可优化为替代策略（需要重构 `build_agent_context` 返回结构化的段落列表）。

### Auto-Capture：会话后自动提取

在 `call_llm` 的正常完成路径触发，将对话内容提交给 OV 的 session/extract 管线。

## 受影响文件

### 修改文件

| 文件 | 位置 | 变更 | 影响范围 |
|------|------|------|---------|
| `backend/app/services/llm/caller.py` | line 360-366 | `build_agent_context` 后追加 OV 语义检索；正常完成路径触发 Auto-Capture | **仅此文件**，其他 6 个调用点零影响 |
| `backend/app/main.py` | line 253 附近 | lifespan 启动 watcher | 应用生命周期 |
| `backend/app/services/openviking_watcher.py` | line 38-44 | 修复线程安全 | 文件监视器 |
| `backend/app/services/openviking_client.py` | line 23, 80-114 | 降低超时；新增 `capture_session_memories()` | HTTP 客户端 |
| `backend/app/services/agent_tools.py` | line 2154-2162 | 扩展索引触发点（focus.md, skills/） | 工具执行 |

### 不修改的文件

| 文件 | 说明 |
|------|------|
| `backend/app/services/agent_context.py` | **不修改**。build_agent_context 签名不变，OV 注入在 caller.py 层完成 |
| `.mcp.json` | 保留 MCP 配置，供 Claude Code 侧直接使用 `memory_recall` 等工具 |

### 保留但暂不使用的代码

| 文件 | 位置 | 说明 |
|------|------|------|
| `openviking_client.py` | `index_enterprise_info()` (line 206-245) | 完整实现，暂无调用方，保留供未来扩展 |
| `openviking_client.py` | `index_all_skills()` (line 248-287) | 同上 |

## 实现细节

### 1. caller.py — Auto-Recall + Auto-Capture

**a) 提取查询词辅助函数（新增，放在 caller.py 文件内）**

```python
def _extract_query_from_messages(messages: list[dict], max_count: int = 3) -> str:
    """从消息列表中提取最近几条用户消息拼接为查询词。

    WHY：OpenViking 语义检索需要查询词。单条消息可能不足以捕捉完整上下文
    （如多轮对话中的指代消解），取最近 2-3 条用户消息更可靠。
    如果用户最后一条消息过短（如“继续”、“好的”），会连同之前的上下文一起提取。
    参考：OpenViking Directory Recursive Retrieval 通过意图分析生成多个检索条件。
    """
    user_msgs = []
    for msg in reversed(messages):
        if msg.get("role") == "user" and msg.get("content"):
            content = msg["content"].strip()
            user_msgs.append(content)
            # ★ 阈值 50 字符（约 25 个汉字 / 10 个英文单词）足以捕捉完整任务描述。
            # 若最近一条消息过短（如"继续"、"好的"），继续向上收集上下文直到达到阈值或 max_count。
            if len(" ".join(user_msgs)) >= 50 or len(user_msgs) >= max_count:
                break
    return " ".join(reversed(user_msgs))[:500]
```

**b) Auto-Recall：在 build_agent_context 后注入语义检索（line 360-366 之间）**

```python
# line 360: 现有代码不变
static_prompt, dynamic_prompt = await build_agent_context(
    agent_id, agent_name, role_description, current_user_name=_user_name
)

# ★ Auto-Recall：语义检索补充相关记忆（叠加策略，不替代全文 Memory）
# WHY：build_agent_context 的 dynamic_prompt 已包含 ## Memory（全文注入 max 2000 chars），
# 此处追加 OV 语义检索结果作为精准补充。叠加而非替代，保证自我进化链条不断。
# 仅在 caller.py 触发（其他 6 个调用点无用户消息，不触发）。
if agent_id:
    _query = _extract_query_from_messages(messages)
    if _query:
        try:
            import time as _time
            _ov_t0 = _time.monotonic()
            from app.services.openviking_client import search_memory, is_available
            if await is_available():
                snippets = await search_memory(query=_query, agent_id=str(agent_id), limit=5)
                _ov_latency = (_time.monotonic() - _ov_t0) * 1000
                if snippets:
                    _ov_memory = "\n".join(f"- {s}" for s in snippets)
                    dynamic_prompt += f"\n## Relevant Memories (semantic)\n{_ov_memory}"
                    logger.info(f"[OpenViking] Auto-Recall 命中: agent={str(agent_id)[:8]} snippets={len(snippets)} latency={_ov_latency:.0f}ms query={_query[:50]}")
                else:
                    logger.debug(f"[OpenViking] Auto-Recall 无结果: agent={str(agent_id)[:8]} latency={_ov_latency:.0f}ms")
        except Exception as e:
            # ★ 语义检索失败不影响主流程，全文 Memory 已在 build_agent_context 中注入
            logger.debug(f"[OpenViking] Auto-Recall 失败（不影响主流程）: {repr(e)}")

# line 366: 现有代码不变
api_messages = [LLMMessage(role="system", content=static_prompt, dynamic_content=dynamic_prompt)]
```

**c) Auto-Capture：正常完成路径触发**

`call_llm` 的正常完成路径**只有一条**：工具调用循环内 `if not response.tool_calls: return response.content`（约 line 456）。
无论是第一轮无工具调用直接返回，还是经过多轮工具调用后最终 LLM 不再调用工具，都走同一个 `return` 语句。
循环结束后的 `return "[Error] Too many tool call rounds"` 是异常路径，不触发。

| 路径 | 位置 | 是否触发 Auto-Capture |
|------|------|---------------------|
| `if not response.tool_calls: return response.content`（唯一正常完成点） | line ~456 | 是 |
| token 限制返回 | line ~350 | 否 |
| cancel_event 中断 | line ~400 | 否 |
| `return "[Error] Too many tool call rounds"` | line ~535 | 否（异常路径） |

```python
# 在 if not response.tool_calls: 分支内，record_token_usage 之后、return 之前添加：
# ★ Auto-Capture：将会话内容提交给 OpenViking 自动提取记忆（fire-and-forget）
# WHY：OpenViking 核心能力——会话结束后自动分析对话内容，
# 提取值得长期记住的信息（用户偏好、操作技巧等），实现"越用越聪明"。
# 参考：OpenViking Claude Code Plugin 的 Stop hook + auto-capture.mjs
# 注意：此处是循环内的唯一正常 return，无需标志位防重入。
if agent_id:
    try:
        from app.services.openviking_client import capture_session_memories, is_available
        if await is_available():
            # ★ 提取结构化 turns：逐条保留 user/assistant 消息（不含 system prompt），
            # 保持角色信息供 OV 的 session/extract 管线识别意图与应答。
            # ★ 视觉消息的 content 是 list[dict]，需只提取文本部分，跳过 image_url 块。
            _turns = []
            for msg in api_messages:
                role = msg.role if hasattr(msg, 'role') else msg.get('role', '')
                content = msg.content if hasattr(msg, 'content') else msg.get('content', '')
                if not content or role not in ('user', 'assistant'):
                    continue
                if isinstance(content, str):
                    _turns.append({"role": role, "content": content[:2000]})
                elif isinstance(content, list):
                    # vision 消息：只提取 type=text 的块，忽略 image_url
                    text = " ".join(
                        p.get("text", "") for p in content
                        if isinstance(p, dict) and p.get("type") == "text"
                    )
                    if text.strip():
                        _turns.append({"role": role, "content": text[:2000]})
            # 当前轮最终回复（response.content 来自 LLM 流式输出，已是 str）
            if response.content:
                _turns.append({"role": "assistant", "content": response.content[:2000]})

            if _turns:
                asyncio.create_task(
                    capture_session_memories(agent_id=str(agent_id), turns=_turns),
                    name=f"ov-capture-{str(agent_id)[:8]}",
                )
                logger.info(f"[OpenViking] Auto-Capture 已触发: agent={str(agent_id)[:8]} turns={len(_turns)}")
    except Exception as e:
        logger.debug(f"[OpenViking] Auto-Capture 失败（不影响主流程）: {repr(e)}")
```

### 2. openviking_client.py — 新增 Auto-Capture + 超时调整

**a) 降低超时**

```python
# ★ 降低搜索超时：语义检索宁可丢失不可延迟主流程
# 1.0s 足够覆盖正常 OV 响应时间（通常 <200ms），避免 OV 慢时阻塞 LLM 调用
OPENVIKING_TIMEOUT = float(os.environ.get("OPENVIKING_TIMEOUT", "1.0"))  # 从 3.0 降到 1.0

# ★ Auto-Capture 专用超时：capture 是 fire-and-forget，允许更宽松的超时。
# 每条 turn 最多等 OPENVIKING_CAPTURE_TIMEOUT 秒，避免多轮对话串行发送时总时间过长。
OPENVIKING_CAPTURE_TIMEOUT = float(os.environ.get("OPENVIKING_CAPTURE_TIMEOUT", "5.0"))
```

**b) 新增 `capture_session_memories()` 函数**

接收结构化的 `turns` 列表（每个 turn 为 `{"role": "user"|"assistant", "content": "..."}` dict），逐条发送给 OV 以保留角色语义。

```python
import asyncio  # ★ openviking_client.py 顶部未导入 asyncio，此处需显式导入（与 index_memory_file 的 inline import 保持一致，建议统一移至文件顶部）


async def capture_session_memories(agent_id: str, turns: list[dict]) -> bool:
    """会话结束后自动提取记忆（Auto-Capture）。

    WHY：OpenViking 核心能力。将完整对话内容提交给 OV 的 session/extract 管线，
    OV 内部用 LLM 自动提取值得长期记住的信息（用户偏好、操作技巧、重要结论等），
    使得智能体"越用越聪明"，实现真正的自我进化。
    参考：OpenViking Claude Code Plugin 的 auto-capture.mjs 实现。

    Args:
        agent_id: 智能体 ID，用于记忆隔离
        turns: 结构化对话轮次，每条格式为 {"role": "user"|"assistant", "content": "..."}
               已过滤掉 system 消息和 tool 结果，只保留对话内容

    流程：创建 session → 逐条发送 turns（保留角色语义） → 触发 extract（fire-and-forget）
    注意：_fire_extract 在 finally 中负责 DELETE session，session 不会泄漏。
    """
    if not turns:
        return False

    headers = _agent_headers(agent_id)
    client = _get_client()
    session_id = None
    try:
        # 1. 创建临时 session
        r = await client.post("/api/v1/sessions", headers=headers, json={},
                              timeout=OPENVIKING_TIMEOUT)
        if r.status_code != 200:
            logger.debug(f"[OpenViking] Auto-Capture 创建 session 失败: status={r.status_code}")
            return False
        session_id = r.json().get("result", {}).get("session_id")
        if not session_id:
            return False

        # 2. ★ 逐条发送对话 turns（保留 role 信息，供 OV 识别用户意图与助手应答）
        # WHY：将所有内容压缩为单条 "user" 消息会丢失角色语义，降低 OV 提取质量。
        for turn in turns:
            await client.post(
                f"/api/v1/sessions/{session_id}/messages",
                headers=headers,
                json={"role": turn["role"], "content": turn["content"]},
                timeout=OPENVIKING_CAPTURE_TIMEOUT,  # ★ 用宽松的 capture 超时，N 条 turns 串行不超时
            )

        # 3. 触发 extract（fire-and-forget，_fire_extract 内部 finally 负责 DELETE session）
        # ★ 到达此处说明步骤 1+2 均成功，session 已有完整内容，可以安全触发 extract
        def _on_extract_done(task: asyncio.Task) -> None:
            # ★ 先检查是否已取消（服务关闭等场景），避免 task.exception() 抛 CancelledError
            if not task.cancelled() and task.exception():
                logger.debug(f"[OpenViking] Auto-Capture extract 失败: {repr(task.exception())}")

        task = asyncio.create_task(_fire_extract(client, session_id, headers))
        task.add_done_callback(_on_extract_done)

        logger.info(
            f"[OpenViking] Auto-Capture 已提交: agent={agent_id[:8]} session={session_id[:8]} turns={len(turns)}"
        )
        return True

    except httpx.ConnectError:
        _invalidate_availability_cache()
        # ★ ConnectError 时 session 未能创建，无需清理
        return False
    except Exception as e:
        logger.debug(f"[OpenViking] Auto-Capture 失败: {repr(e)}")
        # ★ 若 session_id 已创建但后续步骤失败，_fire_extract 未被调用，
        # session 将由 OV 服务端 TTL 自动清理（无需客户端主动 DELETE）
        if session_id:
            logger.debug(f"[OpenViking] 未完成的 session: session_id={session_id[:8]}（OV TTL 自动清理）")
        return False
```

### 3. openviking_watcher.py — 线程安全修复

```python
class AgentMemoryWatcher(FileSystemEventHandler):
    """监控 memory.md 变更并触发增量索引。

    ★ 线程安全：watchdog 的 on_modified 在 Observer 后台线程中执行，
    不能直接调用 asyncio.create_task()（会抛 RuntimeError）。
    必须通过 loop.call_soon_threadsafe() 安全地调度到事件循环线程。
    """

    def __init__(self, agents_root: Path, loop: asyncio.AbstractEventLoop):
        self.agents_root = agents_root
        self._loop = loop  # ★ 保存事件循环引用
        self._debounce_timers: dict[str, asyncio.TimerHandle] = {}
        self.debounce_delay = 2.0  # seconds

    # ★ 监控的文件类型与 agent_tools.py 的索引触发点保持一致
    _WATCHED_NAMES = {"memory.md", "focus.md", "agenda.md"}
    _WATCHED_PARENT = "skills"  # skills/ 目录下的所有 .md 文件

    def _should_index(self, path: Path) -> bool:
        """判断此文件变更是否需要触发索引。"""
        if path.name in self._WATCHED_NAMES:
            return True
        if path.suffix == ".md" and self._WATCHED_PARENT in path.parts:
            return True
        return False

    def on_modified(self, event):
        if event.is_directory:
            return
        path = Path(event.src_path)
        if not self._should_index(path):
            return

        agent_id = path.parent.name
        logger.debug("[OpenViking] 检测到文件变更: agent={} path={}", agent_id, path)

        # ★ 必须通过 call_soon_threadsafe 调度到 event loop 线程执行，
        # 因为 watchdog 的 on_modified 是在独立的后台线程中触发的。
        self._loop.call_soon_threadsafe(self._schedule_debounce, agent_id, path)

    def _schedule_debounce(self, agent_id: str, path: Path):
        # 取消之前的定时器
        old_timer = self._debounce_timers.pop(agent_id, None)
        if old_timer:
            old_timer.cancel()

        # 创建新的 debounce 定时器
        def _do_index():
            # ★ _do_index 由 call_later 调度，已在 event loop 线程中执行，
            # 可直接使用 asyncio.create_task。
            # 注意：asyncio.ensure_future(..., loop=) 已在 Python 3.10 移除，
            # 本项目目标 Python 3.11+，禁止使用该签名。
            asyncio.create_task(index_memory_file(agent_id, path))
            logger.info("[OpenViking] debounce 触发索引: agent={} path={}", agent_id, path.name)

        timer = self._loop.call_later(self.debounce_delay, _do_index)
        self._debounce_timers[agent_id] = timer


def start_watcher(
    agents_root: Path = Path.home() / ".clawith" / "data" / "agents",
    loop: asyncio.AbstractEventLoop | None = None,
) -> Observer | None:
    """启动文件监视器。

    Args:
        agents_root: Agent 数据根目录
        loop: asyncio 事件循环引用（用于线程安全调度）

    Returns:
        Running Observer instance, or None if agents_root doesn't exist
    """
    if not agents_root.exists():
        logger.warning("[OpenViking] agents_root 不存在，跳过 watcher: {}", agents_root)
        return None

    if loop is None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.warning("[OpenViking] 无运行中的事件循环，跳过 watcher")
            return None

    event_handler = AgentMemoryWatcher(agents_root, loop=loop)
    observer = Observer()
    observer.schedule(event_handler, str(agents_root), recursive=True)
    observer.start()
    logger.info("[OpenViking] watcher 已启动: root={}", agents_root)
    return observer
```

### 4. main.py — lifespan 启动 watcher

watcher 返回 `Observer`（线程对象），不适合放入 `asyncio.create_task` 的后台任务列表。采用与 ss-local proxy 类似的模式——直接在 lifespan 中启动：

```python
# 在 ss-local proxy 任务之后（约 line 253），yield 之前
from app.services.openviking_watcher import start_watcher as _start_ov_watcher
_ov_observer = None
try:
    # ★ 显式传入当前运行的 event loop，避免 start_watcher 内部
    # asyncio.get_running_loop() 在非预期上下文中静默跳过 watcher。
    _ov_observer = _start_ov_watcher(loop=asyncio.get_running_loop())
    if _ov_observer:
        logger.info("[startup] OpenViking watcher started")
except Exception as e:
    logger.warning(f"[startup] OpenViking watcher failed: {e}")

yield

# Shutdown
if _ov_observer:
    _ov_observer.stop()
    _ov_observer.join(timeout=5)
    logger.info("[shutdown] OpenViking watcher stopped")
await close_redis()
```

### 5. agent_tools.py — 扩展索引触发点

```python
# line 2154-2162 改造
# ★ 扩展索引触发点：不仅 memory.md，还包括 focus.md 和 skills/
# WHY：focus.md 包含当前任务上下文，skills/ 包含工具使用经验，
# 这些都是 OpenViking 语义检索应覆盖的内容
if "memory" in path and path.endswith(".md"):
    asyncio.create_task(index_memory_file(str(agent_id), Path(path)))
    logger.info("[OpenViking] write_file 触发索引: agent={} type=memory", str(agent_id)[:8])
elif path.endswith("focus.md") or path.endswith("agenda.md"):
    # ★ focus.md 变更触发索引（工作记忆）
    asyncio.create_task(index_memory_file(str(agent_id), Path(path)))
    logger.info("[OpenViking] write_file 触发索引: agent={} type=focus", str(agent_id)[:8])
elif "skills" in path and path.endswith(".md"):
    # ★ skills 目录变更触发索引（工具使用经验）
    # 统一使用 str(agent_id) 作为 agent scope，确保 caller.py 中 search_memory 时能被检索到
    asyncio.create_task(index_memory_file(str(agent_id), Path(path)))
    logger.info("[OpenViking] write_file 触发索引: agent={} type=skills", str(agent_id)[:8])
```

## 边界条件与异常处理

1. **OV 不可用**：`is_available()` 返回 False → 跳过语义检索，全文 Memory 已在 `build_agent_context` 中注入
2. **检索超时**：`search_memory()` 1.0s 超时，内部捕获异常返回空列表 → 不影响主流程
3. **检索结果为空**：不追加 `## Relevant Memories` 段落，LLM 只看到全文 Memory
4. **watcher 启动失败**：记录 warning，不影响应用启动
5. **Auto-Capture 失败**：fire-and-forget，不影响 LLM 响应返回
6. **Auto-Capture 双触发防护**：`_ov_captured` 标志位保证两条正常完成路径只触发一次
7. **Auto-Capture Session 泄漏**：步骤 2 失败时记录 session_id 便于排查；OV 端 TTL 兜底清理
8. **_fire_extract 失败**：`done_callback` 捕获异常并记录 debug 日志
9. **并发索引**：watcher 使用 debounce（2s）+ `call_soon_threadsafe` + `call_later` 避免频繁保存触发多次索引
10. **跨智能体记忆隔离**：`X-OpenViking-Agent: clawith-{agent_id}` 保证各智能体记忆不泄露

## 数据流路径

```
┌─────────────────────────────────────────────────────┐
│ Auto-Recall（每次 LLM 调用前，仅在 caller.py 触发）    │
│                                                      │
│ 用户消息 → call_llm()                                │
│   ├─ build_agent_context() → static + dynamic_prompt │
│   │   └─ [现有] ## Memory（全文注入 max 2000 chars）  │
│   ├─ search_memory(query) → ## Relevant Memories     │
│   │   ├─ OV 可用 + 有结果 → 追加语义检索片段          │
│   │   └─ OV 不可用/无结果 → 跳过（全文 Memory 兜底）  │
│   └─ LLM 调用（携带全文 Memory + 语义检索补充）       │
├─────────────────────────────────────────────────────┤
│ Auto-Capture（LLM 正常响应完成后，fire-and-forget）   │
│                                                      │
│ LLM 返回最终响应                                      │
│   └─ capture_session_memories()                      │
│       ├─ 创建 OV session                             │
│       ├─ 发送对话内容                                 │
│       └─ 触发 extract → OV 自动提取记忆               │
├─────────────────────────────────────────────────────┤
│ 索引（后台，文件变更时）                               │
│                                                      │
│ 文件变更（memory.md / focus.md / skills/*.md）        │
│   ├─ AgentMemoryWatcher (debounce 2s)                │
│   └─ index_memory_file() [fire-and-forget]           │
│       ├─ 创建 session → 发送内容 → extract            │
│       └─ OV 自动分层（L0/L1/L2）                     │
└─────────────────────────────────────────────────────┘
```

## 影响分析

### 对智能体逻辑的影响

| 场景 | 影响 | 风险 |
|------|------|------|
| 长期记忆 | 语义检索叠加在全文 Memory 上，更精准 | 无——叠加策略，不替代 |
| 自我进化 | Auto-Capture 自动提取 + 全文 Memory 兜底 | 无——双重保障 |
| 工作记忆 | 新增 focus.md 索引，不改变注入逻辑 | 无 |
| 技能 | 新增 skills/ 索引，不改变注入逻辑 | 无 |
| build_agent_context | **不修改**签名和逻辑 | 零——其他 6 个调用点不受影响 |

### 对智能体自我进化的影响

**正面**：
- Auto-Capture 自动提取对话中的有价值信息，弥补 LLM 主动写入 `memory.md` 的遗漏
- 语义检索更精准，减少无关记忆噪声
- OV 官方数据：集成后任务完成率提升 43%，输入 token 成本降低 91%

**无负面风险**：
- 叠加策略保证全文 Memory 始终可见
- Auto-Capture 失败不影响主流程
- 索引延迟时全文注入兜底

### 对多智能体协作的影响

- **跨智能体通信**：`send_message_to_agent` 的 consult 模式会触发 `call_llm`，Auto-Recall 自动生效
- **记忆隔离**：`X-OpenViking-Agent: clawith-{agent_id}` 保证各智能体记忆不泄露
- **Auto-Capture 在 consult 中**：consult 的 `call_llm` 调用也会触发 Auto-Capture，提取跨智能体对话中的有价值信息——这是正确行为
- **无负面影响**：不修改 `send_message_to_agent` 的任何逻辑

### 对回复速度的影响

| 组件 | 额外延迟 | 说明 |
|------|---------|------|
| Auto-Recall | 正常 <200ms，最坏 1.0s | 串行在 build_agent_context 后，超时 1.0s |
| Auto-Capture | 0ms | fire-and-forget，不阻塞响应 |
| watcher | 0ms | 后台线程 |

**结论**：最坏情况增加 1.0s（OV 超时），正常 <200ms。若后续需优化，可将 `build_agent_context` 和 `search_memory` 改为 `asyncio.gather` 并行执行。

## 日志规范

| 前缀 | 级别 | 场景 | 示例 |
|------|------|------|------|
| `[OpenViking]` | info | 检索命中、Capture 触发、watcher 启动、索引触发 | `[OpenViking] Auto-Recall 命中: agent=82e3e5d0 snippets=3 latency=120ms query=如何配置...` |
| `[OpenViking]` | debug | 检索无结果、Capture 失败、降级 | `[OpenViking] Auto-Recall 无结果: agent=82e3e5d0 latency=85ms` |
| `[OpenViking]` | warning | watcher 启动失败、OV 连接丢失 | `[OpenViking] agents_root 不存在，跳过 watcher` |
| `[startup]` | info | watcher 启动（与现有 bg task 日志一致） | `[startup] OpenViking watcher started` |

**必须包含延迟日志**：所有 HTTP 调用记录 `latency=...ms`，方便排查性能问题。

## 代码注释规范

- 所有关键逻辑节点添加**中文注释**，说明 WHY（不是 WHAT）
- 示例：`# ★ 语义检索失败不影响主流程，全文 Memory 已在 build_agent_context 中注入`
- 日志前缀统一使用 `[OpenViking]`，与现有 `[LSP4J]`、`[A2A]` 风格一致

## 环境变量

| 变量 | 默认值 | 说明 | 变更 |
|------|--------|------|------|
| `OPENVIKING_URL` | `http://127.0.0.1:1933` | OV 服务地址 | 无 |
| `OPENVIKING_TIMEOUT` | `1.0` | Auto-Recall / 健康检查超时 | **从 3.0 降到 1.0**（检索宁超时降级，不可阻塞 LLM） |
| `OPENVIKING_CAPTURE_TIMEOUT` | `5.0` | Auto-Capture 每次 turn 发送超时 | **新增**（fire-and-forget，允许宽松超时） |
| `OPENVIKING_LIMIT` | `5` | 检索返回记忆数量上限 | 无 |
| `OPENVIKING_SCORE_THRESHOLD` | `0.35` | 相似度阈值 | 无 |

## 预期结果

1. Auto-Recall：`caller.py` 中自动检索并追加相关记忆（叠加在全文 Memory 上）
2. Auto-Capture：正常完成的会话自动提取有价值信息（fire-and-forget）
3. watcher 启动后自动触发增量索引（线程安全修复）
4. 支持 memory.md、focus.md、skills 等多种内容类型索引
5. OV 不可用时 gracefully 跳过（全文 Memory 兜底）
6. 每次调用额外延迟正常 <200ms，最坏 1.0s
7. `build_agent_context` 签名不变，其他 6 个调用点零影响
8. 保留 MCP 接口为未来迁移预留

## 验证方案

1. 启动应用，确认 `[startup] OpenViking watcher started` 日志
2. 发送消息，确认 `[OpenViking] Auto-Recall 命中` 日志及 latency
3. 停止 OV 服务，发送消息，确认无 `[OpenViking]` 错误，全文 Memory 正常
4. 完成一次对话，确认 `[OpenViking] Auto-Capture 已触发` 日志
5. 修改 memory.md，确认 `[OpenViking] debounce 触发索引` 日志
6. 检查自我进化：写入新知识 → 下次调用中该知识在全文 Memory 中可见
7. 检查多智能体隔离：A 智能体的记忆不出现在 B 的检索结果中
8. 检查其他调用点：heartbeat、scheduler、trigger_daemon 正常工作，无 OV 相关日志
