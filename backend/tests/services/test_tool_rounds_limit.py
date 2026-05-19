"""Tests for tool-round limit user-facing messages."""

from app.services.llm.caller import format_tool_rounds_limit_reply


def test_format_tool_rounds_limit_reply_includes_max_rounds():
    text = format_tool_rounds_limit_reply(200)
    assert "200" in text


def test_format_tool_rounds_limit_reply_bilingual():
    text = format_tool_rounds_limit_reply(50)
    assert "已达到本智能体的工具调用上限" in text
    assert "最大工具调用轮次" in text
    assert "The tool call limit for this agent has been reached" in text
    assert "Max tool call rounds" in text
