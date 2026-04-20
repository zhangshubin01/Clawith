# Clawith 日志系统改进

## 需求场景

Clawith 项目的日志系统存在以下问题：

1. **loguru 只配置了 stdout 输出**，没有文件输出 handler，日志无法持久化
2. **没有日志轮转** — 所有 .log 文件会无限增长
3. **日志位置不统一** — 散落在 5+ 个不同路径：
   - 手动启动：`backend/clawith.log`（nohup 重定向）
   - 脚本启动：`.data/log/backend.log`（restart.sh，启动时截断丢失历史）
   - 前端：`.data/log/frontend.log`
   - PostgreSQL：`.pgdata/pg.log`
   - ACP 调试：`.cursor/debug-0afa65.log`（硬编码）
4. **`clawith.log` 被 git 追踪** — 日志文件不应进版本控制
5. **restart.sh 启动时截断日志** — 用 `>` 覆盖，丢失历史
6. **Docker 模式下有 json-file 日志驱动**但本地开发没有等价方案

## 深入分析

### 网络最佳实践要点

1. **12-Factor App 原则**：应用只负责写 stdout，由运行环境（Docker json-file / systemd / nohup）负责持久化。loguru 的文件 handler 是"便捷补充"，不替代 stdout。
2. **loguru 生产配置推荐**：`rotation="00:00"`（每日轮转）、`retention="30 days"`、`compression="gz"`、`enqueue=True`（线程安全队列）。
3. **stdout vs 文件格式分离**：stdout 带颜色（ANSI），文件用纯文本（便于 grep/ELK 解析）。
4. **`diagnose=True` 在生产环境中是安全风险** — 会在 traceback 中暴露变量值（可能含密码、token）。loguru 官方建议生产环境设置 `diagnose=False`。

### 代码逻辑关键发现

#### F1: `configure_logging()` 被调用两次 — 重复注册风险

- **L83**：模块导入时调用 `logger = configure_logging()`
- **L71**：`lifespan()` 中再次调用 `configure_logging()`
- 第二次调用会 `logger.remove()` 清除所有 handler 再重建 — 如果两次配置不一致会导致行为不同
- **解决方案**：使 `configure_logging()` 幂等，且分离 settings 依赖（见 C1）

#### F2: `intercept_standard_logging()` 仅在 lifespan 中调用

- `main.py:72` 中才调用 `intercept_standard_logging()`
- 在 lifespan 之前的 stdlib logging（如 uvicorn 启动日志、SQLAlchemy 日志）不会被 loguru 捕获
- **解决方案**：将 `intercept_standard_logging()` 移入 `configure_logging()` 中，确保在模块导入时就生效

#### F3: `print()` 和 `traceback.print_exc()` 绕过 loguru

- `main.py:162-166`：enterprise_info 迁移使用 `print()` — 3 处
- `main.py:242,258`：`traceback.print_exc()` — 2 处
- 其他 15+ 处 `traceback.print_exc()` 散落在 `trigger_daemon.py`、`dingtalk_stream.py`、`wecom_stream.py`、`websocket.py`、`feishu.py`、`gateway.py`
- 这些输出直接写 stderr，不经过 loguru，nohup 重定向到 `/dev/null` 后会**静默丢失**
- **解决方案**：全部替换为 `logger.exception()`（自动包含 traceback）

#### F4: `diagnose=True` 存在安全隐患

- 当前 `logging_config.py:38` 设置了 `diagnose=True`
- loguru 官方文档明确警告：`diagnose=True` 会在 traceback 中显示变量值，可能泄露敏感信息（密码、token、API key）
- **解决方案**：通过 `LOG_DIAGNOSE` 配置项控制，默认 `False`（生产安全），开发环境可设为 `True`

#### F5: restart.sh 日志路径与 Python 不一致

