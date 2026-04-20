# Logging Improvement — Task Plan

> Source: `.comate/specs/logging-improvement/doc.md`

## Phase 1: Add loguru file handler (keep nohup redirect unchanged)

- [x] **1.1 Add LOG_* settings to `backend/app/config.py`**
    - 1.1.1: Add `_default_log_dir()` helper: Docker → `""`, local → `~/.clawith/data/log`
    - 1.1.2: Add 6 fields to `Settings` class: `LOG_DIR`, `LOG_LEVEL` (`"INFO"`), `LOG_ROTATION` (`"00:00"`), `LOG_RETENTION` (`"30 days"`), `LOG_COMPRESSION` (`"gz"`), `LOG_DIAGNOSE` (`False`)
    - 1.1.3: Reuse existing `_running_in_container()` for Docker detection
    - 1.1.4: Verify: `from app.config import Settings; s = Settings(); print(s.LOG_DIR)` returns expected path

- [x] **1.2 Refactor `backend/app/core/logging_config.py`**
    - 1.2.1: Add `import threading` and module-level `_config_lock = threading.Lock()`, `_configured: bool = False`
    - 1.2.2: Extract format strings to `_LOG_FORMAT_COLOR` and `_LOG_FORMAT_PLAIN` constants
    - 1.2.3: Make `configure_logging()` idempotent: entire function body inside `_config_lock`, check `_configured` flag, set it, then `logger.remove()` + add stdout handler + `intercept_standard_logging()` — all inside the lock
    - 1.2.4: In `configure_logging()`: change `diagnose=True` → `diagnose=False` (hardcoded default, safe for early import, no `get_settings()` call)
    - 1.2.5: Move `intercept_standard_logging()` call into `configure_logging()` so it takes effect at module-import time
    - 1.2.6: Add `configure_file_logging(settings)`: if `settings.LOG_DIR` is empty, return early (Docker mode); else create log dir (`mode=0o750`), remove all handlers, re-add stdout via `_add_stdout_handler(settings)`, add file via `_add_file_handler(settings, log_path)`, re-register intercept. Add comment: "Brief no-handler window during reconfiguration — acceptable at startup only."
    - 1.2.7: Add `_add_stdout_handler(settings)`: same as current stdout config but using `settings.LOG_LEVEL` and `settings.LOG_DIAGNOSE`
    - 1.2.8: Add `_add_file_handler(settings, log_path)`: file handler with `clawith_{time:YYYY-MM-DD}.log` naming, plain format, rotation/retention/compression from settings, `enqueue=True` (thread-safe only, not multi-process safe), `encoding="utf-8"`
    - 1.2.9: Graceful degradation: if log dir creation fails, log WARNING and return (stdout-only)

- [x] **1.3 Update `backend/app/main.py` lifespan for file logging**
    - 1.3.1: Change import to `from app.core.logging_config import configure_logging, configure_file_logging` (remove `intercept_standard_logging`, add `configure_file_logging`)
    - 1.3.2: In `lifespan()`: remove duplicate `configure_logging()` and `intercept_standard_logging()` calls (already done at module import via idempotent `configure_logging()`)
    - 1.3.3: Add `configure_file_logging(settings)` as the first call in lifespan
    - 1.3.4: Update the startup log to: `logger.info("[startup] Logging configured (stdout + file)")`

- [x] **1.4 Phase 1 verification**
    - 1.4.1: Start backend, confirm log file appears under `~/.clawith/data/log/` with correct format (plain text, no ANSI)
    - 1.4.2: Confirm stdout still has colored output
    - 1.4.3: Confirm nohup `backend.log` still receives output (dual logging is OK for Phase 1)
    - 1.4.4: Docker mode: confirm `LOG_DIR=""` when `_running_in_container()` is True, no file handler created
    - 1.4.5: Graceful degradation: set `LOG_DIR` to unwritable path, confirm WARNING logged and stdout works

## Phase 2: Replace print()/traceback.print_exc() with logger calls

- [x] **2.1 Replace `print()` calls in `backend/app/main.py`**
    - 2.1.1: L162: `print(f"[startup] ✅ Migrated ...")` → `logger.info(f"[startup] Migrated ...")`
    - 2.1.2: L164: `print(f"[startup] ℹ️ ... already exists ...")` → `logger.info(f"[startup] ... already exists ...")`
    - 2.1.3: L166: `print(f"[startup] ⚠️ ... migration failed: {e}")` → `logger.warning(f"[startup] ... migration failed: {e}")`
    - 2.1.4: Remove emoji from log messages — loguru level tags replace them

- [x] **2.2 Replace `traceback.print_exc()` in `backend/app/main.py`**
    - 2.2.1: L242: `traceback.print_exception(...)` → `logger.exception(f"[startup] Background task {t.get_name()} CRASHED: {exc}")`
    - 2.2.2: L258: `traceback.print_exc()` → `logger.exception(f"[startup] Background tasks failed: {e}")`
    - 2.2.3: Remove unused `import traceback` if no other references remain

