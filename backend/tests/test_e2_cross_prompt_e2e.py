"""P0-E2E 自动化：persist → convert → hydrate 跨 prompt 链（不依赖 IDE）。"""
import json
import sys
import uuid
from types import SimpleNamespace

import pytest

sys.path.insert(0, "/Users/shubinzhang/Documents/agent/Clawith/backend")

from app.services.llm.history_hydrate import hydrate_history_tool_results
from app.services.llm.utils import convert_chat_messages_to_llm_format


def _msg(role: str, content: str):
    return SimpleNamespace(role=role, content=content, id=uuid.uuid4(), thinking=None)


@pytest.mark.asyncio
async def test_cross_prompt_execute_command_verbatim_1417(monkeypatch):
    """模拟：上轮 persist 1417 字 → 下轮 load+hydrate → LLM 仍见全文。"""
    long_out = "o" * 1417
    payload = {
        "name": "execute_command",
        "args": {"command": "gradle test"},
        "status": "done",
        "result": long_out,
        "tool_call_id": "call-e2e-1",
    }
    rows = [
        _msg("user", "跑 gradle test"),
        _msg("assistant", "好的"),
        _msg("tool_call", json.dumps(payload, ensure_ascii=False)),
        _msg("user", "复述上一轮命令完整输出"),
    ]
    llm_msgs = convert_chat_messages_to_llm_format(
        rows, ctx_window=100_000, path="acp"
    )
    out = await hydrate_history_tool_results(
        llm_msgs,
        session_id=str(uuid.uuid4()),
        agent_id=uuid.uuid4(),
        ctx_path="acp",
        ctx_window=100_000,
        model_name="gpt-4o",
        protect_rounds=20,
    )
    tool_contents = [m["content"] for m in out if m.get("role") == "tool"]
    assert tool_contents, "应有 tool 消息"
    assert long_out in tool_contents[0] or tool_contents[0] == long_out
    assert len(tool_contents[0]) >= 1417


@pytest.mark.asyncio
async def test_cross_prompt_ws_persist_then_load(monkeypatch):
    """Wave1.5：WS 全量 persist payload → convert 不截断。"""
    saved = {}

    async def _fake_save(**kwargs):
        saved.update(kwargs)

    class _FakeDB:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            pass
        def add(self, obj):
            saved["msg"] = obj
        async def commit(self):
            pass

    monkeypatch.setattr("app.database.async_session", lambda: _FakeDB())
    monkeypatch.setattr("app.services.llm.ccr_store.store_entry", lambda **k: "")
    monkeypatch.setattr(
        "app.plugins.clawith_acp.history_cache.invalidate_history_cache",
        lambda sid: None,
    )

    from app.services.chat_session_service import save_tool_call_log

    long_out = "w" * 1417
    sid = str(uuid.uuid4())
    await save_tool_call_log(
        agent_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        conversation_id=sid,
        tool_name="execute_command",
        arguments={"command": "echo hi"},
        result=long_out,
    )
    payload = json.loads(saved["msg"].content)
    assert len(payload["result"]) == 1417

    rows = [_msg("tool_call", saved["msg"].content)]
    out = convert_chat_messages_to_llm_format(rows, ctx_window=100_000, path="ws")
    tool = [m for m in out if m["role"] == "tool"][0]
    assert len(tool["content"]) == 1417