- `restart.sh:14`：`LOG_DIR="$DATA_DIR/log"` → `.data/log/`（项目相对路径）
- Python `config.py`：`AGENT_DATA_DIR` → `~/.clawith/data/agents`（用户主目录）
- 两者路径策略完全不同，导致日志分散
- **解决方案**：统一为 `~/.clawith/data/log/`，restart.sh 通过 `$HOME/.clawith/data/log/` 引用

#### F6: `clawith.log` 被 git 追踪

- `.gitignore` 中有 `*.log` 但 `clawith.log` 已被 commit，git 仍会追踪
- **解决方案**：`git rm --cached backend/clawith.log`

#### F7: `get_settings()` 循环导入风险

- `logging_config.py` 在模块导入时（L83）执行 `configure_logging()`
- 当前代码故意不从 `app.config` 导入任何东西，避免循环导入
- 如果在 `configure_logging()` 中调用 `get_settings()`，可能触发 `app.config` → `app.core.logging_config` 循环
- **解决方案**：`configure_logging()` 只配 stdout（硬编码默认值），文件 handler 通过独立函数 `configure_file_logging(settings)` 在 lifespan 中调用

## 架构和技术方案

### 核心原则：统一使用 loguru

所有日志输出统一走 loguru，不依赖 nohup 重定向、print()、stdlib logging 等其他通道：

- **文件日志**：由 loguru 文件 handler 管理轮转/保留/压缩
- **stdout 日志**：loguru stdout handler（开发调试、Docker json-file 驱动消费）
- **stdlib logging**：通过 InterceptHandler 转发到 loguru
- **print()**：替换为 logger 调用
- **traceback.print_exc()**：替换为 `logger.exception()`
- **nohup 重定向**：最终改为 `/dev/null`，避免与 loguru 文件 handler 产生重复日志（过渡期保留）

### 环境感知

- **Docker 模式**：不写文件日志，仅 stdout + Docker json-file 驱动（已有轮转配置）
- **本地开发模式**：stdout + 按日期轮转的文件日志

### 日志目录策略

统一使用 `~/.clawith/data/log/`，与 `AGENT_DATA_DIR`（`~/.clawith/data/agents`）同级，保持路径一致。

| 环境 | LOG_DIR | 说明 |
|------|---------|------|
| Docker | `""`（空） | 不写文件，由 Docker 日志驱动管理 |
| 本地开发 | `~/.clawith/data/log` | 与 AGENT_DATA_DIR 同级，唯一确定 |

### 日志文件命名

- 文件格式：`clawith_2026-04-17.log`
- 轮转后：`clawith_2026-04-16.log.gz`（自动压缩）
- 保留策略：30 天

## 过渡设计

采用分阶段过渡，每个阶段独立可验证，出问题可单独回退：

| 阶段 | 内容 | 回退能力 |
|------|------|----------|
| **Phase 1** | 添加 loguru 文件 handler，保留 nohup 重定向不变 | 完全回退：去掉新 handler 即可 |
| **Phase 2** | 替换 `print()`/`traceback.print_exc()` 为 logger 调用 | 独立修改，不影响日志输出 |
| **Phase 3** | nohup 重定向改为 `/dev/null`，`print_info()` 指向 loguru 日志路径 | 改回 nohup 重定向即可 |
| **Phase 4** | 移除 `clawith.log` git 追踪，清理 `.data/log/` 旧文件 | `git rm --cached` 不删本地文件 |

### Phase 1 详细说明

- `configure_logging()` 只配 stdout handler（硬编码默认值，不调用 `get_settings()`）
- 新增 `configure_file_logging(settings)` 函数，在 lifespan 中调用
- 文件 handler 添加后，nohup 重定向仍指向 `backend.log`（暂时产生重复日志，可接受）
- 验证：确认 `~/.clawith/data/log/clawith_YYYY-MM-DD.log` 正常生成和轮转

### Phase 2 详细说明

