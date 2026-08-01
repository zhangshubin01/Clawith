"""search_text / find_file 结果 LRU — 默认关闭，需 SEARCH_CACHE_TTL>0 启用。"""
from __future__ import annotations

import os
import time
from collections import OrderedDict
from typing import Any

from loguru import logger

_SEARCH_CACHE_TTL = float(os.getenv("SEARCH_CACHE_TTL", "0"))
_SEARCH_CACHE_MAX_ENTRIES = int(os.getenv("SEARCH_CACHE_MAX_ENTRIES", "500"))

_search_cache: OrderedDict[str, tuple[float, str]] = OrderedDict()


def search_cache_enabled() -> bool:
    return _SEARCH_CACHE_TTL > 0


def _evict_lru_if_needed() -> None:
    while len(_search_cache) > _SEARCH_CACHE_MAX_ENTRIES:
        _search_cache.popitem(last=False)


def normalize_search_key(method: str, session_id: str, params: dict[str, Any]) -> str:
    """与 coalesce_keys 语义对齐的 LRU key。"""
    from app.plugins.clawith_acp.coalesce_keys import normalize_coalesce_key

    return normalize_coalesce_key(session_id, method, "", params)


def get_cached_search(key: str) -> str | None:
    if not search_cache_enabled():
        return None
    entry = _search_cache.get(key)
    if entry is None:
        return None
    ts, val = entry
    if time.monotonic() - ts >= _SEARCH_CACHE_TTL:
        _search_cache.pop(key, None)
        return None
    _search_cache.move_to_end(key)
    return val


def store_search_result(key: str, value: str) -> None:
    if not search_cache_enabled():
        return
    _search_cache[key] = (time.monotonic(), value)
    _search_cache.move_to_end(key)
    _evict_lru_if_needed()


def invalidate_search_cache_for_session(session_id: str) -> None:
    """写操作后清空该 session 的搜索缓存（避免编辑后脏读）。"""
    if not _search_cache:
        return
    prefix = f"{session_id or ''}:"
    keys = [k for k in list(_search_cache.keys()) if k.startswith(prefix)]
    for k in keys:
        _search_cache.pop(k, None)
    if keys:
        logger.info("[SEARCH-DEDUP] invalidated session={} keys={}", (session_id or "")[:8], len(keys))
