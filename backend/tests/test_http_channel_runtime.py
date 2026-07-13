"""HTTP channel webhooks must submit messages to the durable Runtime."""

from __future__ import annotations

from datetime import UTC, datetime
import json
from types import SimpleNamespace
import uuid

import pytest

from app import database
from app.api import feishu as feishu_api
from app.api import dingtalk, slack, teams, whatsapp
from app.services import activity_logger, channel_session
from app.services.agent_runtime.channel_chat import ChannelChatOutcome
from app.services.agent_runtime.chat_intake import ChatRuntimeIntake
from app.services.agent_runtime.contracts import RunHandle, RuntimeEventCursor
from app.services.channel_user_service import channel_user_service


class _Result:
    def __init__(self, value: object) -> None:
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class _Session:
    def __init__(self, *results: object) -> None:
        self.results = iter(results)
        self.commits = 0
        self.flushes = 0
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    async def execute(self, _statement):
        return _Result(next(self.results))

    async def commit(self) -> None:
        self.commits += 1

    async def flush(self) -> None:
        self.flushes += 1

    async def close(self) -> None:
        self.closed = True


class _Request:
    def __init__(self, body: dict) -> None:
        self._body = json.dumps(body).encode()
        self.headers: dict[str, str] = {}

    async def body(self) -> bytes:
        return self._body

    async def json(self) -> dict:
        return json.loads(self._body)


class _SessionFactory:
    def __init__(self, session: _Session) -> None:
        self.session = session

    def __call__(self):
        return self.session


def _runtime(tenant_id: uuid.UUID):
    run_id = uuid.uuid4()
    cursor = RuntimeEventCursor(
        created_at=datetime(2026, 7, 14, 12, 0, tzinfo=UTC),
        event_id=uuid.uuid4(),
    )
    handle = RunHandle(
        tenant_id=tenant_id,
        run_id=run_id,
        thread_id=str(run_id),
        command_id=uuid.uuid4(),
        runtime_type="langgraph",
        created=True,
    )
    return (
        ChatRuntimeIntake(
            handle=handle,
            message_id=uuid.uuid4(),
            resumed=False,
            stream_after=cursor,
        ),
        cursor,
    )


@pytest.mark.asyncio
async def test_slack_webhook_uses_runtime_intake(monkeypatch) -> None:
    tenant_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    user_id = uuid.uuid4()
    session_id = uuid.uuid4()
    event_id = f"slack-event-{uuid.uuid4()}"
    config = SimpleNamespace(encrypt_key="", app_secret="")
    agent = SimpleNamespace(id=agent_id, tenant_id=tenant_id, creator_id=uuid.uuid4())
    user = SimpleNamespace(id=user_id, display_name="Slack User U123")
    session = SimpleNamespace(id=session_id)
    model = SimpleNamespace(id=uuid.uuid4())
    db = _Session(config, agent)
    intake, cursor = _runtime(tenant_id)
    calls: dict[str, object] = {}

    async def resolve_user(**_kwargs):
        return user

    async def find_session(**kwargs):
        calls["session"] = kwargs
        return session

    async def load_model(_db, _agent_id):
        return agent, model, None

    async def enqueue(_db, **kwargs):
        calls["intake"] = kwargs
        return intake

    async def wait(**kwargs):
        calls["wait"] = kwargs
        return ChannelChatOutcome("completed", "Slack Runtime reply", uuid.uuid4())

    monkeypatch.setattr(channel_user_service, "resolve_channel_user", resolve_user)
    monkeypatch.setattr(channel_session, "find_or_create_channel_session", find_session)
    monkeypatch.setattr(feishu_api, "_load_agent_and_model", load_model)
    monkeypatch.setattr(slack, "enqueue_channel_chat_runtime", enqueue)
    monkeypatch.setattr(slack, "wait_for_channel_chat", wait)

    result = await slack.slack_event_webhook(
        agent_id,
        _Request(
            {
                "type": "event_callback",
                "event_id": event_id,
                "event": {
                    "type": "message",
                    "channel": "D123",
                    "user": "U123",
                    "text": "Hello Slack",
                },
            }
        ),  # type: ignore[arg-type]
        db,  # type: ignore[arg-type]
    )

    assert result == {"ok": True}
    assert db.commits == 1
    assert db.closed is True
    session_call = calls["session"]
    assert isinstance(session_call, dict)
    assert session_call["created_by_user_id"] == user_id
    intake_call = calls["intake"]
    assert isinstance(intake_call, dict)
    assert intake_call["source_channel"] == "slack"
    assert intake_call["message_id"] == slack.channel_message_id(
        agent_id,
        "slack",
        event_id,
    )
    assert calls["wait"] == {
        "handle": intake.handle,
        "session_id": session_id,
        "session_factory": slack._async_session,
        "after": cursor,
    }