- `main.py`：3 处 `print()` → `logger.info()`
- 全项目：17+ 处 `traceback.print_exc()` → `logger.exception()`
- 不影响 nohup 重定向，stderr 输出仍被 nohup 捕获
- 验证：grep 确认无残留 `traceback.print_exc()` 和非测试 `print()`

### Phase 3 详细说明

- `restart.sh`：nohup 重定向改为 `>> /dev/null 2>&1`
- 移除 `BACKEND_LOG` 变量和分隔行（`echo "=== ..." >`）
- `print_info()` 中 `tail -f` 提示改为 `~/.clawith/data/log/clawith_$(date +%Y-%m-%d).log`
- 旧的 `.data/log/` 目录不删除（保留历史日志供查阅）
- 验证：重启后确认无 `backend.log` 新增内容，loguru 日志路径正确

### Phase 4 详细说明

- `git rm --cached backend/clawith.log`
- 确认 `.gitignore` 包含 `.cursor/` 和 `*.log`
- `.data/log/` 目录不主动删除（用户可能还需要旧日志）
- 验证：`git status` 确认 `clawith.log` 不再被追踪

## 受影响文件

| 文件 | 修改类型 | 阶段 | 说明 |
|------|----------|------|------|
| `backend/app/config.py` | 修改 | Phase 1 | 新增 LOG_DIR、LOG_LEVEL、LOG_ROTATION、LOG_RETENTION、LOG_COMPRESSION、LOG_DIAGNOSE 配置项 |
| `backend/app/core/logging_config.py` | 修改 | Phase 1 | 添加 `configure_file_logging()`，幂等化（threading.Lock），整合 intercept_standard_logging |
| `backend/app/main.py` | 修改 | Phase 1+2 | lifespan 中调用 `configure_file_logging(settings)`，移除重复调用；`print()`/`traceback.print_exc()` → logger |
| `restart.sh` | 修改 | Phase 3 | nohup 重定向改 `/dev/null`，移除 `BACKEND_LOG`，更新 `print_info()` |
| `backend/clawith.log` | git 操作 | Phase 4 | `git rm --cached` 移除追踪 |
| `.gitignore` | 确认 | Phase 4 | 确保 `.cursor/` 和 `*.log` 在忽略列表 |
| `backend/app/services/trigger_daemon.py` | 修改 | Phase 2 | `traceback.print_exc()` → `logger.exception()`（3 处）|
| `backend/app/services/dingtalk_stream.py` | 修改 | Phase 2 | `traceback.print_exc()` → `logger.exception()`（3 处）|
| `backend/app/services/wecom_stream.py` | 修改 | Phase 2 | `traceback.print_exc()` → `logger.exception()`（2 处）|
| `backend/app/api/websocket.py` | 修改 | Phase 2 | `traceback.print_exc()` → `logger.exception()`（3 处）|
| `backend/app/api/feishu.py` | 修改 | Phase 2 | `traceback.print_exc()` → `logger.exception()`（3 处）|
| `backend/app/api/gateway.py` | 修改 | Phase 2 | `traceback.print_exc()` → `logger.exception()`（1 处）|

## 实现细节

### 1. config.py — 新增配置项（Phase 1）

添加 `_default_log_dir()` 辅助函数：

```python
def _default_log_dir() -> str:
    """Docker 模式返回空（由 json-file 驱动管理），本地模式返回 ~/.clawith/data/log"""
    if _running_in_container():
        return ""
    return str(Path.home() / ".clawith" / "data" / "log")
```

Settings 类新增字段：

```python
# Logging
LOG_DIR: str = _default_log_dir()
LOG_LEVEL: str = "INFO"
LOG_ROTATION: str = "00:00"
LOG_RETENTION: str = "30 days"
LOG_COMPRESSION: str = "gz"
LOG_DIAGNOSE: bool = False  # 生产安全：不在 traceback 中暴露变量值
```

### 2. logging_config.py — 重构（Phase 1）

**关键设计**：`configure_logging()` 不调用 `get_settings()`，避免循环导入。文件 handler 由独立函数 `configure_file_logging(settings)` 在 lifespan 中添加。

