"""ACP 工具路由表 — hooks 与 bridge 共用，避免双份 map 漂移。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

# LLM 工具名 → ACP WebSocket method（bridge 执行用）
ACP_METHOD_MAP: dict[str, str] = {
    "read_file": "fs/read_text_file",
    "write_file": "fs/write_text_file",
    "edit_file": "fs/edit_text_file",
    "delete_file": "fs/safe_delete",
    "list_files": "fs/list_directory",
    "find_file": "fs/find_file",
    "search_text": "fs/search_text",
    "find_class": "fs/find_class",
    "find_symbol": "fs/find_symbol",
    "index_status": "ide/index_status",
    "find_references": "fs/find_references",
    "find_definition": "fs/find_definition",
    "find_implementations": "fs/find_implementations",
    "find_super_methods": "fs/find_super_methods",
    "call_hierarchy": "fs/call_hierarchy",
    "type_hierarchy": "fs/type_hierarchy",
    "diagnostics": "fs/diagnostics",
    "refactor_rename": "fs/refactor_rename",
    "move_file": "fs/move_file",
    "reformat_code": "fs/reformat_code",
    "optimize_imports": "fs/optimize_imports",
    "safe_delete": "fs/safe_delete",
    "convert_java_to_kotlin": "fs/convert_java_to_kotlin",
    "sync_files": "ide/sync_files",
    "active_file": "ide/active_file",
    "open_file": "ide/open_file",
    "file_structure": "fs/file_structure",
    "build_project": "ide/build_project",
    "get_documentation": "fs/get_documentation",
    "apply_quickfix": "ide/apply_quickfix",
    "git_status": "git/status",
    "git_diff": "git/diff",
    "git_stage": "git/stage",
    "git_commit": "git/commit",
    "ide/screenshot": "ide/screenshot",
    "ide_screenshot": "ide/screenshot",
    "download_image": "fs/download_image",
}

# hooks 分发用：含 storage 别名（实际参数由 intercept handler 构建）
ACP_TOOL_MAP: dict[str, str] = {
    **ACP_METHOD_MAP,
    "find_files": "fs/list_directory",
    "search_files": "fs/search_text",
}

# ACP 活跃时从 base 工具列表过滤（执行仍可通过别名路由）
ACP_OVERLAP_BASE_TOOL_NAMES: frozenset[str] = frozenset({
    "read_file", "write_file", "edit_file", "delete_file",
    "find_file", "search_text", "list_files", "move_file",
    "find_files", "search_files",
    # ACP IDE 专属工具 — 基础工具中存在同名但实现不同的工具，须过滤避免重名
    "execute_command", "safe_delete",
    "git_status", "git_diff", "git_stage", "git_commit",
    "build_project", "file_structure",
    "tree",
    "execute_bash",
    "download_image",  # 基础工具与 IDE 代理重名，ACP 活跃时过滤基础版
})


@dataclass(frozen=True)
class StorageAliasRoute:
    """agent storage 工具名 → IDE 工具的声明式别名（简单 1:1 映射）。"""

    ide_tool: str
    acp_method: str


STORAGE_ALIAS_ROUTES: dict[str, StorageAliasRoute] = {
    "search_files": StorageAliasRoute(ide_tool="search_text", acp_method="fs/search_text"),
}
