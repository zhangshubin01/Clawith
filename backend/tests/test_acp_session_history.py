"""ACP 会话历史：tool_call 应纳入 LLM 上下文（对齐 WS 路径）。"""

import json
import uuid
from types import SimpleNamespace

from app.services.llm.utils import convert_chat_messages_to_llm_format


def _msg(role: str, content: str, msg_id: uuid.UUID | None = None):
    return SimpleNamespace(
        role=role,
        content=content,
        id=msg_id or uuid.uuid4(),
        thinking=None,
    )


def test_convert_includes_tool_call_as_assistant_and_tool_pair():
    """tool_call 行应展开为 assistant(tool_calls) + tool 结果。"""
    tc_payload = {
        "name": "read_file",
        "args": {"path": "foo.kt"},
        "status": "done",
        "result": "file contents",
        "tool_call_id": "call_abc",
    }
    rows = [
        _msg("user", "继续"),
        _msg("assistant", "好的"),
        _msg("tool_call", json.dumps(tc_payload)),
    ]
    out = convert_chat_messages_to_llm_format(rows)
    assert out[0] == {"role": "user", "content": "继续"}
    assert out[1] == {"role": "assistant", "content": "好的"}
    assert out[2]["role"] == "assistant"
    assert out[2]["tool_calls"][0]["function"]["name"] == "read_file"
    assert out[3]["role"] == "tool"
    assert "file contents" in out[3]["content"]

def test_execute_command_history_not_hard_truncated_at_500():
    """execute_command 长输出应走 budget 而非 [:500] 硬切。"""
    long_output = "x" * 1417
    tc_payload = {
        "name": "execute_command",
        "args": {"command": "gradle test"},
        "status": "done",
        "result": long_output,
    }
    rows = [_msg("tool_call", json.dumps(tc_payload))]
    out = convert_chat_messages_to_llm_format(rows, ctx_window=100_000, path="test")
    tool_msgs = [m for m in out if m["role"] == "tool"]
    assert len(tool_msgs) == 1
    assert len(tool_msgs[0]["content"]) > 500
    assert tool_msgs[0]["content"].startswith("x" * 100)


def test_ccr_hash_included_in_tool_content():
    """payload 含 ccr_hash 时 tool 消息应带 CCR marker。"""
    tc_payload = {
        "name": "read_file",
        "args": {"path": "big.txt"},
        "status": "done",
        "result": "y" * 3000,
        "ccr_hash": "a" * 64,
    }
    rows = [_msg("tool_call", json.dumps(tc_payload))]
    out = convert_chat_messages_to_llm_format(rows)
    tool_msg = [m for m in out if m["role"] == "tool"][0]
    assert "<!-- ccr:" in tool_msg["content"]

