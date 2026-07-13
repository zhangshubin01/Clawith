"""Concrete LangGraph command driver tests."""

from __future__ import annotations

from dataclasses import replace
from typing import cast
import uuid

from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy.ext.asyncio import AsyncConnection
import pytest

from app.config import Settings
from app.services.agent_runtime.command_worker import (
    CommandExecutionRejected,
    RetryableCommandError,
    RuntimeCommandRecord,
    RuntimeCommandType,
    RuntimeRunRecord,
)
from app.services.agent_runtime.graph import build_agent_runtime_graph
from app.services.agent_runtime.langgraph_driver import (
    LangGraphRuntimeDriver,
    RuntimeGraphRegistry,
    StaticRuntimeInputSnapshotFactory,
)
from app.services.agent_runtime.state import (
    JsonValue,
    RunInputSnapshots,
    RunRegistrySnapshot,
    RuntimeContext,
    RuntimeGraphState,
    RuntimeNodeName,
    RuntimeNodeExecutor,
    RuntimeStateUpdate,
)


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        AGENT_RUNTIME_GRAPH_NAME="driver_graph",
        AGENT_RUNTIME_GRAPH_VERSION="v1",
    )


class CompletingExecutor:
    async def execute(
        self,
        node: RuntimeNodeName,
        state: RuntimeGraphState,
        context: RuntimeContext,
        *,
        resume_value: JsonValue | None = None,
    ) -> RuntimeStateUpdate:
        del context, resume_value
        if node == "model":
            return {"lifecycle": {"status": "verifying", "next_route": "verify"}}
        if node == "verify":
            return {
                "lifecycle": {
                    "status": "completed",
                    "next_route": "terminal",
                    "final_answer": "done",
                }
            }
        return {"lifecycle": dict(state["lifecycle"])}


class WaitingExecutor:
    async def execute(
        self,
        node: RuntimeNodeName,
        state: RuntimeGraphState,
        context: RuntimeContext,
        *,
        resume_value: JsonValue | None = None,
    ) -> RuntimeStateUpdate:
        del context
        if node == "model":
            return {
                "lifecycle": {
                    "status": "waiting_user",
                    "next_route": "wait",
                    "waiting_request": {
                        "waiting_type": "user",
                        "reason": "confirm",
                        "correlation_id": "correlation-1",
                    },
                }
            }
        if node == "wait":
            return {
                "lifecycle": {
                    "status": "completed",
                    "next_route": "terminal",
                    "waiting_request": None,
                    "final_answer": str(resume_value),
                }
            }
        return {"lifecycle": dict(state["lifecycle"])}


def _snapshots() -> RunInputSnapshots:
    return RunInputSnapshots(
        session_context={"version": 0, "summary": ""},
        session_context_version=0,
        recent_session_messages=(),
        related_run_summaries=(),
        initial_input={"message": "hello"},
    )


def _run(run_id: uuid.UUID) -> RuntimeRunRecord:
    tenant_id = uuid.uuid4()
    return RuntimeRunRecord(
        tenant_id=tenant_id,
        run_id=run_id,
        thread_id=str(run_id),
        runtime_type="langgraph",
        registry=RunRegistrySnapshot(
            tenant_id=str(tenant_id),
            run_id=str(run_id),
            goal="Answer the user",
            run_kind="foreground",
            source_type="chat",
            model_id=str(uuid.uuid4()),
            graph_name="driver_graph",
            graph_version="v1",
            agent_id=str(uuid.uuid4()),
        ),
    )


def _command(
    run: RuntimeRunRecord,
    command_type: str,
    *,
    payload: dict[str, JsonValue] | None = None,
) -> RuntimeCommandRecord:
    return RuntimeCommandRecord(
        id=uuid.uuid4(),
        tenant_id=run.tenant_id,
        run_id=run.run_id,
        command_type=cast(RuntimeCommandType, command_type),
        payload=payload or {},
        actor_user_id=uuid.uuid4(),
        actor_agent_id=None,
    )


def _driver(executor: object) -> LangGraphRuntimeDriver:
    graph = build_agent_runtime_graph(
        checkpointer=InMemorySaver(),
        settings=_settings(),
    )
    return LangGraphRuntimeDriver(
        graph_registry=RuntimeGraphRegistry([graph]),
        snapshot_factory=StaticRuntimeInputSnapshotFactory(_snapshots()),
        node_executor=cast(RuntimeNodeExecutor, executor),
    )


def _connection() -> AsyncConnection:
    return cast(AsyncConnection, object())