- [x] **2.3 Replace `traceback.print_exc()` in `backend/app/services/trigger_daemon.py`**
    - 2.3.1: Replace 3 occurrences (L694, L707, L862) with `logger.exception()` with appropriate context message
    - 2.3.2: Ensure `from loguru import logger` is imported; remove unused `import traceback`

- [x] **2.4 Replace `traceback.print_exc()` in `backend/app/services/dingtalk_stream.py`**
    - 2.4.1: Replace 3 occurrences (L127, L135, L157) with `logger.exception()` with context message
    - 2.4.2: Ensure logger is imported; remove unused `import traceback`

- [x] **2.5 Replace `traceback.print_exc()` in `backend/app/services/wecom_stream.py`**
    - 2.5.1: Replace 2 occurrences (L171, L271) with `logger.exception()` with context message
    - 2.5.2: Ensure logger is imported; remove unused `import traceback`

- [x] **2.6 Replace `traceback.print_exc()` in `backend/app/api/websocket.py`**
    - 2.6.1: Replace 3 occurrences (L273, L599, L685) with `logger.exception()` with context message
    - 2.6.2: Ensure logger is imported; remove unused `import traceback`

- [x] **2.7 Replace `traceback.print_exc()` in `backend/app/api/feishu.py`**
    - 2.7.1: Replace 3 occurrences (L1572, L1577, L1609) with `logger.exception()` with context message
    - 2.7.2: Ensure logger is imported; remove unused `import traceback`

- [x] **2.8 Replace `traceback.print_exc()` in `backend/app/api/gateway.py`**
    - 2.8.1: Replace 1 occurrence (L467) with `logger.exception()` with context message
    - 2.8.2: Ensure logger is imported; remove unused `import traceback`

- [x] **2.9 Phase 2 verification**
    - 2.9.1: `grep -rn 'traceback\.print_exc' backend/app/ --include='*.py' | grep -v __pycache__ | grep -v /test` — expect zero results
    - 2.9.2: `grep -rn '^\s*print(' backend/app/main.py` — expect zero results
    - 2.9.3: Start backend, confirm `logger.exception()` output includes full traceback (not just the message)

## Phase 3: Change nohup redirect to /dev/null, update print_info()

- [x] **3.1 Update nohup redirect in `restart.sh`**
    - 3.1.1: Hardcode `FRONTEND_LOG="$DATA_DIR/log/frontend.log"` (decouple from `LOG_DIR`, keep frontend log path unchanged)
    - 3.1.2: Change `LOG_DIR="$DATA_DIR/log"` → `LOG_DIR="$HOME/.clawith/data/log"` (now safe since `FRONTEND_LOG` no longer depends on it)
    - 3.1.3: Remove `BACKEND_LOG="$LOG_DIR/backend.log"` variable assignment
    - 3.1.4: Remove the separator line: `echo "=== Clawith backend log started at ... ===" > "$BACKEND_LOG"`
    - 3.1.5: Change nohup redirect from `>> "$BACKEND_LOG" 2>&1 &` to `>> /dev/null 2>&1 &`

- [x] **3.2 Update `print_info()` in `restart.sh`**
    - 3.2.1: Change `Backend log:  tail -f $BACKEND_LOG` → `Backend log:  tail -f ~/.clawith/data/log/clawith_$(date +%Y-%m-%d).log`
    - 3.2.2: Change `ACP traces:   tail -f $BACKEND_LOG | rg '\\[ACP\\]'` → `ACP traces:   tail -f ~/.clawith/data/log/clawith_$(date +%Y-%m-%d).log | rg '\\[ACP\\]'`
    - 3.2.3: Keep `Frontend log` line unchanged (still uses `$FRONTEND_LOG`)

- [x] **3.3 Phase 3 verification**
    - 3.3.1: Start backend via `restart.sh`, confirm no new content in `.data/log/backend.log`
    - 3.3.2: Confirm loguru file log at `~/.clawith/data/log/clawith_YYYY-MM-DD.log` contains complete startup logs
    - 3.3.3: Confirm `print_info()` output points to the new log path
    - 3.3.4: Confirm `.data/log/` directory is NOT deleted (historical logs preserved)
    - 3.3.5: Confirm frontend log still writes to `.data/log/frontend.log` (unchanged)

## Phase 4: Remove clawith.log from git, confirm .gitignore

- [x] **4.1 Add `.cursor/` to `.gitignore`**
    - 4.1.1: Add `.cursor/` line to `.gitignore`
    - 4.1.2: Verify: `grep -n '\.cursor' .gitignore` shows the new entry

- [x] **4.2 Remove `backend/clawith.log` from git tracking**
    - 4.2.1: Run `git rm --cached backend/clawith.log`
    - 4.2.2: Verify: `git ls-files backend/clawith.log` returns empty
    - 4.2.3: Verify: local file `backend/clawith.log` still exists on disk (not deleted)

- [x] **4.3 Phase 4 verification**
    - 4.3.1: `git status` confirms `clawith.log` is no longer tracked
    - 4.3.2: `.gitignore` contains both `.cursor/` and `*.log`
    - 4.3.3: `git add .` does not re-add `clawith.log`
    - 4.3.4: Confirm `.data/log/` directory is NOT deleted (historical logs preserved)