@pytest.mark.asyncio
async def test_teams_webhook_uses_runtime_intake(monkeypatch) -> None:
    tenant_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    user_id = uuid.uuid4()
    session_id = uuid.uuid4()
    activity_id = f"teams-activity-{uuid.uuid4()}"
    config = SimpleNamespace(
        app_id="bot-1",
        app_secret="",
        extra_config={"use_managed_identity": False},
        is_connected=False,
    )
    agent = SimpleNamespace(id=agent_id, tenant_id=tenant_id, creator_id=uuid.uuid4())
    user = SimpleNamespace(id=user_id, display_name="Teams User sender-1")
    session = SimpleNamespace(id=session_id)
    model = SimpleNamespace(id=uuid.uuid4())
    db = _Session(config, agent)
    intake, cursor = _runtime(tenant_id)
    calls: dict[str, object] = {}

    async def resolve_user(**_kwargs):
        return user

    async def find_session(**kwargs):
        calls["session"] = kwargs
        return session

    async def load_model(_db, _agent_id):
        return agent, model, None

    async def enqueue(_db, **kwargs):
        calls["intake"] = kwargs
        return intake

    async def wait(**kwargs):
        calls["wait"] = kwargs
        return ChannelChatOutcome("completed", "Teams Runtime reply", uuid.uuid4())

    monkeypatch.setattr(channel_user_service, "resolve_channel_user", resolve_user)
    monkeypatch.setattr(teams, "find_or_create_channel_session", find_session)
    monkeypatch.setattr(teams, "_load_agent_and_model", load_model)
    monkeypatch.setattr(teams, "enqueue_channel_chat_runtime", enqueue)
    monkeypatch.setattr(teams, "wait_for_channel_chat", wait)

    result = await teams.teams_event_webhook(
        agent_id,
        _Request(
            {
                "type": "message",
                "id": activity_id,
                "text": "Hello Teams",
                "from": {"id": "sender-1", "name": "Alice"},
                "recipient": {"id": "bot-1"},
                "conversation": {
                    "id": "teams-conversation-1",
                    "conversationType": "personal",
                },
            }
        ),  # type: ignore[arg-type]
        db,  # type: ignore[arg-type]
    )

    assert result == {"ok": True}
    assert db.commits == 1
    assert db.closed is True
    intake_call = calls["intake"]
    assert isinstance(intake_call, dict)
    assert intake_call["source_channel"] == "microsoft_teams"
    assert intake_call["message_id"] == teams.channel_message_id(
        agent_id,
        "microsoft_teams",
        activity_id,
    )
    assert calls["wait"] == {
        "handle": intake.handle,
        "session_id": session_id,
        "session_factory": teams._async_session,
        "after": cursor,
    }


@pytest.mark.asyncio
async def test_whatsapp_webhook_uses_runtime_intake(monkeypatch) -> None:
    tenant_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    user_id = uuid.uuid4()
    session_id = uuid.uuid4()
    provider_message_id = f"wamid-{uuid.uuid4()}"
    config = SimpleNamespace(encrypt_key="", app_id="phone-number-1", app_secret="token-1")
    agent = SimpleNamespace(id=agent_id, tenant_id=tenant_id)
    user = SimpleNamespace(id=user_id)
    session = SimpleNamespace(id=session_id)
    model = SimpleNamespace(id=uuid.uuid4())
    db = _Session(config, agent)
    intake, cursor = _runtime(tenant_id)
    calls: dict[str, object] = {}

    async def resolve_user(**_kwargs):
        return user

    async def find_session(**kwargs):
        calls["session"] = kwargs
        return session

    async def load_model(_db, _agent_id):
        return agent, model, None

    async def enqueue(_db, **kwargs):
        calls["intake"] = kwargs
        return intake

    async def wait(**kwargs):
        calls["wait"] = kwargs
        return ChannelChatOutcome("completed", "WhatsApp Runtime reply", uuid.uuid4())

    async def send(_config, to_phone, text):
        calls["send"] = (to_phone, text)

    monkeypatch.setattr(channel_user_service, "resolve_channel_user", resolve_user)
    monkeypatch.setattr(channel_session, "find_or_create_channel_session", find_session)
    monkeypatch.setattr(feishu_api, "_load_agent_and_model", load_model)
    monkeypatch.setattr(whatsapp, "enqueue_channel_chat_runtime", enqueue)
    monkeypatch.setattr(whatsapp, "wait_for_channel_chat", wait)
    monkeypatch.setattr(whatsapp, "_send_whatsapp_messages", send)

    result = await whatsapp.whatsapp_event_webhook(
        agent_id,
        _Request(
            {
                "entry": [
                    {
                        "changes": [
                            {
                                "value": {
                                    "contacts": [{"profile": {"name": "Alice"}}],
                                    "messages": [
                                        {
                                            "id": provider_message_id,
                                            "from": "15551234567",
                                            "type": "text",
                                            "text": {"body": "Hello WhatsApp"},
                                        }
                                    ],
                                }
                            }
                        ]
                    }
                ]
            }
        ),  # type: ignore[arg-type]
        db,  # type: ignore[arg-type]
    )

    assert result == {"ok": True}
    assert db.commits == 1
    intake_call = calls["intake"]
    assert isinstance(intake_call, dict)
    assert intake_call["source_channel"] == "whatsapp"
    assert intake_call["message_id"] == whatsapp.channel_message_id(
        agent_id,
        "whatsapp",
        provider_message_id,
    )
    assert calls["wait"] == {
        "handle": intake.handle,
        "session_id": session_id,
        "session_factory": whatsapp._async_session,
        "after": cursor,
    }
    assert calls["send"] == ("15551234567", "WhatsApp Runtime reply")