@pytest.mark.asyncio
async def test_start_creates_a_checkpoint_containing_the_command_id() -> None:
    run = _run(uuid.uuid4())
    command = _command(run, "start", payload={"message": "hello"})
    driver = _driver(CompletingExecutor())

    assert await driver.read_latest(connection=_connection(), run=run) is None
    await driver.execute(
        connection=_connection(),
        run=run,
        command=command,
        checkpoint=None,
    )
    observed = await driver.read_latest(connection=_connection(), run=run)

    assert observed is not None
    assert observed.state["registry"] == run.registry
    assert observed.state["lifecycle"]["status"] == "completed"
    assert observed.state["lifecycle"]["last_applied_command_ids"] == [str(command.id)]


@pytest.mark.asyncio
async def test_resume_validates_wait_contract_and_records_both_commands() -> None:
    run = _run(uuid.uuid4())
    start = _command(run, "start")
    driver = _driver(WaitingExecutor())
    await driver.execute(connection=_connection(), run=run, command=start, checkpoint=None)
    waiting = await driver.read_latest(connection=_connection(), run=run)
    assert waiting is not None
    assert waiting.state["lifecycle"]["status"] == "waiting_user"

    resume = _command(
        run,
        "resume",
        payload={
            "resume_type": "user_input",
            "correlation_id": "correlation-1",
            "payload": {"confirmed": True},
        },
    )
    await driver.execute(
        connection=_connection(),
        run=run,
        command=resume,
        checkpoint=waiting,
    )
    completed = await driver.read_latest(connection=_connection(), run=run)

    assert completed is not None
    assert completed.state["lifecycle"]["status"] == "completed"
    assert completed.state["lifecycle"]["last_applied_command_ids"] == [
        str(start.id),
        str(resume.id),
    ]


@pytest.mark.asyncio
async def test_resume_rejects_a_mismatched_correlation_without_advancing() -> None:
    run = _run(uuid.uuid4())
    start = _command(run, "start")
    driver = _driver(WaitingExecutor())
    await driver.execute(connection=_connection(), run=run, command=start, checkpoint=None)
    waiting = await driver.read_latest(connection=_connection(), run=run)
    assert waiting is not None
    resume = _command(
        run,
        "resume",
        payload={
            "resume_type": "user_input",
            "correlation_id": "wrong-correlation",
            "payload": {},
        },
    )

    with pytest.raises(CommandExecutionRejected) as exc_info:
        await driver.execute(
            connection=_connection(),
            run=run,
            command=resume,
            checkpoint=waiting,
        )

    assert exc_info.value.code == "resume_correlation_mismatch"
    unchanged = await driver.read_latest(connection=_connection(), run=run)
    assert unchanged is not None
    assert str(resume.id) not in unchanged.state["lifecycle"].get("last_applied_command_ids", [])


@pytest.mark.asyncio
async def test_cancel_writes_terminal_lifecycle_through_the_graph() -> None:
    run = _run(uuid.uuid4())
    start = _command(run, "start")
    driver = _driver(WaitingExecutor())
    await driver.execute(connection=_connection(), run=run, command=start, checkpoint=None)
    waiting = await driver.read_latest(connection=_connection(), run=run)
    assert waiting is not None
    cancel = _command(run, "cancel", payload={"reason": "user_abort"})

    await driver.execute(
        connection=_connection(),
        run=run,
        command=cancel,
        checkpoint=waiting,
    )
    cancelled = await driver.read_latest(connection=_connection(), run=run)

    assert cancelled is not None
    assert cancelled.state["lifecycle"]["status"] == "cancelled"
    assert cancelled.state["lifecycle"]["reason"] == "user_abort"
    assert cancelled.state["lifecycle"]["waiting_request"] is None
    assert cancelled.state["lifecycle"]["last_applied_command_ids"] == [
        str(start.id),
        str(cancel.id),
    ]


@pytest.mark.asyncio
async def test_driver_refuses_an_uninstalled_pinned_graph_version() -> None:
    run = _run(uuid.uuid4())
    run = RuntimeRunRecord(
        tenant_id=run.tenant_id,
        run_id=run.run_id,
        thread_id=run.thread_id,
        runtime_type=run.runtime_type,
        registry=replace(run.registry, graph_version="old-version"),
    )
    driver = _driver(CompletingExecutor())

    with pytest.raises(RetryableCommandError) as exc_info:
        await driver.read_latest(connection=_connection(), run=run)

    assert exc_info.value.code == "graph_version_unavailable"
