"""External channel attachment tests for stable Runtime delivery events."""

from __future__ import annotations

from collections import deque
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
import uuid

import pytest

from app.models.audit import ChatMessage
from app.services.agent_runtime import channel_chat
from app.services.agent_runtime.channel_chat import (
    channel_message_id,
    enqueue_channel_chat_runtime,
    wait_for_channel_chat,
)
from app.services.agent_runtime.chat_intake import ChatRuntimeIntake
from app.services.agent_runtime.contracts import (
    RunHandle,
    RuntimeEvent,
    RuntimeEventCursor,
)


class _Result:
    def __init__(self, value: object) -> None:
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class _Session:
    def __init__(self, message: ChatMessage) -> None:
        self.message = message

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    async def execute(self, _statement):
        return _Result(self.message)


class _SessionFactory:
    def __init__(self, *sessions: _Session) -> None:
        self.sessions = deque(sessions)

    def __call__(self):
        return self.sessions.popleft()


class _EventSource:
    def __init__(
        self,
        events: list[RuntimeEvent],
        *,
        expected_after: RuntimeEventCursor | None = None,
    ) -> None:
        self.events = events
        self.expected_after = expected_after

    async def stream_run(self, _handle, *, after=None):
        assert after == self.expected_after
        for event in self.events:
            yield event


def _handle() -> RunHandle:
    run_id = uuid.uuid4()
    return RunHandle(
        tenant_id=uuid.uuid4(),
        run_id=run_id,
        thread_id=str(run_id),
        command_id=uuid.uuid4(),
        runtime_type="langgraph",
        created=True,
    )


def _event(
    handle: RunHandle,
    event_type: str,
    position: int,
    payload: dict,
) -> RuntimeEvent:
    return RuntimeEvent(
        tenant_id=handle.tenant_id,
        run_id=handle.run_id,
        event_id=uuid.uuid4(),
        event_type=event_type,  # type: ignore[arg-type]
        payload=payload,
        checkpoint_id=f"checkpoint-{position}",
        created_at=datetime(2026, 7, 14, 12, 0, tzinfo=UTC)
        + timedelta(seconds=position),
    )


@pytest.mark.asyncio
async def test_external_group_waiter_loads_the_persisted_terminal_message() -> None:
    handle = _handle()
    session_id = uuid.uuid4()
    message = ChatMessage(
        id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        user_id=None,
        role="assistant",
        content="Channel result",
        conversation_id=str(session_id),
        mentions=[],
    )
    events = [
        _event(handle, "run_completed", 1, {"status": "completed"}),
        _event(
            handle,
            "delivery_succeeded",
            2,
            {
                "delivery_kind": "terminal",
                "lifecycle_status": "completed",
                "message_id": str(message.id),
            },
        ),
    ]

    outcome = await wait_for_channel_chat(
        handle=handle,
        session_id=session_id,
        session_factory=_SessionFactory(_Session(message)),  # type: ignore[arg-type]
        event_source=_EventSource(events),
    )

    assert outcome.status == "completed"
    assert outcome.content == "Channel result"
    assert outcome.message_id == message.id


@pytest.mark.asyncio
async def test_channel_waiter_returns_waiting_correlation_for_next_message() -> None:
    handle = _handle()
    session_id = uuid.uuid4()
    message = ChatMessage(
        id=uuid.uuid4(),
        role="assistant",
        content="Approve publishing?",
        conversation_id=str(session_id),
        mentions=[],
    )
    events = [
        _event(
            handle,
            "waiting_started",
            1,
            {
                "status": "waiting_user",
                "waiting_type": "user",
                "correlation_id": "publish-1",
            },
        ),
        _event(
            handle,
            "delivery_succeeded",
            2,
            {
                "delivery_kind": "waiting",
                "lifecycle_status": "waiting_user",
                "message_id": str(message.id),
            },
        ),
    ]

    outcome = await wait_for_channel_chat(
        handle=handle,
        session_id=session_id,
        session_factory=_SessionFactory(_Session(message)),  # type: ignore[arg-type]
        event_source=_EventSource(events),
    )

    assert outcome.status == "waiting_user"
    assert outcome.correlation_id == "publish-1"


