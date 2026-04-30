"""Shared tool constants for LSP4J integration."""

from __future__ import annotations

# LSP4J IDE tool names recognized by the plugin.
LSP4J_IDE_TOOL_NAMES = frozenset(
    {
        "read_file",
        "save_file",
        "run_in_terminal",
        "get_terminal_output",
        "replace_text_by_path",
        "create_file_with_text",
        "delete_file_by_path",
        "get_problems",
        "add_tasks",
        "todo_write",
        "search_replace",
        "list_dir",
        "search_file",
    }
)

# Base tool name -> plugin-native tool name.
TOOL_NAME_MAP = {
    "edit_file": "replace_text_by_path",
    "create_file": "create_file_with_text",
    "write_file": "create_file_with_text",
    "delete_file": "delete_file_by_path",
    "list_files": "list_dir",
    "search_files": "search_file",
}

# Name used in markdown toolCall blocks for plugin-side rendering.
TOOL_DISPLAY_NAME_MAP = {
    "replace_text_by_path": "edit_file",
    "create_file_with_text": "write_file",
    "delete_file_by_path": "delete_file",
    "list_files": "list_dir",
    "search_files": "search_file",
}
