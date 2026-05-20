"""OpenAI tool 消息配对修复：孤立 tool / 缺失 tool 结果。"""

from app.services.llm.caller import (
    _repair_openai_tool_call_pairing,
    _sanitize_orphan_tool_messages,
)
from app.services.llm.utils import LLMMessage


def _assistant_with_tools(*call_ids: str) -> LLMMessage:
    return LLMMessage(
        role="assistant",
        content=None,
        tool_calls=[
            {
                "id": call_id,
                "type": "function",
                "function": {"name": "read_file", "arguments": "{}"},
            }
            for call_id in call_ids
        ],
    )


def _tool_result(call_id: str, content: str = "ok") -> LLMMessage:
    return LLMMessage(role="tool", content=content, tool_call_id=call_id)


def test_sanitize_removes_orphan_tool_without_assistant_tool_calls():
    """孤立 tool（前一条不是 assistant tool_calls）应被删除。"""
    messages = [
        LLMMessage(role="system", content="sys"),
        LLMMessage(role="user", content="hi"),
        LLMMessage(role="assistant", content="plain text"),
        _tool_result("call_orphan"),
    ]
    removed = _sanitize_orphan_tool_messages(messages)
    assert removed == 1
    assert all(m.role != "tool" for m in messages)


def test_sanitize_keeps_valid_assistant_tool_chain():
    """assistant + 连续 tool 结果应保留。"""
    messages = [
        LLMMessage(role="system", content="sys"),
        _assistant_with_tools("call_a", "call_b"),
        _tool_result("call_a"),
        _tool_result("call_b"),
        LLMMessage(role="user", content="next"),
    ]
    removed = _sanitize_orphan_tool_messages(messages)
    assert removed == 0
    assert [m.tool_call_id for m in messages if m.role == "tool"] == ["call_a", "call_b"]


def test_repair_inserts_missing_tool_results():
    """assistant 声明了 tool_calls 但缺 tool 结果时应自动补齐。"""
    messages = [
        LLMMessage(role="system", content="sys"),
        _assistant_with_tools("call_x", "call_y"),
        _tool_result("call_x"),
    ]
    _repair_openai_tool_call_pairing(messages)
    tool_ids = [m.tool_call_id for m in messages if m.role == "tool"]
    assert tool_ids == ["call_x", "call_y"]
    assert messages[-1].content == "[工具未返回结果，已跳过]"


def test_repair_sanitizes_then_fills():
    """先删孤立 tool，再补缺失 tool。"""
    messages = [
        LLMMessage(role="system", content="sys"),
        LLMMessage(role="assistant", content="no tools"),
        _tool_result("orphan"),
        _assistant_with_tools("call_ok"),
    ]
    _repair_openai_tool_call_pairing(messages)
    assert all(m.tool_call_id != "orphan" for m in messages if m.role == "tool")
    assert [m.tool_call_id for m in messages if m.role == "tool"] == ["call_ok"]
