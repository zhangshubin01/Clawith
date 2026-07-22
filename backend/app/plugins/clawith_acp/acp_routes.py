"""ACP 工具路由表 — hooks 与 bridge 共用，避免双份 map 漂移。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

# LLM 工具名 → ACP WebSocket method（bridge 执行用）
ACP_METHOD_MAP: dict[str, str] = {
    "acp_read_file": "fs/read_text_file",
    "acp_write_file": "fs/write_text_file",
    "acp_edit_file": "fs/edit_text_file",
    "acp_delete_file": "fs/safe_delete",
    "acp_list_files": "fs/list_directory",
    "acp_find_file": "fs/find_file",
    "acp_search_text": "fs/search_text",
    "acp_find_class": "fs/find_class",
    "acp_find_symbol": "fs/find_symbol",
    "acp_index_status": "ide/index_status",
    "acp_find_references": "fs/find_references",
    "acp_find_definition": "fs/find_definition",
    "acp_find_implementations": "fs/find_implementations",
    "acp_find_super_methods": "fs/find_super_methods",
    "acp_call_hierarchy": "fs/call_hierarchy",
    "acp_type_hierarchy": "fs/type_hierarchy",
    "acp_diagnostics": "fs/diagnostics",
    "acp_refactor_rename": "fs/refactor_rename",
    "acp_move_file": "fs/move_file",
    "acp_reformat_code": "fs/reformat_code",
    "acp_optimize_imports": "fs/optimize_imports",
    "acp_safe_delete": "fs/safe_delete",
    "acp_convert_java_to_kotlin": "fs/convert_java_to_kotlin",
    "acp_sync_files": "ide/sync_files",
    "acp_active_file": "ide/active_file",
    "acp_open_file": "ide/open_file",
    "acp_file_structure": "fs/file_structure",
    "acp_build_project": "ide/build_project",
    "acp_get_documentation": "fs/get_documentation",
    "acp_apply_quickfix": "ide/apply_quickfix",
    "acp_git_status": "git/status",
    "acp_git_diff": "git/diff",
    "acp_git_stage": "git/stage",
    "acp_git_commit": "git/commit",
    "acp_ide_screenshot": "acp_ide_screenshot",
    "acp_ide_screenshot": "acp_ide_screenshot",
}

# hooks 分发用：含 storage 别名（实际参数由 intercept handler 构建）
ACP_TOOL_MAP: dict[str, str] = {
    **ACP_METHOD_MAP,
    "acp_find_files": "fs/list_directory",
    "acp_search_files": "fs/search_text",
}

# ACP 活跃时从 base 工具列表过滤（执行仍可通过别名路由）
ACP_OVERLAP_BASE_TOOL_NAMES: frozenset[str] = frozenset({
    "acp_read_file", "acp_write_file", "acp_edit_file", "acp_delete_file",
    "acp_find_file", "acp_search_text", "acp_list_files", "acp_move_file",
    "acp_find_files", "acp_search_files",
    # ACP IDE 专属工具 — 基础工具中存在同名但实现不同的工具，须过滤避免重名
    "acp_execute_command", "acp_safe_delete",
    "acp_git_status", "acp_git_diff", "acp_git_stage", "acp_git_commit",
    "acp_build_project", "acp_file_structure",
    "tree",
    "execute_bash",
})


@dataclass(frozen=True)
class StorageAliasRoute:
    """agent storage 工具名 → IDE 工具的声明式别名（简单 1:1 映射）。"""

    ide_tool: str
    acp_method: str


STORAGE_ALIAS_ROUTES: dict[str, StorageAliasRoute] = {
    "acp_search_files": StorageAliasRoute(ide_tool="acp_search_text", acp_method="fs/search_text"),
}

# 工具名 → ACP ToolKind 映射
ACP_KIND_MAP: dict[str, str] = {
    "acp_read_file": "read", "acp_write_file": "edit", "acp_edit_file": "edit",
    "acp_delete_file": "delete", "acp_execute_command": "execute", "acp_bash": "execute",
    "acp_find_class": "search", "acp_find_symbol": "search", "acp_index_status": "read",
    "acp_find_references": "search", "acp_find_definition": "search",
    "acp_find_implementations": "search", "acp_find_super_methods": "search",
    "acp_call_hierarchy": "search", "acp_type_hierarchy": "search",
    "acp_diagnostics": "read",
    "acp_refactor_rename": "edit", "acp_move_file": "edit",
    "acp_reformat_code": "edit", "acp_optimize_imports": "edit",
    "acp_safe_delete": "delete", "acp_convert_java_to_kotlin": "edit",
    "acp_sync_files": "edit",
    "acp_active_file": "read", "acp_open_file": "edit",
    "acp_file_structure": "read",
    "acp_find_file": "search",
    "acp_search_text": "search",
    "acp_list_files": "read",
    "acp_build_project": "edit",
    "acp_get_documentation": "read", "acp_apply_quickfix": "edit",
    "acp_git_status": "read", "acp_git_diff": "read", "acp_git_stage": "edit", "acp_git_commit": "edit",
    "acp_ide_screenshot": "read",
}

# 工具名 → 中文显示名
ACP_TOOL_CN_NAME: dict[str, str] = {
    "acp_read_file": "读取文件", "acp_write_file": "写入文件", "acp_edit_file": "编辑文件",
    "acp_delete_file": "删除文件", "acp_execute_command": "执行命令", "acp_bash": "终端",
    "acp_find_class": "搜索类", "acp_find_symbol": "搜索符号", "acp_index_status": "索引进度",
    "acp_find_references": "查找引用", "acp_find_definition": "查找定义",
    "acp_find_implementations": "查找实现", "acp_find_super_methods": "查找父方法",
    "acp_call_hierarchy": "调用层次", "acp_type_hierarchy": "类型层次",
    "acp_diagnostics": "诊断", "acp_refactor_rename": "重命名", "acp_move_file": "移动文件",
    "acp_reformat_code": "格式化", "acp_optimize_imports": "优化导入",
    "acp_safe_delete": "安全删除", "acp_convert_java_to_kotlin": "Java→Kotlin",
    "acp_sync_files": "同步文件", "acp_active_file": "活动文件", "acp_open_file": "打开文件",
    "acp_file_structure": "文件结构", "acp_find_file": "查找文件", "acp_search_text": "文本搜索",
    "acp_list_files": "列出目录",
    "acp_build_project": "构建项目", "acp_get_documentation": "查看文档",
    "acp_apply_quickfix": "应用修复",
    "acp_git_status": "Git状态", "acp_git_diff": "Git差异", "acp_git_stage": "Git暂存", "acp_git_commit": "Git提交",
    "acp_find_files": "查找文件", "acp_search_files": "文本搜索",
    "acp_ide_screenshot": "IDE截图",
}
