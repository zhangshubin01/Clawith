"""F2/F4 hydrate 深化单元测试。"""
import json
import sys
import uuid
from types import SimpleNamespace

import pytest

sys.path.insert(0, "/Users/shubinzhang/Documents/agent/Clawith/backend")

from app.services.llm import history_hydrate
from app.services.llm.history_hydrate import _pretruncate_by_token_budget
from app.services.llm.utils import convert_chat_messages_to_llm_format


def _msg(role: str, content: str):
    return SimpleNamespace(role=role, content=content, id=uuid.uuid4(), thinking=None)


@pytest.mark.asyncio
async def test_f2_pretruncate_drops_oldest_when_over_budget(monkeypatch):
    """超 token 预算时应缩短消息条数。"""
    async def _fake_offload_prefix(all_messages, kept_messages, **kwargs):
        return kept_messages, 0

    monkeypatch.setattr(
        "app.services.llm.ccr_offload.offload_truncated_prefix",
        _fake_offload_prefix,
    )
    msgs = [{"role": "user", "content": "u" * 8000}] * 30
    out = await _pretruncate_by_token_budget(
        msgs, ctx_window=2000, model_name="", ctx_path="test", session_id="s1", agent_id="a1",
    )
    assert len(out) < len(msgs)


@pytest.mark.asyncio
async def test_f2_compress_before_drop_avoids_truncate(monkeypatch):
    """压老轮 tool 后若回到预算内，不应再丢消息。"""
    called = {"drop": 0}

    async def _fake_compress(messages, **kwargs):
        out = []
        for m in messages:
            if m.get("role") == "tool" and isinstance(m.get("content"), str) and len(m["content"]) > 100:
                nm = dict(m)
                nm["content"] = "short"
                out.append(nm)
            else:
                out.append(m)
        return out, 1

    async def _fake_offload_prefix(all_messages, kept_messages, **kwargs):
        called["drop"] += 1
        return kept_messages, 0

    monkeypatch.setattr(history_hydrate, "_offload_old_tools_in_dict", _fake_compress)
    monkeypatch.setattr(
        "app.services.llm.ccr_offload.offload_truncated_prefix",
        _fake_offload_prefix,
    )

    msgs = [{"role": "user", "content": "u" * 400}] * 10 + [
        {"role": "tool", "content": "t" * 5000, "tool_call_id": "x"},
    ]
    out = await _pretruncate_by_token_budget(
        msgs, ctx_window=3000, model_name="", ctx_path="test", session_id="s1", agent_id="a1",
    )
    assert called["drop"] == 0
    assert len(out) == len(msgs)


def test_ws_convert_passes_ctx_window():
    rows = [_msg("user", "hello")]
    out = convert_chat_messages_to_llm_format(rows, ctx_window=8192, path="ws")
    assert out[0]["content"] == "hello"


@pytest.mark.asyncio
async def test_f4_read_lifecycle_applied_after_hydrate(monkeypatch):
    called = {"n": 0}

    async def _fake_lc(messages, **kwargs):
        called["n"] += 1
        from app.services.llm.read_lifecycle import ReadLifecycleResult
        return ReadLifecycleResult(messages=messages)

    async def _fake_pretruncate(m, **k):
        return m

    monkeypatch.setattr(
        "app.services.llm.history_hydrate._apply_history_read_lifecycle",
        _fake_lc,
    )
    monkeypatch.setattr(
        "app.services.llm.history_hydrate._pretruncate_by_token_budget",
        _fake_pretruncate,
    )
    monkeypatch.setattr(
        "app.services.llm.history_hydrate.apply_cross_session_read_hints",
        lambda m, **k: m,
    )

    payload = {"name": "read_file", "args": {"path": "a.py"}, "status": "done", "result": "x" * 100}
    rows = [_msg("tool_call", json.dumps(payload))]
    msgs = convert_chat_messages_to_llm_format(rows, ctx_window=100_000, path="test")
    await history_hydrate.hydrate_history_tool_results(
        msgs,
        session_id=str(uuid.uuid4()),
        agent_id=uuid.uuid4(),
        ctx_path="acp",
        ctx_window=100_000,
    )
    assert called["n"] == 1
