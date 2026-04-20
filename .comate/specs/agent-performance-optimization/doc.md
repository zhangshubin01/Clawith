# Agent 性能优化规范文档 (修订版)

## 1. 需求概述

优化 Clawith 智能体的执行性能，减少首字节时间 (TTFB) 和整体响应延迟。

**核心目标**:
- 首字节时间 (TTFB) 减少 200-500ms
- 工具定义加载缓存化
- Agent Context 构建缓存化
- 工具并行执行支持

---

## 2. Planner 审查发现的问题

### 2.1 遗漏的关键点 ⚠️

| 问题 | 描述 | 影响 |
|------|------|------|
| **动态 Context 未识别** | Feishu/DingTalk/Atlassian 上下文依赖 DB，不是真正的静态内容 | 缓存会导致渠道工具不出现/不消失 |
| **Tenant 设置查询** | 每次查询 TenantSetting/SystemSetting | 性能瓶颈 |
| **ChannelConfig DB 查询** | 每次构建 context 都需要 | 开销未处理 |

### 2.2 兼容性问题

| 问题 | 描述 |
|------|------|
| **Context 缓存 key 简单** | cache_key = agent_id，但实际依赖 has_feishu, has_any_channel, _a2a_async, computer_os_type |
| **tools 缓存 key 缺失** | 工具定义依赖动态参数，只用 agent_id 不够 |

### 2.3 边界情况遗漏

| 边界情况 | 风险 |
|----------|------|
| Cache stampede | 10 个请求同时 miss 缓存 → 同时构建，反而更慢 |
| 无限内存增长 | 1000 agent × 100KB = 100MB+，无清理机制 |
| 缓存并发写入 | 模块级 dict 非线程安全 |
| Agent 被删除 | 缓存数据残留 |

---

## 3. 修订后的技术方案

### 3.1 Context 构建 - 细粒度缓存

```python
# 只缓存真正的静态文件内容 (按文件 mtime 失效)
_context_file_cache: dict[str, tuple[str, float]] = {}  # key: "agent_id:filename"
_CONTEXT_CACHE_TTL = 300  # 5 分钟，兜底 TTL

# 不缓存的部分 (每次重新计算):
# - Feishu/DingTalk/Atlassian 上下文 (依赖 ChannelConfig)
# - Tenant settings
# - Active triggers
# - Current time

# 策略: 分离静态缓存 + 动态计算
def _read_file_cached(agent_id, filename, max_chars=3000):
    """按文件缓存，支持 mtime 失效"""
    ws = _agent_workspace(agent_id)
    filepath = ws / filename
    cache_key = f"{agent_id}:{filename}"

    if filepath.exists():
        mtime = filepath.stat().st_mtime
        if cache_key in _context_file_cache:
            content, cached_mtime = _context_file_cache[cache_key]
            if cached_mtime == mtime:
                return content

    content = _read_file_safe(filepath, max_chars)
    mtime = filepath.stat().st_mtime if filepath.exists() else 0
    _context_file_cache[cache_key] = (content, mtime)
    return content
```

### 3.2 工具定义缓存 - 完整 key

```python
# 扩展 cache key 包含所有动态参数
_tools_def_cache: dict[tuple, tuple[list, float]] = {}
_TOOLS_CACHE_TTL = 60  # 60 秒

def _make_tools_cache_key(agent_id, has_feishu, has_any_channel, a2a_async, os_type):
    """完整的工具缓存 key"""
    return (str(agent_id), has_feishu, has_any_channel, a2a_async, os_type)
```

### 3.3 LRU 缓存 + 并发保护

```python
# 使用 functools.lru_cache 或自定义 LRU
from functools import lru_cache
import asyncio

# 并发保护
_cache_lock = asyncio.Lock()

# 缓存大小限制
_MAX_CACHE_SIZE = 100  # 最多缓存 100 个 agent
```

### 3.4 工具并行执行 - 保守策略

```python
# 只对只读工具并行，写工具保持串行
_READONLY_TOOLS = {"read_file", "list_files", "search_in_files", "jina_search", "web_search"}

async def _execute_tools_parallel(tool_calls):
    """保守的并行策略"""

    # 按��型分组
    readonly = [tc for tc in tool_calls if tc["function"]["name"] in _READONLY_TOOLS]
    writable = [tc for tc in tool_calls if tc["function"]["name"] not in _READONLY_TOOLS]

    results = {}

    # 只读工具可以并行
    if readonly:
        tasks = [_process_tool_call(tc, ...) for tc in readonly]
        readonly_results = await asyncio.gather(*tasks, return_exceptions=True)
        for tc, result in zip(readonly, readonly_results):
            results[tc["id"]] = result

    # 写工具保持串行
    for tc in writable:
        results[tc["id"]] = await _process_tool_call(tc, ...)

    return results
```

### 3.5 Chunk 批处理 - 超时保护

```python
# 累积 + 超时双阈值
_chunk_buffer: list[str] = []
_last_flush_time = None
_FLUSH_THRESHOLD = 3
_FLUSH_TIMEOUT_MS = 100  # 100ms 超时

async def stream_to_ws(text: str):
    _chunk_buffer.append(text)
    now = datetime.now()

    should_flush = (
        len(_chunk_buffer) >= _FLUSH_THRESHOLD or
        (_last_flush_time and (now - _last_flush_time).total_seconds() * 1000 >= _FLUSH_TIMEOUT_MS)
    )

    if should_flush:
        await _flush_chunks()
```

### 3.6 Cache Stampede 防护

```python
# 使用单锁防止 cache stampede
_cache_locks: dict[str, asyncio.Lock] = {}

async def _get_cached_value(key: str, builder):
    """带 stampede 保护的缓存获取"""

    if key in _cache:
        return _cache[key]

    # 为这个 key 创建锁 (如果不存在)
    if key not in _cache_locks:
        _cache_locks[key] = asyncio.Lock()

    async with _cache_locks[key]:
        # Double-check
        if key in _cache:
            return _cache[key]

        # 构建并缓存
        value = await builder()
        _cache[key] = value
        return value
```

---

## 4. 缓存失效策略

| 缓存类型 | 失效条件 | TTL | 备注 |
|---------|---------|-----|------|
| Context 静态文件 | 文件 mtime 变化 | 5 分钟 | 兜底 |
| 工具定义 | 显式刷新或 TTL | 60 秒 | 需完整 key |
| Chunk buffer | 累积 3 个或 100ms | - | 双阈值 |
| LRU 驱逐 | 缓存满时驱逐最旧 | - | maxsize=100 |

---

## 5. 验收标准

### 5.1 性能指标

- [ ] 首次请求 TTFB 不增加
- [ ] 缓存命中后 TTFB 减少 ≥200ms
- [ ] 连续请求平均延迟减少 30%

### 5.2 功能验证

- [ ] Context 构建正确 (skills 列表完整)
- [ ] 渠道配置变更后工具正确出现/消失 (动态部分不缓存)
- [ ] 工具定义正确 (工具数量一致)
- [ ] 并行执行结果与串行一致 (只读工具)
- [ ] Chunk 批处理不影响前端渲染

### 5.3 边界情况

- [ ] 文件变更后缓存正确失效 (mtime 检查)
- [ ] 无 Agent workspace 时不崩溃
- [ ] 并行执行异常正确处理
- [ ] Cache stampede 不发生 (锁保护)
- [ ] 内存不无限增长 (LRU maxsize=100)

### 5.4 风险控制

- [ ] 使用 asyncio.Lock 保护并发写入
- [ ] 动态内容不缓存 (每次重新计算)
- [ ] 完整 cache key 避免脏数据