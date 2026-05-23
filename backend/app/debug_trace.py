"""Debug session NDJSON logging.

Usage:
    from app.debug_trace import dbg
    dbg("H1", "caller.py:fn", "msg", {"key": "val"})

Set env CLAWITH_DEBUG_NDJSON=/path/to/file.ndjson to enable file logging.
Otherwise dbg() is a silent no-op.
"""

from __future__ import annotations

import json as _json
import os
import time as _time
from pathlib import Path as _Path

_ENABLED: bool = bool(os.environ.get("CLAWITH_DEBUG_NDJSON"))


def _write_line(line: str) -> None:
    paths: list[_Path] = []
    if env_path := os.environ.get("CLAWITH_DEBUG_NDJSON"):
        paths.append(_Path(env_path))
    for p in paths:
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            with p.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            continue


def dbg(hypothesis_id: str, location: str, message: str, data: dict | None = None) -> None:
    if not _ENABLED:
        return
    payload = {
        "sessionId": "7c2fa2",
        "hypothesisId": hypothesis_id,
        "location": location,
        "message": message,
        "data": data or {},
        "timestamp": int(_time.time() * 1000),
    }
    _write_line(_json.dumps(payload, ensure_ascii=False))
