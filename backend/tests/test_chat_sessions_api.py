"""Focused tests for the tenant-scoped Direct Chat API lifecycle."""

from collections import deque
from datetime import UTC, datetime
from types import SimpleNamespace
import uuid

import pytest
from sqlalchemy.dialects import postgresql

from app.api import chat_sessions as chat_sessions_api
from app.services.chat_session_service import DirectSessionDeletion


class DummyResult:
    def __init__(self, values=None, scalar_value=None):
        self._values = list(values or [])
        self._scalar_value = scalar_value

    def scalar_one_or_none(self):
        if self._values:
            return self._values[0]
        return self._scalar_value

    def scalars(self):
        return self

    def all(self):
        return list(self._values)


class RecordingDB:
    def __init__(self, *responses):
        self.responses = deque(responses)
        self.statements = []
        self.committed = False
        self.refreshed = []

    async def execute(self, statement, _params=None):
        self.statements.append(statement)
        if not self.responses:
            raise AssertionError("unexpected execute() call")
        return self.responses.popleft()

    async def commit(self):
        self.committed = True

    async def refresh(self, value):
        self.refreshed.append(value)


def _sql(statement) -> str:
    return str(
        statement.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )


def _actor(*, role="member"):
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    return SimpleNamespace(
        id=user_id,
        tenant_id=tenant_id,
        role=role,
        display_name="Current User",
        avatar_url=None,
    )


def _agent(current_user, *, creator_id=None):
    return SimpleNamespace(
        id=uuid.uuid4(),
        tenant_id=current_user.tenant_id,
        creator_id=creator_id or current_user.id,
    )


def _session(agent, user_id, *, is_primary=False):
    now = datetime(2026, 7, 13, 12, 0, tzinfo=UTC)
    return SimpleNamespace(
        id=uuid.uuid4(),
        tenant_id=agent.tenant_id,
        session_type="direct",
        agent_id=agent.id,
        user_id=user_id,
        source_channel="web",
        title="Customer follow-up",
        created_at=now,
        updated_at=now,
        last_message_at=now,
        last_read_at_by_user=None,
        is_primary=is_primary,
    )


@pytest.mark.asyncio
async def test_list_all_direct_sessions_is_tenant_scoped_and_mocks_unread(monkeypatch):
    current_user = _actor(role="org_admin")
    agent = _agent(current_user, creator_id=uuid.uuid4())
    owner_id = uuid.uuid4()
    session = _session(agent, owner_id)
    db = RecordingDB(
        DummyResult([session]),
        DummyResult([(str(session.id), 3)]),
        DummyResult([]),
        DummyResult([(owner_id, "Alice")]),
    )

    async def fake_check_agent_access(_db, _user, _agent_id):
        return agent, "manage"

    monkeypatch.setattr(chat_sessions_api, "check_agent_access", fake_check_agent_access)

    sessions = await chat_sessions_api.list_sessions(
        agent_id=agent.id,
        scope="all",
        current_user=current_user,
        db=db,
    )

    assert len(sessions) == 1
    assert sessions[0].user_id == str(owner_id)
    assert sessions[0].username == "Alice"
    assert sessions[0].unread_count == 0
    session_sql = _sql(db.statements[0])
    assert f"chat_sessions.tenant_id = '{current_user.tenant_id}'" in session_sql
    assert "chat_sessions.session_type = 'direct'" in session_sql
    assert "chat_sessions.deleted_at IS NULL" in session_sql
    for statement in db.statements[1:3]:
        scoped_sql = _sql(statement)
        assert "chat_sessions.session_type = 'direct'" in scoped_sql
        assert "chat_sessions.deleted_at IS NULL" in scoped_sql


@pytest.mark.asyncio
async def test_create_resolves_same_tenant_user_and_participant(monkeypatch):
    current_user = _actor()
    agent = _agent(current_user)
    participant = SimpleNamespace(id=uuid.uuid4())
    created = _session(agent, current_user.id, is_primary=True)
    db = RecordingDB(DummyResult([current_user]))
    captured = {}

    async def fake_check_agent_access(_db, _user, _agent_id):
        return agent, "manage"

    async def fake_get_or_create_participant(_db, user_id, display_name, avatar_url):
        captured["participant"] = (user_id, display_name, avatar_url)
        return participant

    async def fake_create_direct_session(_db, **kwargs):
        captured["create"] = kwargs
        return created

    monkeypatch.setattr(chat_sessions_api, "check_agent_access", fake_check_agent_access)
    monkeypatch.setattr(
        chat_sessions_api,
        "get_or_create_user_participant",
        fake_get_or_create_participant,
    )
    monkeypatch.setattr(
        chat_sessions_api,
        "create_direct_session",
        fake_create_direct_session,
    )

    result = await chat_sessions_api.create_session(
        agent_id=agent.id,
        body=chat_sessions_api.CreateSessionIn(title="Topic"),
        current_user=current_user,
        db=db,
    )

    assert result.agent_id == str(agent.id)
    assert result.user_id == str(current_user.id)
    assert result.is_primary is True
    assert captured["participant"] == (
        current_user.id,
        current_user.display_name,
        current_user.avatar_url,
    )
    assert captured["create"] == {
        "tenant_id": current_user.tenant_id,
        "agent_id": agent.id,
        "user_id": current_user.id,
        "created_by_participant_id": participant.id,
        "title": "Topic",
    }
    user_sql = _sql(db.statements[0])
    assert f"users.tenant_id = '{current_user.tenant_id}'" in user_sql
    assert "users.is_active IS true" in user_sql
    assert db.committed is True
    assert db.refreshed == [created]


