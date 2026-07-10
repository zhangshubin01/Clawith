"""acp_routes 单元测试。"""
import sys

sys.path.insert(0, "/Users/shubinzhang/Documents/agent/Clawith/backend")

from app.plugins.clawith_acp.acp_routes import (
    ACP_OVERLAP_BASE_TOOL_NAMES,
    ACP_TOOL_MAP,
    STORAGE_ALIAS_ROUTES,
)


def test_overlap_includes_storage_search_tools():
    assert "search_files" in ACP_OVERLAP_BASE_TOOL_NAMES
    assert "find_files" in ACP_OVERLAP_BASE_TOOL_NAMES


def test_tool_map_includes_aliases():
    assert ACP_TOOL_MAP["search_files"] == "fs/search_text"
    assert ACP_TOOL_MAP["find_files"] == "fs/list_directory"


def test_storage_alias_routes():
    assert STORAGE_ALIAS_ROUTES["search_files"].ide_tool == "search_text"
