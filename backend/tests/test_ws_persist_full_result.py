"""Wave1.5 E2-WS：WebUI tool persist 不得 [:500] 截断。"""
import sys
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, "/Users/shubinzhang/Documents/agent/Clawith/backend")

from app.api.websocket import WebSocketChatHandler


@pytest.mark.asyncio
async def test_ws_save_tool_call_passes_full_result(monkeypatch):
    """_save_completed_tool_call_to_db 应将 >500 字 result 原样交给 save_tool_call_log。"""
    captured: dict = {}
    agent_id = uuid.uuid4()
    user_id = uuid.uuid4()
    conv_id = str(uuid.uuid4())
    long_result = "x" * 1417

    async def _fake_save_tool_call_log(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(
        "app.services.chat_session_service.save_tool_call_log",
        _fake_save_tool_call_log,
    )

    class _FakeDB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def commit(self):
            pass

    monkeypatch.setattr("app.api.websocket.async_session", lambda: _FakeDB())
    monkeypatch.setattr(
        "app.api.websocket.maybe_mark_session_read_for_active_viewer",
        AsyncMock(),
    )

    handler = WebSocketChatHandler(
        websocket=MagicMock(),
        agent_id=agent_id,
        token="t",
    )
    handler.user = SimpleNamespace(id=user_id)
    handler.conv_id = conv_id

    await handler._save_completed_tool_call_to_db(
        {
            "name": "execute_command",
            "args": {"command": "gradle test"},
            "result": long_result,
            "call_id": "call-1",
        }
    )

    assert captured.get("result") == long_result
    assert len(captured["result"]) > 500
    assert captured["conversation_id"] == conv_id
    assert captured["tool_name"] == "execute_command"
