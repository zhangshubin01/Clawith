"""Unit / integration-style tests for the clawith-acp WebSocket bridge (no real IDE).

Run from repository backend directory:
  cd backend && .venv/bin/pytest tests/plugins/test_clawith_acp.py -v

Requires optional dev deps: pytest, pytest-asyncio (see pyproject.toml [project.optional-dependencies] dev).
"""

from __future__ import annotations

import base64
import importlib.util
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.plugins.clawith_acp import router as acp_router

# Repo root: backend/tests/plugins -> … -> Clawith
REPO_ROOT = Path(__file__).resolve().parents[3]
THIN_SERVER_PATH = REPO_ROOT / "integrations" / "clawith-ide-acp" / "server.py"


def _load_thin_server_module():
    if not THIN_SERVER_PATH.is_file():
        pytest.skip(f"Thin client not found at {THIN_SERVER_PATH}")
    spec = importlib.util.spec_from_file_location("clawith_acp_thin_server", THIN_SERVER_PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── Router: envelope & constants ─────────────────────────────────────────────


def test_acp_ws_envelope_adds_schema_version():
    env = acp_router._acp_ws_envelope({"type": "chunk", "content": "x"})
    assert env["schemaVersion"] == acp_router.ACP_WS_SCHEMA_VERSION
    assert env["type"] == "chunk"
    assert env["content"] == "x"


def test_acp_ws_envelope_done_message():
    env = acp_router._acp_ws_envelope({"type": "done"})
    assert env["schemaVersion"] == acp_router.ACP_WS_SCHEMA_VERSION
    assert env["type"] == "done"


def test_install_acp_tool_hooks_idempotent():
    import app.services.agent_tools as agent_tools

    ref_get = agent_tools.get_agent_tools_for_llm
    ref_exec = agent_tools.execute_tool
    acp_router.install_acp_tool_hooks()
    assert agent_tools.get_agent_tools_for_llm is ref_get
    assert agent_tools.execute_tool is ref_exec


# ── Fake async DB session (async_session context manager) ────────────────────


class _ScalarResult:
    def __init__(self, scalar=None, rows=None):
        self._scalar = scalar
        self._rows = list(rows or [])

    def scalar_one_or_none(self):
        return self._scalar

    def scalars(self):
        return self

    def all(self):
        return self._rows


class _FakeDb:
    def __init__(self, execute_results):
        self._results = list(execute_results)

    async def execute(self, _statement, _params=None):
        if not self._results:
            raise AssertionError("unexpected db.execute() — no more queued results")
        return self._results.pop(0)


class _AsyncSessionCtx:
    def __init__(self, fake_db: _FakeDb):
        self._fake_db = fake_db

    async def __aenter__(self):
        return self._fake_db

    async def __aexit__(self, *args):
        return None


class _FakeAsyncSessionFactory:
    def __init__(self, fake_db: _FakeDb):
        self._fake_db = fake_db

    def __call__(self):
        return _AsyncSessionCtx(self._fake_db)


@pytest.fixture
def patch_acp_async_session(monkeypatch):
    """Inject a fake async_session factory; restore after test."""

    def _apply(fake_db: _FakeDb):
        factory = _FakeAsyncSessionFactory(fake_db)
        monkeypatch.setattr(acp_router, "async_session", factory)
        return fake_db

    yield _apply


# ── _load_acp_history_from_db ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_load_acp_history_invalid_session_id_returns_empty(patch_acp_async_session):
    patch_acp_async_session(_FakeDb([]))
    out = await acp_router._load_acp_history_from_db("not-a-valid-uuid", uuid.uuid4(), uuid.uuid4())
    assert out == []


@pytest.mark.asyncio
async def test_load_acp_history_no_session_row_returns_empty(patch_acp_async_session):
    sid = uuid.uuid4()
    patch_acp_async_session(_FakeDb([_ScalarResult(scalar=None)]))
    out = await acp_router._load_acp_history_from_db(sid.hex, uuid.uuid4(), uuid.uuid4())
    assert out == []


@pytest.mark.asyncio
async def test_load_acp_history_wrong_user_returns_empty(patch_acp_async_session):
    sid = uuid.uuid4()
    agent_id = uuid.uuid4()
    owner = uuid.uuid4()
    attacker = uuid.uuid4()
    sess = SimpleNamespace(id=sid, user_id=owner, agent_id=agent_id)
    patch_acp_async_session(_FakeDb([_ScalarResult(scalar=sess)]))
    out = await acp_router._load_acp_history_from_db(sid.hex, agent_id, attacker)
    assert out == []


@pytest.mark.asyncio
async def test_load_acp_history_success_user_assistant_only(patch_acp_async_session):
    sid = uuid.uuid4()
    agent_id = uuid.uuid4()
    user_id = uuid.uuid4()
    sess = SimpleNamespace(id=sid, user_id=user_id, agent_id=agent_id)
    m1 = SimpleNamespace(role="user", content="hi")
    m2 = SimpleNamespace(role="assistant", content="yo")
    patch_acp_async_session(
        _FakeDb(
            [
                _ScalarResult(scalar=sess),
                _ScalarResult(rows=[m1, m2]),
            ]
        )
    )
    out = await acp_router._load_acp_history_from_db(sid.hex, agent_id, user_id)
    assert out == [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "yo"}]


