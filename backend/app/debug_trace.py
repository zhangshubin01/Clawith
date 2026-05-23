"""Optional structured debug tracing (no-op unless CLAWITH_DEBUG_TRACE is set)."""

from __future__ import annotations

import json
import os
from typing import Any

from loguru import logger


def dbg(region: str, location: str, message: str, data: dict[str, Any] | None = None) -> None:
    if not os.environ.get("CLAWITH_DEBUG_TRACE"):
        return
    payload = {"region": region, "location": location, "message": message, "data": data or {}}
    logger.debug("[debug_trace] {}", json.dumps(payload, ensure_ascii=False, default=str))
