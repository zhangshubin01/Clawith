import sys
sys.path.insert(0, '/Users/shubinzhang/Documents/agent/Clawith/backend')

import json
import pytest

from app.services.llm.client import LLMMessage
from app.services.llm.read_lifecycle import ReadLifecycleManager, FileOperation


def _msgs_read_then_edit():
    return [
        LLMMessage(role="assistant", content="read", tool_calls=[{
            "id": "r1",
            "function": {"name": "read_file", "arguments": json.dumps({"file_path": "a.py"})},
        }]),
        LLMMessage(role="tool", content="line1\nline2\n" + "x" * 600, tool_call_id="r1"),
        LLMMessage(role="assistant", content="edit", tool_calls=[{
            "id": "e1",
            "function": {"name": "edit_file", "arguments": json.dumps({"file_path": "a.py"})},
        }]),
        LLMMessage(role="tool", content="ok", tool_call_id="e1"),
    ]


@pytest.mark.asyncio
async def test_stale_read_replaced_with_ccr_marker(monkeypatch):
    async def _store(**kwargs):
        return "a" * 64
    monkeypatch.setattr("app.services.llm.read_lifecycle.store_entry", _store)
    mgr = ReadLifecycleManager()
    msgs = _msgs_read_then_edit()
    out = await mgr.apply_async(
        msgs, session_id="s1", agent_id="a1", ctx_path="ws", tools_available=True,
    )
    tool = [m for m in out.messages if m.role == "tool" and m.tool_call_id == "r1"][0]
    assert "<!-- ccr:" in tool.content
    assert "stale" in tool.content.lower()


def test_read_covers_full_over_partial():
    later = FileOperation(1, "b", "read_file", "f.py", "read", read_offset=None, read_limit=None)
    earlier = FileOperation(0, "a", "read_file", "f.py", "read", read_offset=10, read_limit=50)
    assert ReadLifecycleManager._read_covers(later, earlier)


@pytest.mark.asyncio
async def test_frozen_prefix_not_replaced(monkeypatch):
    async def _store(**kwargs):
        raise AssertionError("should not store frozen")
    monkeypatch.setattr("app.services.llm.read_lifecycle.store_entry", _store)
    mgr = ReadLifecycleManager()
    msgs = _msgs_read_then_edit()
    out = await mgr.apply_async(
        msgs, session_id="s1", agent_id="a1", ctx_path="ws", frozen_message_count=4,
    )
    assert out.messages[1].content.startswith("line1")
