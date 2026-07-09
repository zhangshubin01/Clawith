"""call_llm 只读并行 gather 时序测试。"""
import asyncio
import sys
import time

sys.path.insert(0, "/Users/shubinzhang/Documents/agent/Clawith/backend")

import pytest

from app.services.llm import caller
from app.services.llm.caller import _execute_tool_round
from app.services.llm.client import LLMMessage


@pytest.mark.asyncio
async def test_parallel_one_failure_isolated():

    async def _fake_execute(tool_name, args, **kwargs):
        if args.get("path") == "bad":
            raise RuntimeError("boom")
        return "ok"

    caller.execute_tool = _fake_execute
    calls = [
        {"id": "c1", "function": {"name": "read_file", "arguments": '{"path":"good"}'}},
        {"id": "c2", "function": {"name": "read_file", "arguments": '{"path":"bad"}'}},
        {"id": "c3", "function": {"name": "read_file", "arguments": '{"path":"good2"}'}},
    ]
    api_messages = [LLMMessage(role="user", content="hi")]
    await _execute_tool_round(
        calls,
        api_messages=api_messages,
        agent_id="a1",
        user_id="u1",
        session_id="s1",
        supports_vision=False,
        on_tool_call=None,
        on_code_output=None,
        full_reasoning_content="",
        allowed_tool_names={"read_file"},
        model_name="gpt-4o",
        ctx_window=100000,
        compress_enabled=False,
        ctx_path="test",
        retrieve_tool_available=False,
        round_i=0,
    )
    tool_msgs = [m for m in api_messages if m.role == "tool"]
    assert len(tool_msgs) == 3
    assert any("boom" in (m.content or "") for m in tool_msgs)


@pytest.mark.asyncio
async def test_parallel_result_order_preserved():
    order = []

    async def _fake_execute(tool_name, args, **kwargs):
        path = args.get("path", "")
        order.append(path)
        await asyncio.sleep(0.02 if path.endswith("2") else 0.01)
        return path

    caller.execute_tool = _fake_execute
    calls = [
        {"id": "c1", "function": {"name": "read_file", "arguments": '{"path":"f1"}'}},
        {"id": "c2", "function": {"name": "read_file", "arguments": '{"path":"f2"}'}},
        {"id": "c3", "function": {"name": "read_file", "arguments": '{"path":"f3"}'}},
    ]
    api_messages = [LLMMessage(role="user", content="hi")]
    await _execute_tool_round(
        calls,
        api_messages=api_messages,
        agent_id="a1",
        user_id="u1",
        session_id="s1",
        supports_vision=False,
        on_tool_call=None,
        on_code_output=None,
        full_reasoning_content="",
        allowed_tool_names={"read_file"},
        model_name="gpt-4o",
        ctx_window=100000,
        compress_enabled=False,
        ctx_path="test",
        retrieve_tool_available=False,
        round_i=0,
    )
    tool_msgs = [m for m in api_messages if m.role == "tool"]
    assert [m.tool_call_id for m in tool_msgs] == ["c1", "c2", "c3"]
    assert [m.content for m in tool_msgs] == ["f1", "f2", "f3"]


@pytest.mark.asyncio
async def test_duckduckgo_search_cap_semaphore(monkeypatch):
    from app.services.llm import tool_execution_policy as policy

    policy._search_sems.clear()
    policy._rate_limit_permits.cache_clear()
    monkeypatch.setenv("PARALLEL_SEARCH_PERMITS", "2")

    active = 0
    peak = 0
    lock = asyncio.Lock()

    async def _fake_execute(tool_name, args, **kwargs):
        nonlocal active, peak
        async with lock:
            active += 1
            peak = max(peak, active)
        await asyncio.sleep(0.15)
        async with lock:
            active -= 1
        return "ok"

    caller.execute_tool = _fake_execute
    calls = [
        {"id": f"c{i}", "function": {"name": "duckduckgo_search", "arguments": f'{{"query":"q{i}"}}'}}
        for i in range(6)
    ]
    api_messages = [LLMMessage(role="user", content="hi")]
    await _execute_tool_round(
        calls,
        api_messages=api_messages,
        agent_id="a1",
        user_id="u1",
        session_id="s1",
        supports_vision=False,
        on_tool_call=None,
        on_code_output=None,
        full_reasoning_content="",
        allowed_tool_names={"duckduckgo_search"},
        model_name="gpt-4o",
        ctx_window=100000,
        compress_enabled=False,
        ctx_path="test",
        retrieve_tool_available=False,
        round_i=0,
    )
    assert peak <= 2
    assert len([m for m in api_messages if m.role == "tool"]) == 6


@pytest.mark.asyncio
async def test_serial_single_tool_exception_returns_error():
    async def _fake_execute(tool_name, args, **kwargs):
        raise RuntimeError("serial-boom")

    caller.execute_tool = _fake_execute
    calls = [{"id": "c1", "function": {"name": "execute_command", "arguments": '{"command":"ls"}'}}]
    api_messages = [LLMMessage(role="user", content="hi")]
    await _execute_tool_round(
        calls,
        api_messages=api_messages,
        agent_id="a1",
        user_id="u1",
        session_id="s1",
        supports_vision=False,
        on_tool_call=None,
        on_code_output=None,
        full_reasoning_content="",
        allowed_tool_names={"execute_command"},
        model_name="gpt-4o",
        ctx_window=100000,
        compress_enabled=False,
        ctx_path="test",
        retrieve_tool_available=False,
        round_i=0,
    )
    tool_msgs = [m for m in api_messages if m.role == "tool"]
    assert len(tool_msgs) == 1
    assert "serial-boom" in (tool_msgs[0].content or "")


@pytest.mark.asyncio
async def test_tool_timeout_isolated(monkeypatch):
    """单工具超时不拖死整批并行工具。"""
    from app.plugins.clawith_acp.turn_budget import TurnBudget, set_turn_budget
    from app.services.llm import caller

    budget = TurnBudget(workflow_seconds=7200, compute_seconds=1800)
    set_turn_budget(budget)
    monkeypatch.setenv("ACP_TOOL_TIMEOUT_SECONDS", "0.01")

    slow_tc = {
        "id": "call_slow",
        "function": {"name": "read_file", "arguments": "{}"},
    }
    fast_tc = {
        "id": "call_fast",
        "function": {"name": "read_file", "arguments": "{}"},
    }

    async def fake_process(tc, **kwargs):
        import asyncio
        from app.services.llm.caller import ToolCallResult
        if tc.get("id") == "call_slow":
            await asyncio.sleep(0.15)
            return ToolCallResult(tool_call_id=tc["id"], tool_name="read_file", content="slow")
        return ToolCallResult(tool_call_id=tc["id"], tool_name="read_file", content="fast")

    monkeypatch.setattr(caller, "_process_tool_call", fake_process)
    api_messages = []
    await caller._execute_tool_round(
        [slow_tc, fast_tc],
        api_messages=api_messages,
        agent_id=None,
        user_id=None,
        session_id="s1",
        supports_vision=False,
        on_tool_call=None,
        on_code_output=None,
        full_reasoning_content="",
        allowed_tool_names={"read_file"},
        model_name="gpt-4",
        ctx_window=128000,
        compress_enabled=False,
        ctx_path="",
        retrieve_tool_available=False,
        round_i=0,
    )
    tool_msgs = [m for m in api_messages if getattr(m, "role", None) == "tool"]
    assert len(tool_msgs) == 2
    contents = [getattr(m, "content", "") for m in tool_msgs]
    assert any("timed out" in c for c in contents)
    assert any(c == "fast" for c in contents)
    set_turn_budget(None)