@pytest.mark.asyncio
async def test_rename_filters_tenant_direct_and_deleted(monkeypatch):
    current_user = _actor()
    agent = _agent(current_user)
    session = _session(agent, current_user.id)
    db = RecordingDB(DummyResult([session]))

    async def fake_check_agent_access(_db, _user, _agent_id):
        return agent, "manage"

    monkeypatch.setattr(chat_sessions_api, "check_agent_access", fake_check_agent_access)

    result = await chat_sessions_api.rename_session(
        agent_id=agent.id,
        session_id=session.id,
        body=chat_sessions_api.PatchSessionIn(title="Renamed"),
        current_user=current_user,
        db=db,
    )

    assert result == {"id": str(session.id), "title": "Renamed"}
    sql = _sql(db.statements[0])
    assert f"chat_sessions.tenant_id = '{current_user.tenant_id}'" in sql
    assert "chat_sessions.session_type = 'direct'" in sql
    assert "chat_sessions.deleted_at IS NULL" in sql


@pytest.mark.asyncio
async def test_delete_delegates_soft_delete_without_physical_message_delete(monkeypatch):
    current_user = _actor()
    agent = _agent(current_user)
    session = _session(agent, current_user.id, is_primary=True)
    db = RecordingDB(DummyResult([session]))
    calls = []

    async def fake_check_agent_access(_db, _user, _agent_id):
        return agent, "manage"

    async def fake_soft_delete(_db, **kwargs):
        calls.append(kwargs)
        return DirectSessionDeletion(session, None, ())

    monkeypatch.setattr(chat_sessions_api, "check_agent_access", fake_check_agent_access)
    monkeypatch.setattr(chat_sessions_api, "soft_delete_direct_session", fake_soft_delete)

    result = await chat_sessions_api.delete_session(
        agent_id=agent.id,
        session_id=session.id,
        current_user=current_user,
        db=db,
    )

    assert result is None
    assert calls == [
        {
            "tenant_id": current_user.tenant_id,
            "agent_id": agent.id,
            "user_id": current_user.id,
            "session_id": session.id,
            "actor_user_id": current_user.id,
        }
    ]
    assert db.committed is True
    assert all(statement.__class__.__name__ != "Delete" for statement in db.statements)


@pytest.mark.asyncio
async def test_messages_use_created_at_id_cursor_and_plain_defaults(monkeypatch):
    current_user = _actor(role="org_admin")
    agent = _agent(current_user, creator_id=uuid.uuid4())
    owner_id = uuid.uuid4()
    session = _session(agent, owner_id)
    message_id = uuid.uuid4()
    created_at = datetime(2026, 7, 13, 11, 0, tzinfo=UTC)
    message = SimpleNamespace(
        id=message_id,
        role="user",
        content="hello",
        created_at=created_at,
        participant_id=None,
        thinking=None,
    )
    before_id = uuid.uuid4()
    before_at = datetime(2026, 7, 13, 12, 0, tzinfo=UTC)
    db = RecordingDB(DummyResult([session]), DummyResult([message]))

    async def fake_check_agent_access(_db, _user, _agent_id):
        return agent, "manage"

    monkeypatch.setattr(chat_sessions_api, "check_agent_access", fake_check_agent_access)

    messages = await chat_sessions_api.get_session_messages(
        agent_id=agent.id,
        session_id=session.id,
        limit=20,
        before=f"{before_at.isoformat()}|{before_id}",
        current_user=current_user,
        db=db,
    )

    assert messages == [
        {
            "id": str(message_id),
            "role": "user",
            "content": "hello",
            "created_at": created_at.isoformat(),
            "cursor": f"{created_at.isoformat()}|{message_id}",
        }
    ]
    sql = _sql(db.statements[1])
    assert f"chat_sessions.tenant_id = '{current_user.tenant_id}'" in sql
    assert "chat_sessions.session_type = 'direct'" in sql
    assert "chat_sessions.deleted_at IS NULL" in sql
    assert "(chat_messages.created_at, chat_messages.id) <" in sql
    assert "ORDER BY chat_messages.created_at DESC, chat_messages.id DESC" in sql
    assert chat_sessions_api.get_session_messages.__defaults__[0] == 20
    assert chat_sessions_api.get_session_messages.__defaults__[1] is None


def test_session_out_accepts_unified_nullable_agent_and_user_ids():
    value = chat_sessions_api.SessionOut(
        id=str(uuid.uuid4()),
        title="System session",
        created_at=datetime.now(UTC).isoformat(),
    )

    assert value.agent_id is None
    assert value.user_id is None
