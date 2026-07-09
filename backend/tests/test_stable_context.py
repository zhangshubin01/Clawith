import sys
sys.path.insert(0, "/Users/shubinzhang/Documents/agent/Clawith/backend")

from app.services.llm.stable_context import (
    build_ccr_system_appendix,
    get_retrieve_context_tool_definition,
    stable_json_dumps,
)


def test_stable_appendix_bytes_identical():
    a = build_ccr_system_appendix()
    b = build_ccr_system_appendix()
    assert a == b
    assert "CCR" in a


def test_retrieve_tool_definition_stable():
    d1 = stable_json_dumps(get_retrieve_context_tool_definition())
    d2 = stable_json_dumps(get_retrieve_context_tool_definition())
    assert d1 == d2
    assert "retrieve_context" in d1
