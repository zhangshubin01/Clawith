"""E0：session/load 返回 historyHasTools。"""
import sys
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, "/Users/shubinzhang/Documents/agent/Clawith/backend")

from app.plugins.clawith_acp.acp_session import AcpSessionManager


@pytest.mark.asyncio
async def test_load_includes_history_has_tools(monkeypatch):
    sid = str(uuid.uuid4())
    uid = str(uuid.uuid4())

    class _Sess:
        agent_id = uuid.uuid4()
        user_id = uuid.UUID(uid)
        project_path = "/tmp"

    class _FakeDB:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            pass
        async def execute(self, q):
            sql = str(q)
            class _R:
                def scalar_one_or_none(inner):
                    return _Sess() if "chat_sessions" in sql.lower() or "chatsession" in sql.lower() else None
                def scalars(inner):
                    class _S:
                        def all(s):
                            return []
                    return _S()
                def scalar(inner):
                    return 3
            return _R()

    monkeypatch.setattr("app.plugins.clawith_acp.acp_session.async_session", lambda: _FakeDB())
    out = await AcpSessionManager().load(sid, uid)
    assert out is not None
    assert out["historyHasTools"] is True
    assert out["tool_call_count"] == 3