# ── _hydrate_if_needed_acp ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_hydrate_skips_when_memory_already_has_messages(patch_acp_async_session, monkeypatch):
    """Must not hit DB when history already present."""
    called = []

    async def boom(*a, **kw):
        called.append(1)
        raise AssertionError("should not load DB")

    monkeypatch.setattr(acp_router, "_load_acp_history_from_db", boom)
    agent = SimpleNamespace(id=uuid.uuid4())
    mem = {"sess1": [{"role": "user", "content": "x"}]}
    await acp_router._hydrate_if_needed_acp("sess1", agent, uuid.uuid4(), mem)
    assert called == []


@pytest.mark.asyncio
async def test_hydrate_fills_empty_memory_from_db(patch_acp_async_session, monkeypatch):
    sid = uuid.uuid4()
    agent_id = uuid.uuid4()
    user_id = uuid.uuid4()

    async def fake_load(sid_str, aid, uid):
        assert sid_str == sid.hex
        assert aid == agent_id
        assert uid == user_id
        return [{"role": "user", "content": "restored"}]

    monkeypatch.setattr(acp_router, "_load_acp_history_from_db", fake_load)
    patch_acp_async_session(_FakeDb([]))
    agent = SimpleNamespace(id=agent_id)
    mem: dict = {}
    await acp_router._hydrate_if_needed_acp(sid.hex, agent, user_id, mem)
    assert mem[sid.hex] == [{"role": "user", "content": "restored"}]


# ── _list_acp_chat_sessions ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_acp_chat_sessions_returns_rows(patch_acp_async_session):
    uid = uuid.uuid4()
    aid = uuid.uuid4()
    now = datetime.now(UTC)
    s1 = SimpleNamespace(
        id=uuid.uuid4(),
        title="IDE 01-01",
        last_message_at=now,
        created_at=now,
    )
    patch_acp_async_session(_FakeDb([_ScalarResult(rows=[s1])]))
    rows = await acp_router._list_acp_chat_sessions(uid, aid, limit=10)
    assert len(rows) == 1
    assert rows[0] is s1


# ── _custom_get_tools with ContextVar ───────────────────────────────────────


@pytest.mark.asyncio
async def test_custom_get_tools_appends_ide_tools_when_ws_active(monkeypatch):
    async def fake_orig(_agent_id):
        return [{"type": "function", "function": {"name": "only_builtin"}}]

    monkeypatch.setattr(acp_router, "_original_get_tools", fake_orig)
    mock_ws = MagicMock()
    tok = acp_router.current_acp_ws.set(mock_ws)
    try:
        tools = await acp_router._custom_get_tools(uuid.uuid4())
        names = [t["function"]["name"] for t in tools if t.get("type") == "function"]
        assert "only_builtin" in names
        assert "ide_read_file" in names
        assert "ide_write_file" in names
        assert "ide_execute_command" in names
        assert "ide_create_terminal" in names
        assert "ide_kill_terminal" in names
        assert "ide_release_terminal" in names
    finally:
        acp_router.current_acp_ws.reset(tok)


@pytest.mark.asyncio
async def test_custom_get_tools_no_ide_when_ws_inactive(monkeypatch):
    async def fake_orig(_agent_id):
        return [{"type": "function", "function": {"name": "only_builtin"}}]

    monkeypatch.setattr(acp_router, "_original_get_tools", fake_orig)
    tok = acp_router.current_acp_ws.set(None)
    try:
        tools = await acp_router._custom_get_tools(uuid.uuid4())
        names = [t["function"]["name"] for t in tools if t.get("type") == "function"]
        assert names == ["only_builtin"]
    finally:
        acp_router.current_acp_ws.reset(tok)


