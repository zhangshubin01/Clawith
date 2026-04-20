# Agent 性能优化任务计划

## 修改文件清单

| 任务 | 文件 | 优先级 |
|------|------|--------|
| 1. Context 文件缓存 | `backend/app/services/agent_context.py` | P0 |
| 2. 工具定义缓存 | `backend/app/services/agent_tools.py` | P0 |
| 3. 工具并行执行 | `backend/app/services/llm/caller.py` | P1 |
| 4. Chunk 批处理 | `backend/app/api/websocket.py` | P1 |

---

## Task 1: Context 文件缓存实现

### 1.1 任务目标
- 为 `build_agent_context()` 添加细粒度文件缓存
- 静态文件 (soul.md, skills/, relationships.md) 缓存
- 动态部分 (Feishu 上下文, Tenant settings, Triggers, Time) 每次计算

### 1.2 实现步骤
1. 添�模块级 `_context_file_cache: dict[str, tuple[str, float]]`
2. 添加 `_MAX_CACHE_SIZE = 100` 限制
3. 实现 `_read_file_cached(agent_id, filename, max_chars)` 函数
4. 使用 `asyncio.Lock` 保护并发
5. 实现 mtime 检查失效
6. 修改 `build_agent_context()` 使用缓存读取

### 1.3 代码位置
- `agent_context.py:22` 附近 (现有 `_read_file_safe`)
- `agent_context.py:152` (build_agent_context 函数)

---

## Task 2: 工具定义缓存实现

### 2.1 任务目标
- 为 `get_agent_tools_for_llm()` 添加缓存
- 完整 cache key 包含所有动态参数

### 2.2 实现步骤
1. 添加 `_tools_def_cache: dict[tuple, tuple[list, float]]`
2. 实现 `_make_tools_cache_key()` 包含 (agent_id, has_feishu, has_any_channel, a2a_async, os_type)
3. 添加 `_TOOLS_CACHE_TTL = 60`
4. 修改 `get_agent_tools_for_llm()` 先检查缓存
5. 添加 LRU 驱逐逻辑

### 2.3 代码位置
- `agent_tools.py:1838` 附近 (现有 get_agent_tools_for_llm 函数)

---

## Task 3: 工具并行执行

### 3.1 任务目标
- 为 caller.py 添加保守的并行执行支持
- 只读工具并行，写工具串行

### 3.2 实现步骤
1. 定义 `_READONLY_TOOLS = {"read_file", "list_files", ...}`
2. 实现 `_execute_tools_parallel(tool_calls)` 函数
3. 添加 `asyncio.gather()` 并行执行只读工具
4. 保留串行执行写工具
5. 处理异常正确返回

### 3.3 代码位置
- `caller.py:423` 附近 (现有 tool call 循环)

---

## Task 4: Chunk 批处理

### 4.1 任务目标
- 为 websocket.py 添加 chunk 累积 + 超时发送

### 4.2 实现步骤
1. 添加 `_chunk_buffer: list[str]`
2. 添加 `_last_flush_time` 记录时间
3. 设置 `_FLUSH_THRESHOLD = 3`
4. 设置 `_FLUSH_TIMEOUT_MS = 100`
5. 修改 `stream_to_ws()` 实现双阈值 flush

### 4.3 代码位置
- `websocket.py:448` 附近 (现有 stream_to_ws 函数)

---

## 验收 checklist

- [x] 1.1 成功添加 Context 文件缓存
- [x] 1.2 缓存支持 mtime 失效
- [x] 1.3 缓存大小限制 100
- [x] 1.4 并发保护正常
- [x] 2.1 成功添加工具定义缓存
- [x] 2.2 完整 cache key 正确
- [x] 2.3 TTL 60 秒生效
- [x] 3.1 只读工具并行执行
- [x] 3.2 写工具保持串行
- [x] 3.3 异常处理正确
- [x] 4.1 Chunk 累积 3 个发送
- [x] 4.2 100ms 超时发送
- [x] 4.3 不影响前端渲染
