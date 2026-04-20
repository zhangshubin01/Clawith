# lark-oapi SDK 日志降噪

## 需求场景

lark-oapi SDK 的 WebSocket 连接/断线/重连日志在生产环境中占 40%（432 行中 173 行），且存在三个问题：
1. **凭据泄露**：每次连接/断开都打印完整 URL（含 `access_key` 和 `ticket`）
2. **级别不当**：正常断线重连的 "receive message loop exit" 标记为 ERROR
3. **Docker 双重日志**：Docker 模式下 "Lark" logger 双重输出（SDK StreamHandler + loguru InterceptHandler）

## 根因分析

lark-oapi SDK 使用单个 stdlib logger `"Lark"`，日志由 `ws.Client` 发出：

| 消息 | 级别 | 来源 | 改后可见 |
|------|------|------|----------|
| `connected to wss://...`（含凭据） | INFO | `_connect()` L158 | 不可见 |
| `disconnected to wss://...`（含凭据） | INFO | `_disconnect()` L323 | 不可见 |
| `trying to reconnect for the Nth time` | INFO | `_try_connect()` L306 | 不可见 |
| `ping failed, err: ...` | WARN | `_ping_loop()` L137 | 可见 |
| `receive message loop exit, err: ...` | ERROR | `_receive_message_loop()` L173 | 可见 |
| `connect failed, err: ...` | ERROR | `start()`/`_try_connect()` L116/311 | 可见 |

**根因 1**：`feishu_ws.py` 创建 WS Client 时传入 `log_level=lark.LogLevel.INFO`，SDK 将 `"Lark"` logger 级别设为 INFO，导致所有连接状态日志通过 InterceptHandler 进入 loguru。SDK 默认级别是 WARNING。

**根因 2（Docker 双重日志）**：import 顺序导致 Docker 模式下 "Lark" logger 双重输出：
1. `main.py` import `logging_config` → `configure_logging()` → `_intercept_standard_logging()` — 此时 lark-oapi **还未** import，"Lark" logger 不存在
2. `main.py` import `feishu` router → lark_oapi 被 import → "Lark" logger 创建，带自己的 `StreamHandler(sys.stdout)` + `propagate=True`
3. 有 `LOG_DIR` 时：`configure_file_logging()` 再次调用 `_intercept_standard_logging()`，替换 "Lark" handler → 正常
4. **无 `LOG_DIR`（Docker）**：不再调用 `_intercept_standard_logging()` → "Lark" logger 保留 SDK 的 StreamHandler + 传播到 root → **每条日志输出两次**

## 技术方案

### 改动 1：修改 WS Client log_level 参数

将 `lark.LogLevel.INFO` 改为 `lark.LogLevel.WARNING`（2 处），恢复 SDK 默认行为。

### 改动 2：补充 Clawith 侧连接成功日志

改为 WARNING 后 SDK 的 `connected to wss://...` 被静默，但当前 Clawith 侧**没有**连接成功的确认日志（`logger.info("[Feishu WS] Starting async WS client...")` 是连接**之前**的）。需要在 `await client._connect()` 成功后添加日志。

### 改动 3：修复 Docker 双重日志

在 `_intercept_standard_logging()` 末尾显式处理 `"Lark"` logger：移除 SDK 自己的 StreamHandler，替换为 InterceptHandler，禁止传播。

## 受影响文件

| 文件 | 修改类型 | 说明 |
|------|----------|------|
| `backend/app/services/feishu_ws.py` | 修改 | log_level 改 WARNING（2 处）+ 补充连接成功日志 |
| `backend/app/core/logging_config.py` | 修改 | `_intercept_standard_logging()` 中处理 "Lark" logger |

## 实现细节

### 1. feishu_ws.py — WS Client log_level 修改 + 连接成功日志

```python
# L278-283: 首次创建 — 改 log_level
client = ws.Client(
    app_id,
    app_secret,
    event_handler=event_handler,
    log_level=lark.LogLevel.WARNING,  # was .INFO — suppress routine connect/disconnect logs
)

# L306 之后: 补充连接成功日志
if _no_proxy_ctx:
    async with _no_proxy_ctx():
        await client._connect()
else:
    await client._connect()
logger.info(f"[Feishu WS] Connected for agent {agent_id}")  # 新增：SDK connected 日志被静默后需 Clawith 侧确认

# L337-342: 重连时重新创建 — 改 log_level
client = ws.Client(
    app_id,
    app_secret,
    event_handler=event_handler,
    log_level=lark.LogLevel.WARNING,  # was .INFO
)
```

### 2. logging_config.py — 修复 Docker 模式双重日志

在 `configure_file_logging()` 的 Docker 早返回分支中调用 `_intercept_standard_logging()`，确保 Docker 模式下也能拦截后加载的 logger（如 lark-oapi 的 "Lark" logger）。

**原理**：lark-oapi 在 `from app.api.feishu import router` 时（`main.py` 模块级 import）加载，此时早于 `configure_file_logging()` 调用。有 `LOG_DIR` 时 `configure_file_logging()` 会再次调用 `_intercept_standard_logging()`，能正确拦截；Docker 模式下当前直接 return，遗漏了 "Lark" logger。

**安全性**：`_intercept_standard_logging()` 内部使用 `logging.basicConfig(force=True)` 和 `handlers = [handler]` 替换模式，多次调用不会累积 handler。

```python
def configure_file_logging(settings: Settings) -> None:
    global _file_logging_configured
    with _config_lock:
        if _file_logging_configured:
            return
        _file_logging_configured = True

        if not settings.LOG_DIR:
            # Docker mode: still need to re-intercept for loggers loaded after
            # the initial configure_logging() call (e.g., lark-oapi "Lark")
            _intercept_standard_logging()
            return

        # ... rest unchanged
```

## 边界条件

| 场景 | 处理方式 |
|------|----------|
| 正常连接成功 | SDK 静默，Clawith 侧新增 `logger.info("[Feishu WS] Connected for agent...")` |
| 正常断线重连 | SDK 静默，自动重连无需日志 |
| ping 失败 | SDK WARNING 可见 |
| 连接失败（DNS/SSL 错误） | SDK ERROR 可见 |
| 消息循环异常退出（包括正常断线） | SDK ERROR 可见（SDK 设计问题，所有断线都打 ERROR） |
| 最终重连失败 | Clawith 侧 `logger.error("[Feishu WS] Max retries exceeded...")` 已有 |
| Docker 模式 | 修复双重日志，Docker 分支调用 `_intercept_standard_logging()` 拦截后加载的 logger |
| REST Client 共享 "Lark" logger | `lark.Client.build()` 也设 WARNING，不冲突 |

## 预期结果

1. 飞书 WS 连接/断线/重连日志不再刷屏（减少约 100+ 行/天）
2. `access_key` / `ticket` 凭据不再泄露到日志
3. Docker 模式下不再双重日志
4. 异常（ping 失败、连接失败、消息循环退出）仍可见
5. Clawith 侧关键连接状态仍可追踪（新增 Connected 日志）