def test_channel_message_id_is_stable_for_provider_retries() -> None:
    agent_id = uuid.uuid4()

    first = channel_message_id(agent_id, "wechat", "provider-message-1")
    retry = channel_message_id(agent_id, "wechat", "provider-message-1")
    other_channel = channel_message_id(agent_id, "slack", "provider-message-1")

    assert first == retry
    assert first != other_channel


@pytest.mark.asyncio
async def test_channel_intake_resumes_the_latest_waiting_run(monkeypatch) -> None:
    tenant_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    user_id = uuid.uuid4()
    session_id = uuid.uuid4()
    waiting_run_id = uuid.uuid4()
    message_id = uuid.uuid4()
    handle = RunHandle(
        tenant_id=tenant_id,
        run_id=waiting_run_id,
        thread_id=str(waiting_run_id),
        command_id=uuid.uuid4(),
        runtime_type="langgraph",
        created=False,
    )
    expected = ChatRuntimeIntake(
        handle=handle,
        message_id=message_id,
        resumed=True,
    )
    captured: dict[str, object] = {}

    async def fake_waiting_resume(_db, **kwargs):
        captured["resume_scope"] = kwargs
        return waiting_run_id, "approval-7"

    async def fake_enqueue(_db, **kwargs):
        captured["enqueue"] = kwargs
        return expected

    monkeypatch.setattr(channel_chat, "_waiting_resume", fake_waiting_resume)
    monkeypatch.setattr(channel_chat, "enqueue_chat_runtime", fake_enqueue)

    result = await enqueue_channel_chat_runtime(
        object(),  # type: ignore[arg-type]
        agent=SimpleNamespace(id=agent_id, tenant_id=tenant_id),  # type: ignore[arg-type]
        user=SimpleNamespace(id=user_id),  # type: ignore[arg-type]
        session=SimpleNamespace(id=session_id),  # type: ignore[arg-type]
        model=SimpleNamespace(id=uuid.uuid4()),  # type: ignore[arg-type]
        content="approve",
        source_channel="wechat",
        message_id=message_id,
    )

    assert result is expected
    assert captured["resume_scope"] == {
        "tenant_id": tenant_id,
        "agent_id": agent_id,
        "session_id": session_id,
        "user_id": user_id,
    }
    enqueue = captured["enqueue"]
    assert isinstance(enqueue, dict)
    assert enqueue["resume_run_id"] == waiting_run_id
    assert enqueue["resume_correlation_id"] == "approval-7"
    assert enqueue["source_channel"] == "wechat"


@pytest.mark.asyncio
async def test_channel_waiter_starts_after_the_resume_cursor() -> None:
    handle = _handle()
    session_id = uuid.uuid4()
    message = ChatMessage(
        id=uuid.uuid4(),
        role="assistant",
        content="Approved",
        conversation_id=str(session_id),
        mentions=[],
    )
    cursor = RuntimeEventCursor(
        datetime(2026, 7, 14, 11, 59, tzinfo=UTC),
        uuid.uuid4(),
    )
    events = [
        _event(handle, "run_completed", 1, {"status": "completed"}),
        _event(
            handle,
            "delivery_succeeded",
            2,
            {
                "delivery_kind": "terminal",
                "lifecycle_status": "completed",
                "message_id": str(message.id),
            },
        ),
    ]

    outcome = await wait_for_channel_chat(
        handle=handle,
        session_id=session_id,
        session_factory=_SessionFactory(_Session(message)),  # type: ignore[arg-type]
        after=cursor,
        event_source=_EventSource(events, expected_after=cursor),
    )

    assert outcome.content == "Approved"