```python
import threading

_config_lock = threading.Lock()
_configured: bool = False

_LOG_FORMAT_COLOR = (
    "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
    "<level>{level: <8}</level> | "
    "<cyan>{extra[trace_id]:-<12}</cyan> | "
    "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - "
    "<level>{message}</level>"
)
_LOG_FORMAT_PLAIN = (
    "{time:YYYY-MM-DD HH:mm:ss} | "
    "{level: <8} | "
    "{extra[trace_id]:-<12} | "
    "{name}:{function}:{line} - {message}"
)


def configure_logging():
    """Configure loguru stdout handler. Called at module import time — MUST NOT call get_settings()."""
    global _configured
    with _config_lock:
        if _configured:
            return logger
        _configured = True

    logger.remove()

    # stdout handler with hardcoded defaults (safe for early import)
    logger.add(
        sys.stdout,
        level="INFO",
        format=_LOG_FORMAT_COLOR,
        enqueue=True,
        backtrace=True,
        diagnose=False,
        filter=lambda record: (
            record["extra"].setdefault("trace_id", get_trace_id() or str(uuid4())) is not None
        ),
    )

    # Intercept stdlib logging immediately
    intercept_standard_logging()

    return logger


def configure_file_logging(settings):
    """Add file handler based on settings. Called from lifespan after settings are available."""
    if not settings.LOG_DIR:
        return  # Docker mode: stdout only

    log_path = Path(settings.LOG_DIR)
    try:
        log_path.mkdir(parents=True, exist_ok=True, mode=0o750)
    except OSError:
        logger.warning(f"[logging] Cannot create log dir {log_path}, file logging disabled")
        return

    # Reconfigure stdout level from settings
    # (remove existing stdout handler and re-add with correct level)
    logger.remove(None)  # Remove all handlers
    _add_stdout_handler(settings)
    _add_file_handler(settings, log_path)
    intercept_standard_logging()  # Re-register after remove()


def _add_stdout_handler(settings):
    logger.add(
        sys.stdout,
        level=settings.LOG_LEVEL,
        format=_LOG_FORMAT_COLOR,
        enqueue=True,
        backtrace=True,
        diagnose=settings.LOG_DIAGNOSE,
        filter=lambda record: (
            record["extra"].setdefault("trace_id", get_trace_id() or str(uuid4())) is not None
        ),
    )


def _add_file_handler(settings, log_path: Path):
    logger.add(
        str(log_path / "clawith_{time:YYYY-MM-DD}.log"),
        level=settings.LOG_LEVEL,
        format=_LOG_FORMAT_PLAIN,
        rotation=settings.LOG_ROTATION,
        retention=settings.LOG_RETENTION,
        compression=settings.LOG_COMPRESSION,
        enqueue=True,  # 线程安全（非多进程安全，当前部署为单进程）
        backtrace=True,
        diagnose=settings.LOG_DIAGNOSE,
        filter=lambda record: (
            record["extra"].setdefault("trace_id", get_trace_id() or str(uuid4())) is not None
        ),
        encoding="utf-8",
    )
```

### 3. main.py — lifespan 修改（Phase 1 + Phase 2）

**Phase 1**：添加文件 handler，移除重复调用

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    # File logging configured after settings are available
    configure_file_logging(settings)
    # intercept_standard_logging() already called in configure_logging()
    logger.info("[startup] Logging configured (stdout + file)")
    # ... rest of lifespan
```

**Phase 2**：替换 `print()` 和 `traceback.print_exc()`

- `print(f"[startup] ✅ Migrated enterprise_info ...")` → `logger.info(f"[startup] Migrated enterprise_info ...")`
- `print(f"[startup] ⚠️ enterprise_info migration failed: {e}")` → `logger.warning(f"[startup] enterprise_info migration failed: {e}")`
- `traceback.print_exc()` → `logger.exception()` （全项目 17+ 处）

### 4. restart.sh — Phase 3 修改

```bash
# 修改前
LOG_DIR="$DATA_DIR/log"
BACKEND_LOG="$LOG_DIR/backend.log"
echo "=== ... ===" > "$BACKEND_LOG"
nohup ... >> "$BACKEND_LOG" 2>&1 &

