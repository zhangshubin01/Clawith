"""Stable AgentRunEvent streaming and reconnect cursor tests."""

from __future__ import annotations

import asyncio
from collections import deque
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
import uuid

import pytest
from sqlalchemy.dialects import postgresql

from app.models.agent_run import AgentRun
from app.models.agent_run_event import AgentRunEvent
from app.services.agent_runtime.contracts import RunHandle, RuntimeEventCursor
from app.services.agent_runtime.event_stream import (
    DatabaseRuntimeEventStream,
    RuntimeEventStreamError,
)


class _Result:
    def __init__(self, *, scalar=None, rows=(), one=None) -> None:
        self.scalar_value = scalar
        self.rows = list(rows)
        self.one_value = one

    def scalar_one_or_none(self):
        return self.scalar_value

    def scalar(self):
        return self.scalar_value

    def one_or_none(self):
        return self.one_value

    def scalars(self):
        return self

    def all(self):
        return list(self.rows)


class _Session:
    def __init__(self, *results: _Result) -> None:
        self.results = deque(results)
        self.statements = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    async def execute(self, statement):
        self.statements.append(statement)
        return self.results.popleft()


class _SessionFactory:
    def __init__(self, *sessions: _Session) -> None:
        self.sessions = deque(sessions)

    def __call__(self) -> _Session:
        return self.sessions.popleft()


class _DispatchSession:
    """Reusable fake session whose results follow the queried table.

    The idle-liveness loop polls an unbounded number of times, so results are
    dispatched by statement shape instead of being queued in fixed order.
    """

    def __init__(
        self,
        *,
        run: AgentRun | None = None,
        event_batches: tuple[tuple[AgentRunEvent, ...], ...] = (),
        delivery_status: str = "pending",
        command_row: SimpleNamespace | None = None,
        lease_active: bool = False,
    ) -> None:
        self.run = run
        self.event_batches = deque(event_batches)
        self.delivery_status = delivery_status
        self.command_row = command_row
        self.lease_active = lease_active

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    async def execute(self, statement):
        columns = list(statement.selected_columns)
        table = None
        if columns:
            table = getattr(columns[0], "table", None)
            table = table.name if table is not None else None
        if table == "agent_run_events":
            batch = self.event_batches.popleft() if self.event_batches else []
            return _Result(rows=batch)
        if table == "agent_runs":
            if len(columns) == 1:
                return _Result(scalar=self.delivery_status)
            return _Result(scalar=self.run)
        if table == "agent_run_commands":
            return _Result(one=self.command_row)
        return _Result(scalar=self.lease_active)


def _run() -> tuple[AgentRun, RunHandle]:
    tenant_id = uuid.uuid4()
    run_id = uuid.uuid4()
    run = AgentRun(
        id=run_id,
        tenant_id=tenant_id,
        agent_id=uuid.uuid4(),
        source_type="chat",
        goal="answer",
        run_kind="foreground",
        model_id=uuid.uuid4(),
        runtime_type="langgraph",
        runtime_thread_id=str(run_id),
        graph_name="runtime_graph",
        graph_version="v1",
        lane_held=False,
        delivery_status="pending",
    )
    handle = RunHandle(
        tenant_id=tenant_id,
        run_id=run_id,
        thread_id=str(run_id),
        command_id=uuid.uuid4(),
        runtime_type="langgraph",
        created=True,
    )
    return run, handle


def _direct_thread_run() -> tuple[AgentRun, RunHandle]:
    run, handle = _run()
    session_thread_id = str(uuid.uuid4())
    run.runtime_thread_id = session_thread_id
    return run, RunHandle(
        tenant_id=handle.tenant_id,
        run_id=handle.run_id,
        thread_id=session_thread_id,
        command_id=handle.command_id,
        runtime_type="langgraph",
        created=handle.created,
    )


