"""B3/B4+ ACP UX 兜底逻辑单元测试。"""
import asyncio
import sys
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, "/Users/shubinzhang/Documents/agent/Clawith/backend")

from app.plugins.clawith_acp.acp_handler import AcpHandler


@pytest.mark.asyncio
async def test_last_assistant_returns_db_text(monkeypatch):
    handler = AcpHandler(MagicMock(), str(uuid.uuid4()))
    handler.session_id = str(uuid.uuid4())
    handler.user_id = str(uuid.uuid4())

    class _Msg:
        content = "assistant body " * 20

    class _FakeDB:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            pass
        async def execute(self, q):
            class _R:
                def scalar_one_or_none(self):
                    return _Msg()
            return _R()

    monkeypatch.setattr("app.plugins.clawith_acp.acp_handler.async_session", lambda: _FakeDB())
    out = await handler._handle_last_assistant({"sessionId": handler.session_id})
    assert len(out.get("text", "")) > 50


@pytest.mark.asyncio
async def test_do_flush_failure_triggers_persist_fallback(monkeypatch):
    handler = AcpHandler(MagicMock(), str(uuid.uuid4()))
    handler.session_id = "sess-1"
    handler._streamed_reply_parts = ["partial answer"]
    handler._chunk_buffer = "tail"
    handler.ws = MagicMock()
    handler.ws.send_text = AsyncMock(side_effect=RuntimeError("ws dead"))
    called = {"n": 0}

    async def _fake_persist(user_text: str = "") -> None:
        called["n"] += 1

    monkeypatch.setattr(handler, "_persist_streamed_reply_fallback", _fake_persist)
    monkeypatch.setattr(handler, "_push_recovery_notice", AsyncMock())
    await handler._do_flush()
    assert called["n"] == 1
