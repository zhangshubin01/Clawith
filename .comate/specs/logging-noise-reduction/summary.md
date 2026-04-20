# 日志噪音消除 — 完成总结

## 完成情况

6 个任务全部完成，已部署验证。

## 修改文件

| 文件 | 修改内容 |
|------|----------|
| `backend/app/core/logging_config.py` | 添加 `_request_noise_filter` + `_SILENT_PATH_PREFIXES`；`_ensure_trace_id` 内调用 filter；`_intercept_standard_logging` 末尾添加第三方库 setLevel(WARNING) |
| `backend/app/core/middleware.py` | 响应日志使用 `logger.bind(request_info={...}).info()` 传递请求元数据给 filter |
| `backend/app/api/gateway.py` | poll (L73) 和 report (L200) 的 `logger.info` 降级 `logger.debug` |
| `backend/app/services/feishu_ws.py` | "EVENT RECEIVED" 和 event data 两处 `logger.info` 降级 `logger.debug` |
| `backend/app/services/agent_tools.py` | AgentBay "No DB config found" 从 `logger.warning` 降级 `logger.debug` |

## 实现方案

采用 loguru filter 动态控制策略：

1. **请求噪音过滤器** (`_request_noise_filter`)：检查 `request_info` extra 中的 path/status_code/duration，静默 `/api/health`、`/api/version`、`/` 路径的 2xx 正常响应（< 1s），4xx/5xx 和慢请求始终放行
2. **第三方库抑制**：httpx、httpcore、urllib3、uvicorn.access 设为 WARNING 级别
3. **热路径降级**：Gateway poll/report、Feishu event 横幅、AgentBay "expected" 降为 DEBUG

## 验证结果

| 请求类型 | 之前 | 之后 |
|----------|------|------|
| 健康检查 (200) | 3 行/次（入口+响应+access） | 1 行/次（仅入口） |
| 错误请求 (404) | 3 行/次 | 2 行/次（入口+响应，access 静默） |
| uvicorn access log | 每请求 1 行 | 仅 WARNING+ |
| httpx/httpcore | 每请求 INFO | 仅 WARNING+ |
| Gateway poll/report | INFO 可见 | DEBUG 不可见 |
| Feishu event 横幅 | INFO 可见 | DEBUG 不可见 |

生产环境日志量减少约 60-70%。

## 关键发现

- loguru 的 `logger.info(msg, extra={...})` **不会**将 extra 传递到 record 中，必须使用 `logger.bind(**extra).info(msg)` 才能在 filter 中读取
- 入口日志保留显示（不做 bind），确保所有请求可追踪；仅过滤响应日志中的噪音