def _event(
    run: AgentRun,
    event_type: str,
    *,
    created_at: datetime,
    checkpoint_id: str | None = "checkpoint-1",
) -> AgentRunEvent:
    return AgentRunEvent(
        id=uuid.uuid4(),
        tenant_id=run.tenant_id,
        run_id=run.id,
        agent_id=run.agent_id,
        event_type=event_type,
        summary=event_type.replace("_", " "),
        payload={"status": event_type},
        artifact_refs=["artifact://one"],
        idempotency_key=f"event:{event_type}",
        source_checkpoint_id=checkpoint_id,
        created_at=created_at,
    )


@pytest.mark.asyncio
async def test_stream_yields_terminal_and_delivery_events_before_closing() -> None:
    run, handle = _run()
    base = datetime(2026, 7, 13, 18, 0, tzinfo=UTC)
    terminal = _event(run, "run_completed", created_at=base)
    delivered = _event(
        run,
        "delivery_succeeded",
        created_at=base + timedelta(microseconds=1),
        checkpoint_id=None,
    )
    factory = _SessionFactory(
        _Session(_Result(scalar=run)),
        _Session(
            _Result(rows=[terminal, delivered]),
            _Result(scalar="delivered"),
        ),
    )
    stream = DatabaseRuntimeEventStream(
        session_factory=factory,  # type: ignore[arg-type]
        poll_interval_seconds=0.001,
    )

    events = [event async for event in stream.stream_run(handle)]

    assert [event.event_type for event in events] == [
        "run_completed",
        "delivery_succeeded",
    ]
    assert events[0].event_id == terminal.id
    assert events[0].payload == {
        "status": "run_completed",
        "summary": "run completed",
        "artifact_refs": ["artifact://one"],
    }


@pytest.mark.asyncio
async def test_terminal_projection_waits_for_later_delivery_settlement() -> None:
    run, handle = _run()
    base = datetime(2026, 7, 13, 18, 0, tzinfo=UTC)
    terminal = _event(run, "run_failed", created_at=base)
    failed_delivery = _event(
        run,
        "delivery_failed",
        created_at=base + timedelta(seconds=1),
        checkpoint_id=None,
    )
    factory = _SessionFactory(
        _Session(_Result(scalar=run)),
        _Session(_Result(rows=[terminal]), _Result(scalar="pending")),
        _Session(_Result(rows=[failed_delivery]), _Result(scalar="failed")),
    )
    stream = DatabaseRuntimeEventStream(
        session_factory=factory,  # type: ignore[arg-type]
        poll_interval_seconds=0.001,
    )

    events = [event async for event in stream.stream_run(handle)]

    assert [event.event_type for event in events] == ["run_failed", "delivery_failed"]


@pytest.mark.asyncio
async def test_reconnect_cursor_uses_created_at_and_id_together() -> None:
    run, handle = _run()
    base = datetime(2026, 7, 13, 18, 0, tzinfo=UTC)
    cursor = RuntimeEventCursor(base, uuid.uuid4())
    terminal = _event(run, "run_completed", created_at=base)
    poll = _Session(
        _Result(rows=[terminal]),
        _Result(scalar="not_required"),
    )
    factory = _SessionFactory(_Session(_Result(scalar=run)), poll)
    stream = DatabaseRuntimeEventStream(
        session_factory=factory,  # type: ignore[arg-type]
        poll_interval_seconds=0.001,
    )

    events = [event async for event in stream.stream_run(handle, after=cursor)]

    assert len(events) == 1
    compiled = poll.statements[0].compile(
        dialect=postgresql.dialect(),
        compile_kwargs={"literal_binds": True},
    )
    sql = str(compiled)
    assert "agent_run_events.created_at >" in sql
    assert "agent_run_events.created_at =" in sql
    assert "agent_run_events.id >" in sql
    assert "ORDER BY agent_run_events.created_at ASC, agent_run_events.id ASC" in sql


