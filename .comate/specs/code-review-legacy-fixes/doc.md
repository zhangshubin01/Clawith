# Code Review Legacy Fixes

## Requirement

Fix pre-existing issues found during code review of recent logging/SDK changes:

1. **HIGH**: Orphaned `ping_task` in feishu_ws.py — never cancelled on disconnect/retry, leaking zombie async tasks
2. **MEDIUM**: Unused imports — `threading` and redundant local `import json` in feishu_ws.py; `Optional` in logging_config.py
3. **MEDIUM**: SQLAlchemy boolean comparison `== True` — non-idiomatic pattern (53 instances codebase-wide; fix feishu_ws.py only in this spec)

## Architecture & Technical Approach

### Issue 1: Orphaned ping_task

**File**: `backend/app/services/feishu_ws.py`, method `_run_async_client()` (inside `start_client()`)

**Root cause**: `ping_task = asyncio.create_task(client._ping_loop())` is created after connection but never tracked or cancelled. When the WebSocket drops or retry occurs, the exception handler calls `client._disconnect()` but never cancels `ping_task`. The old task keeps running against the disconnected client until it fails internally, consuming resources and generating spurious error logs.

**Fix**: Declare `ping_task` as a tracked variable (`ping_task: asyncio.Task | None = None`). Cancel and await it at every exit/retry point:
- `CancelledError` path (clean shutdown)
- Fatal error path (SOCKS proxy error)
- Retry path (before re-creating client)
- Reset `ping_task = None` after cancellation

**Cancellation pattern** (safe, recommended by asyncio docs):
```python
if ping_task and not ping_task.done():
    ping_task.cancel()
    try:
        await ping_task
    except asyncio.CancelledError:
        pass
```

### Issue 2: Unused imports

**feishu_ws.py**:
- Remove `import threading` (line 5) — never used anywhere in the file
- Remove redundant local `import json` at lines 132 and 221 — module-level `import json` (line 4) covers all usages. The local re-imports inside `elif` blocks shadow the module-level import unnecessarily.

**logging_config.py**:
- Remove `Optional` from `from typing import Optional, TYPE_CHECKING` (line 10) — never used. `TYPE_CHECKING` must be preserved.

### Issue 3: SQLAlchemy boolean comparison

**Scope**: 53 instances of `column == True` / `column == False` found across the codebase. This spec fixes only the instance in feishu_ws.py (the file already being modified).

**Change**: `ChannelConfig.is_configured == True` → `ChannelConfig.is_configured.is_(True)` at line 382.

**Note**: `== True` generates `column = true` in SQL; `.is_(True)` generates `column IS true`. For NOT NULL boolean columns they are functionally equivalent, but `.is_()` is the SQLAlchemy-idiomatic pattern and avoids PEP 8 E712 lint warnings. The remaining 52 instances should be addressed in a separate spec.

## Affected Files

| File | Modification Type | Changes |
|------|------------------|---------|
| `backend/app/services/feishu_ws.py` | Edit | Track & cancel ping_task; remove `import threading`; remove redundant local `import json` (2 places); fix `== True` → `.is_(True)` |
| `backend/app/core/logging_config.py` | Edit | Remove unused `Optional` import |

## Implementation Details

### feishu_ws.py `_run_async_client()` — ping_task lifecycle

```python
async def _run_async_client():
    nonlocal client
    retry_count = 0
    max_retries = 3
    retry_delay = 5
    ping_task: asyncio.Task | None = None      # ← ADD: track ping_task

    while retry_count < max_retries:
        try:
            if _no_proxy_ctx:
                async with _no_proxy_ctx():
                    await client._connect()
            else:
                await client._connect()
            logger.info(f"[Feishu WS] Connected for agent {agent_id}")
            ping_task = asyncio.create_task(client._ping_loop())

            while True:
                await asyncio.sleep(3600)
        except asyncio.CancelledError:
            logger.info(f"[Feishu WS] Async client task cancelled for {agent_id}")
            if ping_task and not ping_task.done():    # ← ADD
                ping_task.cancel()
                try:
                    await ping_task
                except asyncio.CancelledError:
                    pass
            await client._disconnect()
            return
        except Exception as e:
            error_str = str(e)
            if "python-socks is required to use a SOCKS proxy" in error_str:
                logger.error(f"[Feishu WS] Connection failed for {agent_id}: {error_str}")
                logger.error("[Feishu WS] To use SOCKS proxy with Feishu WebSocket, please install python-socks: pip install python-socks[socksio]")
                if ping_task and not ping_task.done():  # ← ADD
                    ping_task.cancel()
                    try:
                        await ping_task
                    except asyncio.CancelledError:
                        pass
                await client._disconnect()
                self._clients.pop(agent_id, None)
                return

            retry_count += 1
            logger.exception(f"[Feishu WS] Async client exception for {agent_id}: {e} (retry {retry_count}/{max_retries})")
            if ping_task and not ping_task.done():      # ← ADD
                ping_task.cancel()
                try:
                    await ping_task
                except asyncio.CancelledError:
                    pass
            ping_task = None                             # ← ADD: reset reference
            await client._disconnect()
            if retry_count < max_retries:
                logger.info(f"[Feishu WS] Trying to reconnect in {retry_delay} seconds...")
                await asyncio.sleep(retry_delay)
                try:
                    event_handler = self._create_event_handler(agent_id)
                    client = ws.Client(
                        app_id,
                        app_secret,
                        event_handler=event_handler,
                        log_level=lark.LogLevel.WARNING,
                    )
                    self._clients[agent_id] = client
                except Exception as create_err:
                    logger.exception(f"[Feishu WS] Failed to recreate client for {agent_id}: {create_err}")
                    retry_count = max_retries
                    break

    if retry_count >= max_retries:
        logger.error(f"[Feishu WS] Max retries ({max_retries}) exceeded for {agent_id}, stopping reconnections")
        # ping_task is guaranteed None here (retry path always cancels and resets it),
        # but cancel defensively for future-proofing
        if ping_task and not ping_task.done():
            ping_task.cancel()
            try:
                await ping_task
            except asyncio.CancelledError:
                pass
        await client._disconnect()
        self._clients.pop(agent_id, None)
```

### feishu_ws.py — import cleanup

```python
# BEFORE (lines 3-5):
import asyncio
import json
import threading

# AFTER:
import asyncio
import json
```

Remove local `import json` at ~line 132 and ~line 221 (inside `elif hasattr(data, "content")` blocks).

### feishu_ws.py — SQLAlchemy boolean fix

```python
# BEFORE (line 382):
ChannelConfig.is_configured == True,

# AFTER:
ChannelConfig.is_configured.is_(True),
```

### logging_config.py — unused import

```python
# BEFORE (line 10):
from typing import Optional, TYPE_CHECKING

# AFTER:
from typing import TYPE_CHECKING
```

## Boundary Conditions & Exception Handling

- `ping_task.cancel()` + `await ping_task` may raise `CancelledError` — explicitly caught and swallowed
- `ping_task` may already be done (if it failed internally before the outer exception) — guarded by `not ping_task.done()`
- `ping_task` is `None` on first iteration before connection — guarded by `if ping_task`
- All fixes are independent — no cross-dependencies between the 4 changes

## Expected Outcomes

- No more zombie `ping_task` instances after Feishu WS reconnections
- Clean import list in both files (no unused imports, no redundant local imports)
- Idiomatic SQLAlchemy boolean comparison in feishu_ws.py
- No functional behavior changes — all fixes are structural/code quality only