@pytest.mark.asyncio
async def test_dingtalk_message_uses_runtime_and_group_scope(monkeypatch) -> None:
    import httpx

    tenant_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    user_id = uuid.uuid4()
    session_id = uuid.uuid4()
    provider_message_id = f"dingtalk-message-{uuid.uuid4()}"
    agent = SimpleNamespace(
        id=agent_id,
        tenant_id=tenant_id,
        creator_id=uuid.uuid4(),
        name="Runtime Agent",
    )
    user = SimpleNamespace(id=user_id)
    session = SimpleNamespace(id=session_id)
    model = SimpleNamespace(id=uuid.uuid4())
    db = _Session(agent, None)
    session_factory = _SessionFactory(db)
    intake, cursor = _runtime(tenant_id)
    calls: dict[str, object] = {"posts": []}

    async def resolve_user(**_kwargs):
        return user

    async def find_session(**kwargs):
        calls["session"] = kwargs
        return session

    async def load_model(_db, _agent_id):
        return agent, model, None

    async def enqueue(_db, **kwargs):
        calls["intake"] = kwargs
        return intake

    async def wait(**kwargs):
        calls["wait"] = kwargs
        return ChannelChatOutcome("completed", "DingTalk Runtime reply", uuid.uuid4())

    async def log(*_args, **_kwargs):
        return None

    class _HTTPClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        async def post(self, url, **kwargs):
            calls["posts"].append((url, kwargs))  # type: ignore[union-attr]
            return SimpleNamespace()

    monkeypatch.setattr(database, "async_session", session_factory)
    monkeypatch.setattr(channel_user_service, "resolve_channel_user", resolve_user)
    monkeypatch.setattr(channel_session, "find_or_create_channel_session", find_session)
    monkeypatch.setattr(feishu_api, "_load_agent_and_model", load_model)
    monkeypatch.setattr(dingtalk, "enqueue_channel_chat_runtime", enqueue)
    monkeypatch.setattr(dingtalk, "wait_for_channel_chat", wait)
    monkeypatch.setattr(activity_logger, "log_activity", log)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **_kwargs: _HTTPClient())

    await dingtalk.process_dingtalk_message(
        agent_id=agent_id,
        sender_staff_id="staff-1",
        user_text="Hello DingTalk",
        conversation_id="group-1",
        conversation_type="2",
        session_webhook="https://dingtalk.example/session",
        message_id=provider_message_id,
    )

    assert db.commits == 1
    session_call = calls["session"]
    assert isinstance(session_call, dict)
    assert session_call["is_group"] is True
    assert session_call["created_by_user_id"] == user_id
    intake_call = calls["intake"]
    assert isinstance(intake_call, dict)
    assert intake_call["source_channel"] == "dingtalk"
    assert intake_call["message_id"] == dingtalk.channel_message_id(
        agent_id,
        "dingtalk",
        provider_message_id,
    )
    assert calls["wait"] == {
        "handle": intake.handle,
        "session_id": session_id,
        "session_factory": session_factory,
        "after": cursor,
    }
    posts = calls["posts"]
    assert isinstance(posts, list)
    assert posts[0][1]["json"]["markdown"]["text"] == "DingTalk Runtime reply"
