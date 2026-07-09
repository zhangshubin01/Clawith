"""ACP 会话历史内存缓存失效 — tool 写入 DB 后须立即使 load_history 缓存失效。"""

from __future__ import annotations

from typing import Callable

_listeners: list[Callable[[str], None]] = []


def register_history_cache_invalidator(fn: Callable[[str], None]) -> None:
    _listeners.append(fn)


def invalidate_history_cache(session_id: str) -> None:
    if not session_id:
        return
    for fn in _listeners:
        try:
            fn(session_id)
        except Exception:
            pass
