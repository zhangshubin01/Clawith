"""Layer1/F2 CCR offload 单元测试 — 不依赖真实 PG。"""
import sys

sys.path.insert(0, "/Users/shubinzhang/Documents/agent/Clawith/backend")

import pytest

from app.services.llm.client import LLMMessage
from app.services.llm import ccr_offload


def _make_history(*, rounds: int, content_len: int = 2000) -> list[LLMMessage]:
    """构造含多轮 assistant+tool 的消息链。"""
    messages: list[LLMMessage] = [LLMMessage(role="system", content="sys")]
    for r in range(rounds):
        tc_id = f"tc{r}"
        messages.append(
            LLMMessage(
                role="assistant",
                content="",
                tool_calls=[{"id": tc_id, "function": {"name": "search_clawhub", "arguments": "{}"}}],
            )
        )
        messages.append(
            LLMMessage(role="tool", content="x" * content_len, tool_call_id=tc_id)
        )
    return messages


@pytest.mark.asyncio
async def test_layer1_offloads_old_rounds(monkeypatch):
    async def _fake_store(**kwargs):
        return "a" * 64

    monkeypatch.setattr(ccr_offload, "store_entry", _fake_store)
    messages = _make_history(rounds=5, content_len=2000)
    out, n = await ccr_offload.offload_old_tool_messages(
        messages,
        session_id="sess-offload-1",
        agent_id="agent-1",
        ctx_path="acp",
        protect_rounds=2,
    )
    assert n >= 1
    old_tool = [m for m in out if getattr(m, "role", None) == "tool"][0]
    assert "<!-- ccr:" in old_tool.content


@pytest.mark.asyncio
async def test_layer1_skips_excluded_tools(monkeypatch):
    called = {"n": 0}

    async def _fake_store(**kwargs):
        called["n"] += 1
        return "b" * 64

    monkeypatch.setattr(ccr_offload, "store_entry", _fake_store)
    messages: list[LLMMessage] = [LLMMessage(role="system", content="sys")]
    for r in range(4):
        tc_id = f"tc{r}"
        messages.append(
            LLMMessage(
                role="assistant",
                content="",
                tool_calls=[{"id": tc_id, "function": {"name": "read_file", "arguments": "{}"}}],
            )
        )
        messages.append(
            LLMMessage(role="tool", content="y" * 2000, tool_call_id=tc_id)
        )
    _, n = await ccr_offload.offload_old_tool_messages(
        messages,
        session_id="sess-offload-2",
        agent_id="agent-1",
        ctx_path="ws",
        protect_rounds=1,
    )
    assert n == 0
    assert called["n"] == 0


@pytest.mark.asyncio
async def test_f2_offload_truncated_prefix_all_roles(monkeypatch):
    async def _fake_store(**kwargs):
        return "c" * 64

    monkeypatch.setattr(ccr_offload, "store_entry", _fake_store)

    full = [
        {"role": "user", "content": "old user message with enough length"},
        {"role": "tool", "content": "z" * 2000, "tool_call_id": "t0"},
        {"role": "assistant", "content": "mid assistant reply"},
        {"role": "user", "content": "new"},
    ]
    kept, n = await ccr_offload.offload_truncated_prefix(
        full,
        full[-2:],
        session_id="sess-f2",
        agent_id="agent-1",
        ctx_path="feishu",
    )
    assert n >= 2
    assert any("<!-- ccr:" in (m.get("content") or "") for m in kept)


@pytest.mark.asyncio
async def test_layer1_no_session_no_offload():
    messages = _make_history(rounds=3)
    out, n = await ccr_offload.offload_old_tool_messages(
        messages,
        session_id="",
        agent_id="agent-1",
        ctx_path="acp",
    )
    assert n == 0
    assert out is messages


@pytest.mark.asyncio
async def test_f2_injects_discoverable_marker(monkeypatch):
    async def _fake_store(**kwargs):
        return "e" * 64

    monkeypatch.setattr(ccr_offload, "store_entry", _fake_store)

    full = [
        {"role": "user", "content": "old user context that should be archived"},
        {"role": "tool", "content": "z" * 2000, "tool_call_id": "t0"},
        {"role": "assistant", "content": "mid"},
        {"role": "user", "content": "new"},
    ]
    kept, n = await ccr_offload.offload_truncated_prefix(
        full,
        full[-2:],
        session_id="sess-marker",
        agent_id="agent-1",
        ctx_path="ws",
    )
    assert n >= 1
    assert any("历史截断" in (m.get("content") or "") for m in kept)
    assert any("<!-- ccr:" in (m.get("content") or "") for m in kept)
