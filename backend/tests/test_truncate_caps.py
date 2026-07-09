"""C1/C2/C4 truncate_caps 单元测试。"""
import json
import sys

sys.path.insert(0, "/Users/shubinzhang/Documents/agent/Clawith/backend")

from app.services.llm.emit_guarded import emit_guarded
from app.services.llm.truncate_caps import (
    apply_cross_session_read_hints,
    is_rtk_invariant_enabled,
)


def test_lossy_unrecoverable_reverts_with_rtk_path(monkeypatch):
    class _S:
        CTX_RTK_INVARIANT_PATHS = "acp"

    import app.config as cfg

    monkeypatch.setattr(cfg, "get_settings", lambda: _S())
    original = "short original text"
    compressed = "tiny"
    hint = "retrieve " * 100
    assert emit_guarded(compressed, hint, original, model_name="", ctx_path="acp") == original
    assert is_rtk_invariant_enabled("acp")
    assert not is_rtk_invariant_enabled("feishu")


def test_read_tail_hint_on_duplicate_path(monkeypatch):
    class _S:
        CTX_RTK_INVARIANT_PATHS = "acp"

    import app.config as cfg

    monkeypatch.setattr(cfg, "get_settings", lambda: _S())
    h = "a" * 64
    arg = json.dumps({"path": "foo.kt"})
    messages = [
        {
            "role": "assistant",
            "tool_calls": [
                {"id": "t1", "function": {"name": "read_file", "arguments": arg}},
                {"id": "t2", "function": {"name": "read_file", "arguments": arg}},
            ],
        },
        {"role": "tool", "tool_call_id": "t1", "content": f"<!-- ccr:{h} -->\nfirst"},
        {"role": "tool", "tool_call_id": "t2", "content": "second read"},
    ]
    out = apply_cross_session_read_hints(messages, ctx_path="acp")
    assert "勿重复 read" in out[-1]["content"]
