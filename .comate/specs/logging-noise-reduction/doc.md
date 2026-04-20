# 日志噪音消除

## 需求场景

日志系统改进已上线，但运行日志中噪音过多，影响排查效率。安全问题（CRITICAL）单独处理，本次只做噪音消除。

### 噪音问题

| ID | 严重度 | 文件 | 行号 | 问题 |
|----|--------|------|------|------|
| N1 | HIGH | `middleware.py` | 28, 41 | TraceIdMiddleware 每请求 2 行 INFO，健康检查和高频请求刷屏 |
| N2 | HIGH | `gateway.py` | 73, 200 | 每次轮询/上报都记录 INFO |
| N3 | HIGH | `feishu_ws.py` | 106 | 每条飞书事件 "EVENT RECEIVED" INFO 横幅 |
| N4 | HIGH | 第三方库 | — | httpx/httpcore 每次请求都记录 INFO |
| N5 | MEDIUM | `agent_tools.py` | 160 | AgentBay "expected" 条件用 WARNING 级别 |
| N6 | MEDIUM | uvicorn.access | — | access log 与 TraceIdMiddleware 重复 |
| N7 | MEDIUM | `feishu_ws.py` | 108 | 每条飞书消息完整 event data 写入 INFO |

## 架构和技术方案

### 核心策略：loguru filter 动态控制

用 filter 精细控制日志输出，而非简单降级到 DEBUG（否则生产环境完全看不到请求日志）。

| 日志来源 | 生产环境 INFO 级行为 | 实现方式 |
|----------|----------------------|----------|
| middleware 正常请求（2xx, < 1s） | 静默 | filter 检查 status_code + duration |
| middleware 异常请求（4xx/5xx 或慢请求 ≥ 1s） | 记录 | filter 放行 |
| uvicorn access log | 静默 | `setLevel(WARNING)` |
| httpx/httpcore | 静默 | `setLevel(WARNING)` |
| AgentBay "expected" | 静默 | 降级 `logger.debug()` |
| Feishu "EVENT RECEIVED" + event data | 静默 | 降级 `logger.debug()` |
| Gateway poll | 静默 | 降级 `logger.debug()` |
| Gateway report 正常 | 静默 | 降级 `logger.debug()` |
| Gateway report 错误 | 记录 | 保留 `logger.error/warning` |

## 受影响文件

| 文件 | 修改类型 | 说明 |
|------|----------|------|
| `backend/app/core/logging_config.py` | 修改 | 添加 `_request_noise_filter`，第三方库 setLevel |
| `backend/app/core/middleware.py` | 修改 | N1: 请求日志附加 extra 字段，供 filter 使用 |
| `backend/app/api/gateway.py` | 修改 | N2: poll/report 降级 DEBUG |
| `backend/app/services/feishu_ws.py` | 修改 | N3, N7: EVENT RECEIVED + event data 降级 DEBUG |
| `backend/app/services/agent_tools.py` | 修改 | N5: AgentBay "expected" 降级 DEBUG |

## 实现细节

### 1. logging_config.py — 请求噪音过滤器 + 第三方库控制

添加请求噪音过滤器，静默正常请求（2xx 且 < 1s），仅记录异常请求：

```python
# 静默的路径前缀（健康检查、轮询等高频低价值请求）
_SILENT_PATH_PREFIXES = ("/api/health", "/api/version", "/")

def _request_noise_filter(record) -> bool:
    """Filter noisy request logs: silence normal 2xx responses < 1s.

    Non-request logs (without request_extra) always pass through.
    Abnormal requests (4xx/5xx, slow ≥ 1s) always pass through.
    """
    req = record["extra"].get("request_info")
    if req is None:
        return True  # Non-request log, always pass

    status = req.get("status_code", 0)
    duration = req.get("duration", 0)
    path = req.get("path", "")

    # Always log errors and slow requests
    if status >= 400 or duration >= 1.0:
        return True

    # Silence health checks and root path
    if any(path.startswith(p) for p in _SILENT_PATH_PREFIXES):
        return False

    # Normal request: pass through (still visible at INFO level)
    return True
```

在 `_add_stdout_handler` 和 `_add_file_handler` 中使用 `filter=_request_noise_filter`（替代 `_ensure_trace_id`）。

修改 `_ensure_trace_id` 使其在设置 trace_id 的同时仍返回 True，然后作为 filter 链的前置：

```python
def _ensure_trace_id(record) -> bool:
    """Filter that ensures trace_id is always set in log records."""
    record["extra"].setdefault("trace_id", get_trace_id() or str(uuid4()))
    return _request_noise_filter(record)
```

第三方库日志级别控制（在 `_intercept_standard_logging()` 末尾）：

```python
# Suppress noisy third-party loggers
for name in ("httpx", "httpcore", "urllib3"):
    logging.getLogger(name).setLevel(logging.WARNING)

# Uvicorn access log redundant with TraceIdMiddleware
logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
```

### 2. middleware.py — 请求日志附加 extra 字段

在 TraceIdMiddleware 中将请求信息写入 `extra["request_info"]`，供 filter 判断：

```python
# Before:
logger.info(f"--> {request.method} {request.url.path} [client: {client_host}]")
...
logger.info(f"<-- {request.method} {request.url.path} {response.status_code} {duration:.3f}s")

# After:
logger.info(f"--> {request.method} {request.url.path} [client: {client_host}]")
...
logger.info(
    f"<-- {request.method} {request.url.path} {response.status_code} {duration:.3f}s",
    extra={"request_info": {
        "path": request.url.path,
        "method": request.method,
        "status_code": response.status_code,
        "duration": duration,
    }},
)
```

注意：只有响应日志需要附加 extra（filter 只需判断响应状态），请求入口日志保持不变。

### 3. gateway.py — poll/report 降级 DEBUG

```python
# L73:
logger.debug(f"[Gateway] poll called, key_prefix={x_api_key[:8]}...")
# L200:
logger.debug(f"[Gateway] report called, key_prefix={x_api_key[:8]}..., msg_id={body.message_id}")
```

### 4. feishu_ws.py — 事件日志降级 DEBUG

```python
# L106:
logger.debug(f"[Feishu WS] EVENT RECEIVED for agent {agent_id}")
# L108:
logger.debug(f"[Feishu WS] Received event: type={type(data)}")
```

### 5. agent_tools.py — AgentBay "expected" 降级 DEBUG

```python
# L160:
logger.debug(f"[ToolConfig] No DB config found for {tool_name}, agent_id={agent_id}")
```

## 边界条件

| 场景 | 处理方式 |
|------|----------|
| httpx 请求失败 | WARNING 级别仍记录，不丢错误 |
| uvicorn 非 2xx 响应 | WARNING+ 仍记录 |
| 正常 API 请求（非健康检查） | 仍记录（filter 放行） |
| 慢请求（≥ 1s） | 强制记录（filter 放行） |
| 错误请求（4xx/5xx） | 强制记录（filter 放行） |
| DEBUG 模式 | 所有降级日志可见 |
| filter 无 request_info 的日志 | 全部放行（不影响业务日志） |

## 预期结果

1. 健康检查、根路径不再刷屏
2. 正常 API 请求仍记录（可排查）
3. 异常请求（4xx/5xx/慢请求）强制记录
4. httpx、uvicorn.access 仅记录 WARNING+
5. AgentBay "expected"、Feishu 事件横幅不再刷屏
6. 生产环境日志量减少约 60-70%
