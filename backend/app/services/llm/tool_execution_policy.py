"""ACP 工作区写操作工具分类。

被 tool_bridge.py 导入，用于区分读/写工具以控制自主权闸门。
"""

WORKSPACE_WRITE_TOOLS: frozenset[str] = frozenset({
    "write_file",
    "edit_file",
    "delete_file",
    "move_file",
    "refactor_rename",
    "safe_delete",
    "reformat_code",
    "optimize_imports",
    "convert_java_to_kotlin",
})
