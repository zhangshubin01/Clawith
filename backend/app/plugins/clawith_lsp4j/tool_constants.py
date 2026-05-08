"""LSP4J IDE 工具常量定义。

定义插件端可识别的工具名称、名称映射及显示名称，
供 JSON-RPC 路由、tool hooks 及工具调用同步使用。
"""

from __future__ import annotations

# ── IDE 工具名称集合 ──────────────────────────────────────────────
# LSP4J 插件端（通义灵码 IDE 插件）能识别并处理的工具名称。
# 用于 toolCall/sync 的合法性校验，不在集合中的工具名会被标记为 UNKNOWN。
LSP4J_IDE_TOOL_NAMES = frozenset(
    {
        "read_file",              # 读取文件
        "run_in_terminal",        # 终端运行命令
        "get_terminal_output",    # 获取终端输出
        "replace_text_by_path",   # 路径替换文本（edit_file 的插件原生实现）
        "create_file_with_text",  # 创建文件并写入文本
        "delete_file_by_path",    # 按路径删除文件
        "get_problems",           # 获取 IDE 诊断问题
        "add_tasks",              # 添加任务
        "todo_write",             # 写入待办事项
        "search_replace",         # 搜索替换
        "list_dir",               # 列出目录
        "search_file",            # 搜索文件
        "grep_code",              # 代码搜索（grep）
        "search_codebase",        # 代码库搜索
        "search_symbol",          # 符号搜索
        "save_file",              # 保存文件
        "update_tasks",           # 更新任务
        "apply_patch",            # 应用补丁
    }
)

# ── 工具名称映射 ──────────────────────────────────────────────────
# 基础工具名 → 插件原生工具名。
# Clawith 后端使用的通用工具名与插件端能识别的具体工具名之间的映射关系。
TOOL_NAME_MAP = {
    "edit_file": "replace_text_by_path",     # 编辑文件 → 路径替换文本
    "create_file": "create_file_with_text",  # 创建文件 → 创建文件并写入文本
    "write_file": "create_file_with_text",   # 写入文件 → 创建文件并写入文本（同 create_file）
    "delete_file": "delete_file_by_path",    # 删除文件 → 按路径删除文件
    "list_files": "list_dir",                # 列出文件 → 列出目录
    "search_files": "search_file",           # 搜索文件 → 搜索文件
}

# ── 工具显示名称映射 ──────────────────────────────────────────────
# 用于 Markdown toolCall 代码块渲染时的显示名称。
# 插件 ToolPanel 的文件分支切换逻辑会检查 raw toolName 是否匹配预期字符串，
# 因此显示名称需要与插件端的判断逻辑保持一致。
TOOL_DISPLAY_NAME_MAP = {
    "replace_text_by_path": "edit_file",
    # 插件 ToolPanel 文件分支切换检查 raw string toolName，期望 "create_file"。
    # 若发送 "write_file"，将无法稳定进入文件工具分支。
    "create_file_with_text": "create_file",
    "write_file": "create_file",
    "delete_file_by_path": "delete_file",
    "list_files": "list_dir",
    "search_files": "search_file",
    "search_replace": "search_replace",
}
