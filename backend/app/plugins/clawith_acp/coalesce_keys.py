"""ACP 并行读请求 coalesce key — 与 list_dedup LRU 解耦。"""
from __future__ import annotations

METHODS_FOR_COALESCE: frozenset[str] = frozenset({
    "fs/read_text_file",
    "fs/list_directory",
    "fs/search_text",
    "fs/find_file",
    "fs/file_structure",
})


def _s(value: object, default: str = "") -> str:
    if value is None:
        return default
    return str(value)


def normalize_coalesce_key(
    session_id: str,
    method: str,
    path: str,
    args: dict | None = None,
) -> str:
    """返回 in-flight 合并 key；空串表示不参与合并。"""
    args = args or {}
    sid = session_id or ""

    if method == "fs/read_text_file":
        if args.get("line") is not None or args.get("limit") is not None:
            return ""
        return f"{sid}:{method}:{path}"

    if method == "fs/list_directory":
        from app.plugins.clawith_acp.list_dedup import normalize_list_args, _norm_path

        depth, limit = normalize_list_args(args)
        return f"{sid}:{method}:{_norm_path(path)}:{depth}:{limit}"

    if method == "fs/search_text":
        if args.get("cursor"):
            return ""
        return ":".join([
            sid,
            method,
            _s(args.get("query")),
            _s(args.get("filePattern"), "*"),
            _s(args.get("regex"), "false"),
            _s(args.get("caseSensitive"), "true"),
            _s(args.get("context"), "all"),
            _s(args.get("pageSize"), "100"),
        ])

    if method == "fs/find_file":
        if args.get("cursor"):
            return ""
        return ":".join([
            sid,
            method,
            _s(args.get("query")),
            _s(args.get("scope"), "project_files"),
            _s(args.get("pageSize"), "25"),
        ])

    if method == "fs/file_structure":
        from app.plugins.clawith_acp.list_dedup import _norm_path

        return f"{sid}:{method}:{_norm_path(path)}"

    return ""
