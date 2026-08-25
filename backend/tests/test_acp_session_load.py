"""ACP session/load 非法 UUID 守卫回归测试。

线上曾出现 [ACP] session/load 失败 → -32603 Internal error：客户端传入
空/非法 sessionId 时，load() 直接把字符串与 UUID 列比较，asyncpg 抛
DataError（invalid UUID），被 dispatch 层总 catch 吞掉。修复后非法 UUID
在进入 DB 前短路返回 None，handler 优雅返回 {"error": "Session not found"}。
"""

import uuid
from types import SimpleNamespace

import pytest

from app.plugins.clawith_acp import acp_session as acp_session_mod
from app.plugins.clawith_acp.acp_handler import AcpHandler
from app.plugins.clawith_acp.acp_session import AcpSessionManager


class FakeRowsResult:
    def __init__(self, rows=(), scalar_value=None):
        self._rows = list(rows)
        self._scalar_value = scalar_value

    def scalar_one_or_none(self):
        return self._scalar_value

    def scalars(self):
        return self

    def all(self):
        return self._rows

    def scalar(self):
        return self._scalar_value


class FakeDB:
    """按顺序返回预设结果的最小 async session 双身。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.statements = []

    async def execute(self, statement, _params=None):
        self.statements.append(statement)
        if not self.responses:
            raise AssertionError("unexpected execute() call")
        return self.responses.pop(0)


class FakeSessionFactory:
    def __init__(self, db):
        self.db = db

    def __call__(self):
        return _FakeSessionCtx(self.db)


class _FakeSessionCtx:
    def __init__(self, db):
        self.db = db

    async def __aenter__(self):
        return self.db

    async def __aexit__(self, *exc):
        return False


@pytest.mark.asyncio
async def test_load_rejects_invalid_ids_without_touching_db(monkeypatch):
    mgr = AcpSessionManager()
    called = {"n": 0}

    async def fake_session():
        called["n"] += 1
        raise AssertionError("DB must not be touched for invalid UUID")

    monkeypatch.setattr(acp_session_mod, "async_session", fake_session)

    assert await mgr.load("", str(uuid.uuid4())) is None
    assert await mgr.load("not-a-uuid", str(uuid.uuid4())) is None
    assert await mgr.load(str(uuid.uuid4()), "not-a-uuid") is None
    assert called["n"] == 0


@pytest.mark.asyncio
async def test_load_returns_session_payload_for_valid_ids(monkeypatch):
    sid = uuid.uuid4()
    uid = uuid.uuid4()
    mgr = AcpSessionManager()

    session_row = SimpleNamespace(id=sid, user_id=uid, agent_id=uuid.uuid4(), title="/proj")
    db = FakeDB(
        [
            FakeRowsResult(scalar_value=session_row),  # ChatSession select
            FakeRowsResult(rows=[]),  # history select
            FakeRowsResult(scalar_value=0),  # tool_call count
        ]
    )
    monkeypatch.setattr(acp_session_mod, "async_session", FakeSessionFactory(db))

    result = await mgr.load(str(sid), str(uid))

    assert result["agent_id"] == str(session_row.agent_id)
    assert result["cwd"] == "/proj"
    assert result["history"] == []
    assert result["tool_call_count"] == 0


@pytest.mark.asyncio
async def test_update_agent_invalid_ids_noop(monkeypatch):
    mgr = AcpSessionManager()
    called = {"n": 0}

    async def fake_session():
        called["n"] += 1
        raise AssertionError("DB must not be touched for invalid UUID")

    monkeypatch.setattr(acp_session_mod, "async_session", fake_session)

    await mgr.update_agent("bad-session", "bad-agent")
    assert called["n"] == 0


@pytest.mark.asyncio
async def test_handle_session_load_invalid_id_returns_not_found():
    handler = AcpHandler(None, str(uuid.uuid4()))
    # 真实 session_mgr 的 load() 守卫在 async_session 前短路，无需 DB
    result = await handler._handle_session_load({"sessionId": ""})
    assert result == {"error": "Session not found"}


@pytest.mark.asyncio
async def test_handle_session_load_tolerates_non_dict_meta():
    handler = AcpHandler(None, str(uuid.uuid4()))
    sid = str(uuid.uuid4())
    agent_id = str(uuid.uuid4())

    class FakeMgr:
        async def load(self, _s, _u):
            return {
                "agent_id": agent_id,
                "cwd": "",
                "history": [],
                "historyHasTools": False,
                "tool_call_count": 0,
            }

    handler.session_mgr = FakeMgr()

    # _meta 不是 dict 时不得抛 AttributeError，应回退到会话绑定的 agent
    result = await handler._handle_session_load({"sessionId": sid, "_meta": ["bad"]})

    assert result["sessionId"] == sid
    assert handler.agent_id == agent_id