@pytest.mark.asyncio
async def test_invalid_handle_is_rejected_before_database_access() -> None:
    run, handle = _run()
    del run
    invalid = RunHandle(
        tenant_id=handle.tenant_id,
        run_id=handle.run_id,
        thread_id="",
        command_id=handle.command_id,
        runtime_type="langgraph",
        created=handle.created,
    )
    stream = DatabaseRuntimeEventStream(
        session_factory=_SessionFactory(),  # type: ignore[arg-type]
        poll_interval_seconds=0.001,
    )

    with pytest.raises(RuntimeEventStreamError) as exc_info:
        await anext(stream.stream_run(invalid))

    assert exc_info.value.code == "runtime_identity_mismatch"


@pytest.mark.asyncio
async def test_direct_session_thread_handle_is_valid_even_when_thread_differs_from_run_id() -> None:
    run, handle = _direct_thread_run()
    base = datetime(2026, 7, 16, 18, 0, tzinfo=UTC)
    terminal = _event(run, "run_completed", created_at=base)
    delivered = _event(
        run,
        "delivery_succeeded",
        created_at=base + timedelta(microseconds=1),
        checkpoint_id=None,
    )
    factory = _SessionFactory(
        _Session(_Result(scalar=run)),
        _Session(
            _Result(rows=[terminal, delivered]),
            _Result(scalar="delivered"),
        ),
    )

    events = [
        event
        async for event in DatabaseRuntimeEventStream(
            session_factory=factory,  # type: ignore[arg-type]
            poll_interval_seconds=0.001,
        ).stream_run(handle)
    ]

    assert [event.event_type for event in events] == [
        "run_completed",
        "delivery_succeeded",
    ]


@pytest.mark.asyncio
async def test_event_stream_rejects_handle_thread_that_disagrees_with_stored_run() -> None:
    run, handle = _direct_thread_run()
    wrong = RunHandle(
        tenant_id=handle.tenant_id,
        run_id=handle.run_id,
        thread_id="wrong-thread",
        command_id=handle.command_id,
        runtime_type="langgraph",
        created=handle.created,
    )
    stream = DatabaseRuntimeEventStream(
        session_factory=_SessionFactory(_Session(_Result(scalar=run))),  # type: ignore[arg-type]
        poll_interval_seconds=0.001,
    )

    with pytest.raises(RuntimeEventStreamError) as exc_info:
        await anext(stream.stream_run(wrong))

    assert exc_info.value.code == "runtime_identity_mismatch"


@pytest.mark.asyncio
@pytest.mark.parametrize("wrong_identity", ("tenant", "run"))
async def test_event_stream_rejects_handle_outside_stored_tenant_run_scope(
    wrong_identity: str,
) -> None:
    _run_record, handle = _direct_thread_run()
    invalid = RunHandle(
        tenant_id=(uuid.uuid4() if wrong_identity == "tenant" else handle.tenant_id),
        run_id=(uuid.uuid4() if wrong_identity == "run" else handle.run_id),
        thread_id=handle.thread_id,
        command_id=handle.command_id,
        runtime_type="langgraph",
        created=handle.created,
    )
    stream = DatabaseRuntimeEventStream(
        session_factory=_SessionFactory(_Session(_Result(scalar=None))),  # type: ignore[arg-type]
        poll_interval_seconds=0.001,
    )

    with pytest.raises(RuntimeEventStreamError) as exc_info:
        await anext(stream.stream_run(invalid))

    assert exc_info.value.code == "run_not_found"