# 修改后
LOG_DIR="$HOME/.clawith/data/log"
# BACKEND_LOG 不再需要，nohup 输出由 loguru 管理
nohup env PYTHONUNBUFFERED=1 \
    PUBLIC_BASE_URL="${PUBLIC_BASE_URL:-}" \
    DATABASE_URL="$DATABASE_URL" \
    .venv/bin/uvicorn app.main:app --host 0.0.0.0 --port $BACKEND_PORT \
    >> /dev/null 2>&1 &
```

`print_info()` 更新：

```bash
echo -e "  Backend log:  tail -f ~/.clawith/data/log/clawith_$(date +%Y-%m-%d).log"
```

### 5. Git 清理（Phase 4）

- `git rm --cached backend/clawith.log`
- 确认 `.gitignore` 包含 `.cursor/` 和 `*.log`

## 边界条件和异常处理

| 场景 | 处理方式 |
|------|----------|
| 日志目录不存在 | `mkdir(parents=True, exist_ok=True, mode=0o750)` 自动创建，设置权限 |
| 日志目录创建失败（权限等） | 优雅降级，仅 stdout 输出，打印 WARNING |
| Docker 模式 | `LOG_DIR=""` 时跳过文件 handler |
| `configure_logging()` 多次调用 | `threading.Lock` + `_configured` 标记幂等，只配置一次 |
| 多线程写入 | loguru `enqueue=True` 内部队列保证线程安全 |
| 多进程写入 | **不支持** — `enqueue=True` 仅线程安全，当前部署为单进程模式 |
| 磁盘满 | loguru 会捕获 OSError，不影响主业务 |
| 首次运行 | 目录自动创建，零配置 |
| `diagnose=False` 生产安全 | traceback 不暴露变量值，开发环境可通过 .env 设为 True |
| 进程硬崩溃（SIGKILL/OOM） | `enqueue=True` 队列中未写入的消息会丢失（可接受的权衡） |
| 循环导入 | `configure_logging()` 不调用 `get_settings()`，文件 handler 延迟到 lifespan 配置 |

## 数据流路径

```
loguru logger (统一入口)
    ├── sys.stdout handler (始终存在，带 ANSI 颜色)
    │   └── 终端直接查看 (开发模式)
    │   └── Docker json-file (Docker 模式)
    │   └── /dev/null (restart.sh 模式，Phase 3 后)
    │
    └── 文件 handler (仅本地开发, LOG_DIR 非空时, 纯文本)
        └── ~/.clawith/data/log/clawith_2026-04-17.log
        └── 轮转: clawith_2026-04-16.log.gz
        └── 30天后自动删除

其他通道 → loguru:
    stdlib logging → InterceptHandler → loguru
    print() → logger.info() (Phase 2)
    traceback.print_exc() → logger.exception() (Phase 2)
```

## 预期结果

1. 每天一个日志文件，自动轮转和压缩
2. 旧日志 30 天后自动清理
3. 日志位置统一为 `~/.clawith/data/log/`
4. `clawith.log` 不再被 git 追踪
5. restart.sh 不再截断历史日志
6. Docker 模式不受影响
7. `configure_logging()` 幂等且线程安全，消除重复配置风险
8. stdlib logging 从模块导入时即被拦截
9. `print()` 和 `traceback.print_exc()` 全部走 loguru，不再静默丢失
10. `diagnose=False` 确保生产环境不泄露敏感变量值
11. 无循环导入风险，`configure_logging()` 可安全在模块导入时执行
12. 过渡期间 nohup 重定向保留，Phase 3 才移除，降低切换风险
