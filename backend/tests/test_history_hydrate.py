"""history_hydrate：persist → load → marker → retrieve 链路。"""

import json
import sys
import uuid
from types import SimpleNamespace

import pytest

sys.path.insert(0, "/Users/shubinzhang/Documents/agent/Clawith/backend")

from app.services.llm import history_hydrate
from app.services.llm.utils import convert_chat_messages_to_llm_format


def _msg(role: str, content: str, msg_id: uuid.UUID | None = None):
    return SimpleNamespace(
        role=role,
        content=content,
        id=msg_id or uuid.uuid4(),
        thinking=None,
    )


def _tool_chain(*, rounds: int, content_len: int = 4000) -> list[dict]:
    rows = []
    for r in range(rounds):
        tc_payload = {
            "name": "search_clawhub",
            "args": {"query": f"q{r}"},
            "status": "done",
            "result": "z" * content_len,
        }
        rows.append(_msg("tool_call", json.dumps(tc_payload)))
    return convert_chat_messages_to_llm_format(rows, ctx_window=100_000, path="test")


@pytest.mark.asyncio
async def test_hydrate_offloads_old_rounds(monkeypatch):
    """超过 protect_rounds 的老 tool 结果应 offload 为 CCR marker。"""
    fake_hash = "d" * 64

    async def _fake_store(**kwargs):
        return fake_hash

    monkeypatch.setattr("app.services.llm.ccr_store.store_entry", _fake_store)
    messages = _tool_chain(rounds=12, content_len=3000)
    out = await history_hydrate.hydrate_history_tool_results(
        messages,
        session_id="sess-hydrate-1",
        agent_id=uuid.uuid4(),
        ctx_path="test",
        ctx_window=100_000,
        protect_rounds=2,
    )
    old_tools = [m for m in out if m.get("role") == "tool"][:2]
    assert any("<!-- ccr:" in (m.get("content") or "") for m in old_tools)


@pytest.mark.asyncio
async def test_hydrate_compress_recent_over_budget(monkeypatch):
    """最近轮次超 budget 时走 guarded compress。"""
    called = {"n": 0}

    async def _fake_compress(content, tool_name, budget, model_name, ctx_path, session_id, agent_id, **kw):
        called["n"] += 1
        return content[:200] + "\n[compressed]"

    monkeypatch.setattr("app.services.llm.caller._guarded_compress_with_ccr", _fake_compress)
    messages = _tool_chain(rounds=3, content_len=50_000)
    out = await history_hydrate.hydrate_history_tool_results(
        messages,
        session_id="sess-hydrate-2",
        agent_id=uuid.uuid4(),
        ctx_path="test",
        ctx_window=100_000,
        protect_rounds=20,
    )
    assert called["n"] >= 1
    last_tool = [m for m in out if m.get("role") == "tool"][-1]
    assert "[compressed]" in last_tool["content"]


@pytest.mark.asyncio
async def test_hydrate_skips_existing_ccr_marker():
    """已有 CCR marker 的 tool 消息不再重复处理。"""
    messages = [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": "tc0", "function": {"name": "read_file", "arguments": "{}"}}],
        },
        {"role": "tool", "content": "<!-- ccr:abc -->\nkept", "tool_call_id": "tc0"},
    ]
    out = await history_hydrate.hydrate_history_tool_results(
        messages,
        session_id="sess-hydrate-3",
        agent_id=uuid.uuid4(),
        ctx_path="test",
    )
    assert out[1]["content"] == "<!-- ccr:abc -->\nkept"