@pytest.mark.asyncio
async def test_idle_stream_waits_while_command_claim_is_fresh_then_raises_once_it_expires() -> None:
    run, handle = _run()
    base = datetime.now(UTC)
    first_event = _event(run, "status_changed", created_at=base)
    session = _DispatchSession(
        run=run,
        event_batches=((first_event,),),
        command_row=SimpleNamespace(
            status="claimed",
            claim_expires_at=base + timedelta(minutes=30),
        ),
        lease_active=False,
    )
    stream = DatabaseRuntimeEventStream(
        session_factory=lambda: session,  # type: ignore[arg-type]
        poll_interval_seconds=0.001,
        idle_timeout_seconds=0.05,
    )
    agent = stream.stream_run(handle)
    assert (await anext(agent)).event_id == first_event.id

    waiter = asyncio.create_task(anext(agent))
    await asyncio.sleep(0.3)
    assert not waiter.done(), "a live command claim must keep the idle stream open"

    session.command_row = SimpleNamespace(
        status="claimed",
        claim_expires_at=base - timedelta(seconds=1),
    )
    with pytest.raises(RuntimeEventStreamError) as exc_info:
        await asyncio.wait_for(waiter, timeout=2)
    assert exc_info.value.code == "runtime_event_stream_idle_timeout"


@pytest.mark.asyncio
async def test_idle_stream_keeps_waiting_while_command_is_pending() -> None:
    run, handle = _run()
    base = datetime.now(UTC)
    first_event = _event(run, "status_changed", created_at=base)
    session = _DispatchSession(
        run=run,
        event_batches=((first_event,),),
        command_row=SimpleNamespace(status="pending", claim_expires_at=None),
        lease_active=False,
    )
    stream = DatabaseRuntimeEventStream(
        session_factory=lambda: session,  # type: ignore[arg-type]
        poll_interval_seconds=0.001,
        idle_timeout_seconds=0.05,
    )
    agent = stream.stream_run(handle)
    assert (await anext(agent)).event_id == first_event.id

    waiter = asyncio.create_task(anext(agent))
    try:
        await asyncio.sleep(0.3)
        assert not waiter.done(), "a queued command must keep the idle stream open"
    finally:
        waiter.cancel()
        with suppress(asyncio.CancelledError):
            await waiter
    with suppress(Exception):
        await agent.aclose()


@pytest.mark.asyncio
async def test_idle_stream_keeps_waiting_while_active_tool_lease_outlives_command_claim() -> None:
    run, handle = _run()
    base = datetime.now(UTC)
    first_event = _event(run, "status_changed", created_at=base)
    session = _DispatchSession(
        run=run,
        event_batches=((first_event,),),
        command_row=SimpleNamespace(
            status="claimed",
            claim_expires_at=base - timedelta(seconds=1),
        ),
        lease_active=True,
    )
    stream = DatabaseRuntimeEventStream(
        session_factory=lambda: session,  # type: ignore[arg-type]
        poll_interval_seconds=0.001,
        idle_timeout_seconds=0.05,
    )
    agent = stream.stream_run(handle)
    assert (await anext(agent)).event_id == first_event.id

    waiter = asyncio.create_task(anext(agent))
    try:
        await asyncio.sleep(0.3)
        assert not waiter.done(), "an active tool lease must keep the idle stream open"
    finally:
        waiter.cancel()
        with suppress(asyncio.CancelledError):
            await waiter
    with suppress(Exception):
        await agent.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("command_present", [True, False])
async def test_idle_stream_raises_when_no_liveness_signal_remains(command_present: bool) -> None:
    run, handle = _run()
    base = datetime.now(UTC)
    first_event = _event(run, "status_changed", created_at=base)
    session = _DispatchSession(
        run=run,
        event_batches=((first_event,),),
        command_row=(
            SimpleNamespace(
                status="claimed",
                claim_expires_at=base - timedelta(seconds=1),
            )
            if command_present
            else None
        ),
        lease_active=False,
    )
    stream = DatabaseRuntimeEventStream(
        session_factory=lambda: session,  # type: ignore[arg-type]
        poll_interval_seconds=0.001,
        idle_timeout_seconds=0.05,
    )
    agent = stream.stream_run(handle)
    assert (await anext(agent)).event_id == first_event.id

    with pytest.raises(RuntimeEventStreamError) as exc_info:
        await asyncio.wait_for(anext(agent), timeout=2)
    assert exc_info.value.code == "runtime_event_stream_idle_timeout"