# ── Thin client module (integrations/clawith-ide-acp/server.py) ─────────────


def test_thin_cloud_msg_matches_router_version():
    thin = _load_thin_server_module()
    assert thin.CLOUD_WS_SCHEMA_VERSION == acp_router.ACP_WS_SCHEMA_VERSION


def test_thin_cloud_msg_shape():
    thin = _load_thin_server_module()
    msg = thin._cloud_msg({"type": "prompt", "text": "hi", "session_id": "abc"})
    assert msg["schemaVersion"] == thin.CLOUD_WS_SCHEMA_VERSION
    assert msg["type"] == "prompt"


@pytest.mark.asyncio
async def test_thin_list_sessions_ws_round_trip(monkeypatch):
    thin = _load_thin_server_module()
    sent = []

    class _FakeWs:
        async def send(self, raw: str):
            sent.append(json.loads(raw))

        async def recv(self):
            return json.dumps(
                {
                    "schemaVersion": thin.CLOUD_WS_SCHEMA_VERSION,
                    "type": "list_sessions_result",
                    "sessions": [
                        {
                            "sessionId": "deadbeef" * 4,
                            "cwd": "/project",
                            "title": "My IDE chat",
                            "updatedAt": "2026-04-05T12:00:00+00:00",
                        }
                    ],
                    "nextCursor": None,
                }
            )

    class _ConnCm:
        def __init__(self):
            self.ws = _FakeWs()

        async def __aenter__(self):
            return self.ws

        async def __aexit__(self, *args):
            return None

    def _fake_connect(_uri, **_kwargs):
        return _ConnCm()

    monkeypatch.setattr(thin.websockets, "connect", _fake_connect)
    agent = thin.ClawithThinClientAgent("WL4", "cw-test", "http://127.0.0.1:8008")
    res = await agent.list_sessions(cursor=None, cwd="/project")
    assert len(res.sessions) == 1
    assert res.sessions[0].session_id == "deadbeef" * 4
    assert res.sessions[0].title == "My IDE chat"
    assert sent[0]["type"] == "list_sessions"
    assert sent[0]["cwd"] == "/project"
    assert sent[0]["schemaVersion"] == thin.CLOUD_WS_SCHEMA_VERSION


@pytest.mark.asyncio
async def test_thin_load_session_returns_response():
    thin = _load_thin_server_module()
    agent = thin.ClawithThinClientAgent("WL4", "cw-x", "http://localhost:8008")
    out = await agent.load_session(cwd="/tmp", session_id="a" * 32)
    assert out is not None


@pytest.mark.asyncio
async def test_thin_authenticate_and_set_config_noop():
    thin = _load_thin_server_module()
    agent = thin.ClawithThinClientAgent("WL4", "cw-x", "http://localhost:8008")
    auth = await agent.authenticate("any")
    assert auth is not None
    cfg = await agent.set_config_option("x", "sess", True)
    assert cfg is not None
    assert cfg.config_options == []


@pytest.mark.asyncio
async def test_thin_cancel_no_ws_is_safe():
    thin = _load_thin_server_module()
    agent = thin.ClawithThinClientAgent("WL4", "cw-x", "http://localhost:8008")
    await agent.cancel("session-z")


@pytest.mark.asyncio
async def test_thin_cancel_sends_when_prompt_ws_active():
    thin = _load_thin_server_module()
    agent = thin.ClawithThinClientAgent("WL4", "cw-x", "http://localhost:8008")
    sent: list[dict] = []

    class _Ws:
        async def send(self, raw: str):
            sent.append(json.loads(raw))

    agent._active_prompt_ws = _Ws()
    await agent.cancel("session-z")
    assert len(sent) == 1
    assert sent[0].get("schemaVersion") == thin.CLOUD_WS_SCHEMA_VERSION
    assert sent[0].get("type") == "cancel"
    assert sent[0].get("session_id") == "session-z"


@pytest.mark.asyncio
async def test_thin_new_session_records_cwd():
    thin = _load_thin_server_module()
    agent = thin.ClawithThinClientAgent("WL4", "cw-x", "http://localhost:8008")
    res = await agent.new_session(cwd="/proj/x")
    assert res.session_id
    assert agent._session_cwds[res.session_id] == "/proj/x"


