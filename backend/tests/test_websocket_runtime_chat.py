"""WebSocket cutover tests for durable native Web Chat runs."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import uuid

from fastapi import WebSocketDisconnect
import pytest

from app.api.websocket import WebSocketChatHandler
from app.models.user import User
from app.services.agent_runtime.chat_intake import ChatRuntimeIntake
from app.services.agent_runtime.chat_stream import ChatRuntimeStreamOutcome
from app.services.agent_runtime.contracts import (
    CancelRunCommand,
    RunHandle,
    RuntimeEventCursor,
)


class _WebSocket:
    def __init__(self, *incoming: dict) -> None:
        self.incoming = list(incoming)
        self.sent: list[dict] = []

    async def receive_json(self):
        if not self.incoming:
            raise WebSocketDisconnect()
        return self.incoming.pop(0)

    async def send_json(self, packet: dict) -> None:
        self.sent.append(packet)


class _Transaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class _Session:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    def begin(self):
        return _Transaction()


def _handler(websocket: _WebSocket) -> WebSocketChatHandler:
    user = User(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        display_name="Ada",
        role="member",
        is_active=True,
    )
    handler = WebSocketChatHandler(
        websocket,  # type: ignore[arg-type]
        uuid.uuid4(),
        "token",
    )
    handler.user = user
    handler.agent_type = "native"
    handler.agent_name = "Analyst"
    handler.conv_id = str(uuid.uuid4())
    handler.history_messages = [SimpleNamespace()]
    handler.conversation = []
    return handler


def _handle(tenant_id: uuid.UUID) -> RunHandle:
    run_id = uuid.uuid4()
    return RunHandle(
        tenant_id=tenant_id,
        run_id=run_id,
        thread_id=str(run_id),
        command_id=uuid.uuid4(),
        runtime_type="langgraph",
        created=True,
    )


@pytest.mark.asyncio
async def test_native_message_uses_runtime_without_entering_legacy_tool_loop() -> None:
    websocket = _WebSocket({"content": "Investigate the issue"})
    handler = _handler(websocket)
    model = SimpleNamespace(id=uuid.uuid4())
    handle = _handle(handler.user.tenant_id)
    intake = ChatRuntimeIntake(
        handle=handle,
        message_id=uuid.uuid4(),
        resumed=False,
    )
    outcome = ChatRuntimeStreamOutcome(
        status="completed",
        content="Investigation complete",
        cursor=RuntimeEventCursor(
            datetime(2026, 7, 14, 10, 0, tzinfo=UTC),
            uuid.uuid4(),
        ),
    )

    with (
        patch.object(handler, "_resolve_effective_model", new=AsyncMock(return_value=model)),
        patch.object(handler, "_check_quotas", new=AsyncMock(return_value=True)),
        patch.object(handler, "_enqueue_runtime_chat", new=AsyncMock(return_value=intake)) as enqueue,
        patch.object(
            handler,
            "_run_runtime_and_stream",
            new=AsyncMock(return_value=(outcome, [])),
        ) as run_runtime,
        patch.object(handler, "_save_user_message", new=AsyncMock()) as legacy_save,
        patch.object(handler, "_run_llm_and_stream", new=AsyncMock()) as legacy_llm,
    ):
        with pytest.raises(WebSocketDisconnect):
            await handler.message_loop()

    enqueue.assert_awaited_once()
    assert enqueue.await_args.kwargs["content"] == "Investigate the issue"
    assert enqueue.await_args.kwargs["model_id"] == model.id
    run_runtime.assert_awaited_once_with(
        intake,
        user_content="Investigate the issue",
    )
    legacy_save.assert_not_awaited()
    legacy_llm.assert_not_awaited()
    assert handler.conversation == [
        {"role": "user", "content": "Investigate the issue"},
        {"role": "assistant", "content": "Investigation complete"},
    ]


@pytest.mark.asyncio
async def test_next_message_resumes_the_exact_wait_returned_on_this_socket() -> None:
    websocket = _WebSocket({"content": "Yes, publish it"})
    handler = _handler(websocket)
    handler.waiting_runtime_run_id = uuid.uuid4()
    handler.waiting_runtime_correlation_id = "publish-confirmation"
    model = SimpleNamespace(id=uuid.uuid4())
    intake = ChatRuntimeIntake(
        handle=_handle(handler.user.tenant_id),
        message_id=uuid.uuid4(),
        resumed=True,
    )

    with (
        patch.object(handler, "_resolve_effective_model", new=AsyncMock(return_value=model)),
        patch.object(handler, "_check_quotas", new=AsyncMock(return_value=True)),
        patch.object(handler, "_enqueue_runtime_chat", new=AsyncMock(return_value=intake)) as enqueue,
        patch.object(
            handler,
            "_run_runtime_and_stream",
            new=AsyncMock(return_value=(None, [])),
        ),
    ):
        with pytest.raises(WebSocketDisconnect):
            await handler.message_loop()

    assert enqueue.await_args.kwargs["resume_run_id"] == handler.waiting_runtime_run_id
    assert enqueue.await_args.kwargs["resume_correlation_id"] == "publish-confirmation"


@pytest.mark.asyncio
async def test_abort_enqueues_a_durable_cancel_command() -> None:
    handler = _handler(_WebSocket())
    handle = _handle(handler.user.tenant_id)

    with (
        patch("app.api.websocket.async_session", return_value=_Session()),
        patch(
            "app.api.websocket.TransactionalAgentRuntimeAdapter.cancel_run",
            new=AsyncMock(return_value=handle),
        ) as cancel_run,
    ):
        await handler._cancel_runtime_run(handle)

    command = cancel_run.await_args.args[0]
    assert isinstance(command, CancelRunCommand)
    assert command.run_id == handle.run_id
    assert command.idempotency_key == f"cancel:web:{handle.run_id}"
    assert command.actor_user_id == handler.user.id
