# Code Review Legacy Fixes — Task Plan

- [x] Task 1: Fix orphaned ping_task in feishu_ws.py
    - 1.1: Add `ping_task: asyncio.Task | None = None` declaration after `retry_delay = 5`
    - 1.2: Add ping_task cancellation in `CancelledError` handler (before `client._disconnect()`)
    - 1.3: Add ping_task cancellation in SOCKS proxy fatal error path (before `client._disconnect()`)
    - 1.4: Add ping_task cancellation + reset in retry path (before `client._disconnect()`)
    - 1.5: Add defensive ping_task cancellation in max-retries-exceeded path (before `client._disconnect()`)

- [x] Task 2: Remove unused imports in feishu_ws.py
    - 2.1: Remove `import threading` (line 5)
    - 2.2: Remove redundant local `import json` at line 132 (inside `elif hasattr(data, "content")` block in `handle_message`)
    - 2.3: Remove redundant local `import json` at line 221 (inside `elif hasattr(data, "content")` block in `_async_handle_message`)

- [x] Task 3: Fix SQLAlchemy boolean comparison in feishu_ws.py
    - 3.1: Change `ChannelConfig.is_configured == True` to `ChannelConfig.is_configured.is_(True)` at line 382

- [x] Task 4: Remove unused import in logging_config.py
    - 4.1: Remove `Optional` from `from typing import Optional, TYPE_CHECKING` at line 10, keep `TYPE_CHECKING`

- [x] Task 5: Verify changes
    - 5.1: Restart clawith and check no import errors
    - 5.2: Verify Feishu WS connects successfully (Connected log appears)
    - 5.3: Verify no regression in log output