@pytest.mark.asyncio
async def test_thin_fork_session_returns_new_hex_id():
    thin = _load_thin_server_module()
    agent = thin.ClawithThinClientAgent("WL4", "cw-x", "http://localhost:8008")
    fr = await agent.fork_session(cwd="/x", session_id="b" * 32)
    assert len(fr.session_id) == 32
    assert all(c in "0123456789abcdef" for c in fr.session_id)


# ── P2: multimodal prompt_parts ↔ call_llm ──────────────────────────────────


def test_build_acp_user_turn_legacy_text_only():
    c, disp = acp_router._build_acp_user_turn_from_ws({"text": "  hello  "})
    assert c == "hello"
    assert disp == "hello"


def test_build_acp_user_turn_image_empty_data_becomes_text_not_broken_url():
    parts = [{"type": "image", "mime_type": "image/png", "data": ""}]
    c, disp = acp_router._build_acp_user_turn_from_ws({"text": "", "prompt_parts": parts})
    assert isinstance(c, str)
    assert "无有效" in c
    assert "[图片/空]" in disp
    assert "data:image/png;base64," not in disp


def test_build_acp_user_turn_text_plus_image_opencv_style():
    parts = [
        {"type": "text", "text": "What is this?"},
        {"type": "image", "mime_type": "image/png", "data": "AAA"},
    ]
    c, disp = acp_router._build_acp_user_turn_from_ws({"text": "", "prompt_parts": parts})
    assert isinstance(c, list)
    assert c[0] == {"type": "text", "text": "What is this?"}
    assert c[1]["type"] == "image_url"
    assert c[1]["image_url"]["url"] == "data:image/png;base64,AAA"
    assert "[图片 1]" in disp


def test_thin_resolve_image_payload_from_file_uri(tmp_path):
    thin = _load_thin_server_module()
    img = tmp_path / "pixel.png"
    raw = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8
    img.write_bytes(raw)
    mime, payload = thin._resolve_image_payload_for_cloud(
        {"mimeType": "image/png", "data": "", "uri": img.as_uri()}
    )
    assert mime == "image/png"
    assert payload == base64.standard_b64encode(raw).decode("ascii")


def test_thin_serialize_acp_prompt_blocks():
    thin = _load_thin_server_module()
    wire, plain = thin._serialize_acp_prompt_for_cloud(
        [
            {"type": "text", "text": "Hi"},
            {"type": "image", "mimeType": "image/jpeg", "data": "QQ"},
        ]
    )
    assert wire[0] == {"type": "text", "text": "Hi"}
    assert wire[1]["type"] == "image"
    assert wire[1]["mime_type"] == "image/jpeg"
    assert wire[1]["data"] == "QQ"
    assert "Hi" in plain and "[图片]" in plain


def test_thin_resource_link_png_file_uri_inlines_as_image(tmp_path):
    """JetBrains-style attachment: resource_link + file:// temp png, not type=image."""
    thin = _load_thin_server_module()
    img = tmp_path / "ai-chat-attachment-18384789275225456650.png"
    raw = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
    img.write_bytes(raw)
    wire, plain = thin._serialize_acp_prompt_for_cloud(
        [
            {"type": "text", "text": "这张图里主要有什么？"},
            {
                "type": "resource_link",
                "name": img.name,
                "uri": img.as_uri(),
            },
        ]
    )
    assert wire[0] == {"type": "text", "text": "这张图里主要有什么？"}
    assert wire[1]["type"] == "image"
    assert wire[1]["mime_type"] == "image/png"
    assert wire[1]["data"] == base64.standard_b64encode(raw).decode("ascii")
    assert wire[1]["data"]
    assert not any(p.get("type") == "resource_link" for p in wire)
    assert "[图片]" in plain


@pytest.mark.asyncio
async def test_thin_initialize_declares_image_prompt_capability():
    thin = _load_thin_server_module()
    agent = thin.ClawithThinClientAgent("WL4", "cw-x", "http://localhost:8008")
    init = await agent.initialize(1, client_capabilities=None, client_info=None)
    assert init.agent_capabilities is not None
    assert init.agent_capabilities.prompt_capabilities.image is True
    assert init.agent_capabilities.load_session is True
