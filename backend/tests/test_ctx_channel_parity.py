import sys
sys.path.insert(0, "/Users/shubinzhang/Documents/agent/Clawith/backend")

import pytest

from app.services.llm.caller import _resolve_ctx_path, current_ctx_path
from app.services.llm.compression_config import is_tool_excluded


def test_resolve_ctx_path_explicit():
    tok = current_ctx_path.set("feishu")
    try:
        assert _resolve_ctx_path() == "feishu"
    finally:
        current_ctx_path.reset(tok)


@pytest.mark.parametrize("tool", ["read_file", "list_files", "retrieve_context"])
def test_tier1_exclude_backend(tool):
    assert is_tool_excluded(tool)
