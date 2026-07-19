"""list_files 全通道去重缓存 — 进程级 LRU + 写后失效。

供 tool_bridge（ACP/IDE）与 agent_tools（storage 回落）共用。
"""
from __future__ import annotations

import os
import time
from collections import OrderedDict
from typing import Any

from loguru import logger

_LS_CACHE_TTL = float(os.getenv("LS_CACHE_TTL", "60"))
_LS_CACHE_MAX_ENTRIES = int(os.getenv("LS_CACHE_MAX_ENTRIES", "2000"))
_LIST_SESSION_TTL = float(os.getenv("LIST_SESSION_TTL", "300"))

_ls_cache: OrderedDict[str, tuple[float, str]] = OrderedDict()

LIST_CACHE_INVALIDATE_TOOLS = frozenset({
    "write_file", "edit_file", "move_file", "delete_file", "sync_files",
    "safe_delete", "refactor_rename", "reformat_code", "optimize_imports",
    "convert_java_to_kotlin",
})


def normalize_list_args(args: dict | None) -> tuple[int, int]:
    args = args or {}
    depth = max(1, min(int(args.get("depth", 3) or 3), 10))
    limit = max(10, min(int(args.get("limit", 500) or 500), 2000))
    return depth, limit


def _norm_path(path: str | None) -> str:
    return (path or ".").replace("\\", "/").rstrip("/") or "."


def normalize_list_key(session_id: str, cwd: str, path: str, args: dict | None = None) -> str:
    depth, limit = normalize_list_args(args)
    return f"{session_id}:{cwd}:{_norm_path(path)}:{depth}:{limit}"


def _evict_lru_if_needed() -> None:
    while len(_ls_cache) > _LS_CACHE_MAX_ENTRIES:
        _ls_cache.popitem(last=False)


def get_cached_list(key: str) -> str | None:
    if _LIST_SESSION_TTL <= 0:
        return None
    entry = _ls_cache.get(key)
    if entry is None:
        return None
    ts, val = entry
    if time.monotonic() - ts >= _LS_CACHE_TTL:
        _ls_cache.pop(key, None)
        return None
    _ls_cache.move_to_end(key)
    return val


def store_list_result(key: str, value: str) -> None:
    if _LIST_SESSION_TTL <= 0:
        return
    _ls_cache[key] = (time.monotonic(), value)
    _ls_cache.move_to_end(key)
    _evict_lru_if_needed()


def _cached_path_from_key(key: str, session_id: str, cwd: str) -> str | None:
    prefix = f"{session_id}:{cwd}:"
    if not key.startswith(prefix):
        return None
    rest = key[len(prefix):]
    parts = rest.rsplit(":", 2)
    if len(parts) < 3:
        return None
    return parts[0]


def invalidate_list_cache_for_path(session_id: str, cwd: str, dir_path: str) -> None:
    target = _norm_path(dir_path)
    keys_to_drop: list[str] = []
    for key in list(_ls_cache.keys()):
        cached_path = _cached_path_from_key(key, session_id, cwd)
        if cached_path is None:
            continue
        if (
            cached_path == target
            or target.startswith(cached_path + "/")
            or cached_path.startswith(target + "/")
        ):
            keys_to_drop.append(key)
    for key in keys_to_drop:
        _ls_cache.pop(key, None)
    if keys_to_drop:
        logger.info(
            "[LIST-DEDUP] invalidated session={} dir={} keys={}",
            (session_id or "")[:8],
            os.path.basename(target),
            len(keys_to_drop),
        )


def format_list_directory_result(result: Any) -> str:
    if isinstance(result, dict):
        entries = result.get("entries") or result.get("files") or []
        if not entries:
            return "(目录为空)"
        lines: list[str] = []
        for e in entries:
            if isinstance(e, dict):
                prefix = "📁 " if e.get("isDirectory") else "📄 "
                lines.append(f"{prefix}{e.get('name', '?')}")
            else:
                lines.append(str(e))
        if result.get("truncated"):
            lines.append(f"... (截断, 共 {result.get('totalCount', '?')} 项)")
        return "\n".join(lines)
    return str(result)


def summarize_listing_for_dedup(content: str) -> tuple[int, list[str]]:
    lines = [ln for ln in content.splitlines() if ln.strip() and not ln.startswith("...")]
    names: list[str] = []
    for ln in lines[:5]:
        name = ln.strip()
        if name.startswith("📁 ") or name.startswith("📄 "):
            name = name[2:].strip()
        names.append(name)
    return len(lines), names

_AGENT_DEBUG_LOG = os.getenv("AGENT_DEBUG_LOG_PATH")


def agent_debug_log(
    location: str,
    message: str,
    hypothesis_id: str,
    data: dict | None = None,
    run_id: str = "pre-fix",
) -> None:
    """Debug 会话 NDJSON — 仅当 AGENT_DEBUG_LOG_PATH 设置时写入，失败静默。"""
    if not _AGENT_DEBUG_LOG:
        return
    try:
        import json

        payload = {
            "sessionId": "7cda9a",
            "runId": run_id,
            "hypothesisId": hypothesis_id,
            "location": location,
            "message": message,
            "data": data or {},
            "timestamp": int(time.time() * 1000),
        }
        with open(_AGENT_DEBUG_LOG, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except Exception:
        pass
