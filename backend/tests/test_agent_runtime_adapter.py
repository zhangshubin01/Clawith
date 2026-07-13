"""Transactional intake tests for the product-facing Runtime Adapter."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch
import uuid

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.models.agent_run import AgentRun
from app.models.agent_run_command import AgentRunCommand
from app.services.agent_runtime.adapter import (
    RuntimeAdapterError,
    TransactionalAgentRuntimeAdapter,
)
from app.services.agent_runtime.contracts import (
    CancelRunCommand,
    ResumeRunCommand,
    StartRunCommand,
)
from app.services.agent_runtime.persistence import (
    EnqueuedCommand,
    RegisteredRun,
    RunRegistration,
)


class _Result:
    def __init__(self, value: object | None) -> None:
        self._value = value

    def scalar_one_or_none(self) -> object | None:
        return self._value


def _settings(*, enabled: bool, source_types: str = "") -> Settings:
    return Settings(
        _env_file=None,
        AGENT_RUNTIME_V2_ENABLED=enabled,
        AGENT_RUNTIME_V2_AGENT_IDS="",
        AGENT_RUNTIME_V2_SOURCE_TYPES=source_types,
        AGENT_RUNTIME_GRAPH_NAME="runtime_graph",
        AGENT_RUNTIME_GRAPH_VERSION="v2",
    )


def _session(result: object | None = None) -> AsyncMock:
    db = AsyncMock(spec=AsyncSession)
    db.execute.return_value = _Result(result)
    return db


def _run(
    *,
    tenant_id: uuid.UUID | None = None,
    run_id: uuid.UUID | None = None,
    runtime_type: str = "langgraph",
    projected_status: str | None = None,
    graph_name: str = "runtime_graph",
    graph_version: str = "v2",
    source_execution_id: str | None = None,
) -> AgentRun:
    resolved_tenant_id = tenant_id or uuid.uuid4()
    resolved_run_id = run_id or uuid.uuid4()
    now = datetime(2026, 7, 13, 12, 0, tzinfo=UTC)
    return AgentRun(
        id=resolved_run_id,
        tenant_id=resolved_tenant_id,
        agent_id=uuid.uuid4(),
        source_type="chat",
        source_execution_id=source_execution_id,
        goal="Answer the user",
        run_kind="foreground",
        model_id=uuid.uuid4(),
        runtime_type=runtime_type,
        runtime_thread_id=str(resolved_run_id),
        graph_name=graph_name,
        graph_version=graph_version,
        lane_held=False,
        projected_execution_status=projected_status,
        delivery_status="pending",
        created_at=now,
        updated_at=now,
    )


def _command(run: AgentRun, *, command_type: str, command_id: uuid.UUID | None = None) -> AgentRunCommand:
    return AgentRunCommand(
        id=command_id or uuid.uuid4(),
        tenant_id=run.tenant_id,
        run_id=run.id,
        command_type=command_type,
        payload={},
        idempotency_key=f"{command_type}:1",
        status="pending",
        attempt_count=0,
    )


def _start(tenant_id: uuid.UUID, *, source_execution_id: str | None = None) -> StartRunCommand:
    return StartRunCommand(
        tenant_id=tenant_id,
        agent_id=uuid.uuid4(),
        source_type="chat",
        source_execution_id=source_execution_id,
        goal="Answer the user",
        run_kind="foreground",
        model_id=uuid.uuid4(),
        idempotency_key="start:message:1",
        payload={"message_id": "message-1"},
        delivery_status="pending",
    )


def _compiled_query(db: AsyncMock) -> str:
    statement = db.execute.await_args.args[0]
    return str(
        statement.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )


@pytest.mark.asyncio
async def test_start_persists_registry_and_command_without_committing() -> None:
    tenant_id = uuid.uuid4()
    command = _start(tenant_id)
    run = _run(tenant_id=tenant_id)
    start_command = _command(run, command_type="start")
    db = _session()

    with patch(
        "app.services.agent_runtime.adapter.register_run_with_start",
        new=AsyncMock(return_value=RegisteredRun(run, start_command, True)),
    ) as persist:
        handle = await TransactionalAgentRuntimeAdapter(
            db,
            settings=_settings(enabled=True),
        ).start_run(command)

    assert handle.run_id == run.id
    assert handle.thread_id == str(run.id)
    assert handle.command_id == start_command.id
    assert handle.runtime_type == "langgraph"
    assert handle.created is True
    registration = persist.await_args.args[1]
    assert isinstance(registration, RunRegistration)
    assert registration.tenant_id == tenant_id
    assert registration.runtime_type == "langgraph"
    assert (registration.graph_name, registration.graph_version) == ("runtime_graph", "v2")
    assert persist.await_args.kwargs["start_payload"] == {"message_id": "message-1"}
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_new_start_fails_closed_when_v2_gate_is_disabled() -> None:
    db = _session()
    with patch(
        "app.services.agent_runtime.adapter.register_run_with_start",
        new=AsyncMock(),
    ) as persist:
        with pytest.raises(RuntimeAdapterError) as exc_info:
            await TransactionalAgentRuntimeAdapter(
                db,
                settings=_settings(enabled=False),
            ).start_run(_start(uuid.uuid4()))

    assert exc_info.value.code == "runtime_v2_disabled"
    persist.assert_not_awaited()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_start_retry_preserves_existing_runtime_and_graph_identity() -> None:
    tenant_id = uuid.uuid4()
    source_execution_id = "chat:message:1"
    run = _run(
        tenant_id=tenant_id,
        graph_name="runtime_graph",
        graph_version="v1",
        source_execution_id=source_execution_id,
    )
    start_command = _command(run, command_type="start")
    db = _session(run)

    with patch(
        "app.services.agent_runtime.adapter.register_run_with_start",
        new=AsyncMock(return_value=RegisteredRun(run, start_command, False)),
    ) as persist:
        handle = await TransactionalAgentRuntimeAdapter(
            db,
            settings=_settings(enabled=False),
        ).start_run(_start(tenant_id, source_execution_id=source_execution_id))

    registration = persist.await_args.args[1]
    assert registration.runtime_type == "langgraph"
    assert (registration.graph_name, registration.graph_version) == ("runtime_graph", "v1")
    assert handle.created is False
    sql = _compiled_query(db)
    assert "agent_runs.tenant_id" in sql
    assert "agent_runs.source_type" in sql
    assert "agent_runs.source_execution_id" in sql
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_resume_uses_runtime_type_not_a_conflicting_projection() -> None:
    tenant_id = uuid.uuid4()
    run = _run(tenant_id=tenant_id, projected_status="cancelled")
    resume_command = _command(run, command_type="resume")
    db = _session(run)
    command = ResumeRunCommand(
        tenant_id=tenant_id,
        run_id=run.id,
        idempotency_key="resume:message:2",
        payload={"value": "continue"},
    )

    with patch(
        "app.services.agent_runtime.adapter.enqueue_resume",
        new=AsyncMock(return_value=EnqueuedCommand(resume_command, True)),
    ) as enqueue:
        handle = await TransactionalAgentRuntimeAdapter(
            db,
            settings=_settings(enabled=False),
        ).resume_run(command)

    assert handle.runtime_type == "langgraph"
    assert handle.command_id == resume_command.id
    assert enqueue.await_args.kwargs["tenant_id"] == tenant_id
    assert enqueue.await_args.kwargs["run_id"] == run.id
    assert "projected" not in repr(enqueue.await_args)
    sql = _compiled_query(db)
    assert "agent_runs.tenant_id" in sql
    assert "agent_runs.id" in sql
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_existing_legacy_run_is_not_switched_by_enabled_v2_gate() -> None:
    tenant_id = uuid.uuid4()
    run = _run(
        tenant_id=tenant_id,
        runtime_type="legacy",
        projected_status="running",
    )
    db = _session(run)
    command = ResumeRunCommand(
        tenant_id=tenant_id,
        run_id=run.id,
        idempotency_key="resume:1",
        payload={"value": "continue"},
    )

    with patch(
        "app.services.agent_runtime.adapter.enqueue_resume",
        new=AsyncMock(),
    ) as enqueue:
        with pytest.raises(RuntimeAdapterError) as exc_info:
            await TransactionalAgentRuntimeAdapter(
                db,
                settings=_settings(enabled=True),
            ).resume_run(command)

    assert exc_info.value.code == "runtime_v2_disabled"
    assert run.runtime_type == "legacy"
    enqueue.assert_not_awaited()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancel_persists_command_and_returns_its_stable_identity() -> None:
    tenant_id = uuid.uuid4()
    run = _run(tenant_id=tenant_id)
    cancel_command = _command(run, command_type="cancel")
    db = _session(run)
    command = CancelRunCommand(
        tenant_id=tenant_id,
        run_id=run.id,
        idempotency_key="cancel:user:1",
        reason="user_abort",
    )

    with patch(
        "app.services.agent_runtime.adapter.enqueue_cancel",
        new=AsyncMock(return_value=EnqueuedCommand(cancel_command, True)),
    ) as enqueue:
        handle = await TransactionalAgentRuntimeAdapter(
            db,
            settings=_settings(enabled=True),
        ).cancel_run(command)

    assert (handle.run_id, handle.thread_id, handle.command_id) == (
        run.id,
        str(run.id),
        cancel_command.id,
    )
    assert enqueue.await_args.kwargs["reason"] == "user_abort"
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_run_state_maps_projection_for_queries_only() -> None:
    tenant_id = uuid.uuid4()
    run = _run(tenant_id=tenant_id, projected_status="waiting_user")
    run.projected_waiting_type = "user_input"
    run.projected_result_summary = "partial"
    run.projected_error_code = "temporary"
    run.projected_last_error = "retry later"
    run.projected_checkpoint_id = "checkpoint-7"
    run.projection_updated_at = datetime(2026, 7, 13, 12, 1, tzinfo=UTC)
    db = _session(run)

    view = await TransactionalAgentRuntimeAdapter(
        db,
        settings=_settings(enabled=False),
    ).get_run_state(tenant_id, run.id)

    assert view.tenant_id == tenant_id
    assert view.run_id == run.id
    assert view.execution_status == "waiting_user"
    assert view.waiting_type == "user_input"
    assert view.result_summary == "partial"
    assert view.error_code == "temporary"
    assert view.last_error == "retry later"
    assert view.projection_checkpoint_id == "checkpoint-7"
    assert view.projection_updated_at == run.projection_updated_at
    sql = _compiled_query(db)
    assert "agent_runs.tenant_id" in sql
    assert "agent_runs.id" in sql
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_handle_rejects_a_thread_id_that_differs_from_run_id() -> None:
    tenant_id = uuid.uuid4()
    run = _run(tenant_id=tenant_id)
    run.runtime_thread_id = "different-thread"
    resume_command = _command(run, command_type="resume")
    db = _session(run)
    command = ResumeRunCommand(
        tenant_id=tenant_id,
        run_id=run.id,
        idempotency_key="resume:1",
        payload={"value": "continue"},
    )

    with patch(
        "app.services.agent_runtime.adapter.enqueue_resume",
        new=AsyncMock(return_value=EnqueuedCommand(resume_command, True)),
    ) as enqueue:
        with pytest.raises(RuntimeAdapterError) as exc_info:
            await TransactionalAgentRuntimeAdapter(
                db,
                settings=_settings(enabled=True),
            ).resume_run(command)

    assert exc_info.value.code == "runtime_identity_mismatch"
    enqueue.assert_not_awaited()
    db.commit.assert_not_awaited()
