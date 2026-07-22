"""Debug session log — ACP 预算跟踪日志。

将预算消耗事件写入调试日志，用于问题排查。
"""

from __future__ import annotations

import json
import os
import time
from typing import Any

from loguru import logger


def debug_session_log(
    session_id: str,
    event: str,
    **data: Any,
) -> None:
    """记录 ACP 会话调试事件。"""
    entry = {
        "session_id": session_id,
        "timestamp": int(time.time() * 1000),
        "event": event,
        "data": data,
    }
    logger.debug("[ACP-BUDGET] {} {}", event, data)

    debug_path = os.getenv(
        "ACP_DEBUG_LOG_PATH",
        "/tmp/clawith-acp-debug.log",
    )
    try:
        os.makedirs(os.path.dirname(debug_path), exist_ok=True)
        with open(debug_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass
