"""Task entrypoint cutover tests for the durable Runtime."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch
import uuid

import pytest

from app.config import Settings
from app.models.agent import Agent
from app.models.task import Task, TaskLog
from app.services.agent_runtime.contracts import RunHandle, StartRunCommand
from app.services.task_executor import TaskRuntimeIntakeError, enqueue_task_runtime


class _Session:
    def __init__(self) -> None:
        self.added: list[object] = []

    def add(self, value: object) -> None:
        self.added.append(value)


def _settings(*, enabled: bool) -> Settings:
    return Settings(
        _env_file=None,
        AGENT_RUNTIME_V2_ENABLED=enabled,
        AGENT_RUNTIME_V2_SOURCE_TYPES="task" if enabled else "",
    )


def _records(*, task_type: str = "todo") -> tuple[Task, Agent]:
    agent_id = uuid.uuid4()
    creator_id = uuid.uuid4()
    task = Task(
        id=uuid.uuid4(),
        agent_id=agent_id,
        title="Prepare the report",
        description="Use the current workspace evidence",
        type=task_type,
        status="pending",
        priority="medium",
        created_by=creator_id,
    )
    agent = Agent(
        id=agent_id,
        tenant_id=uuid.uuid4(),
        creator_id=creator_id,
        name="Analyst",
        role_description="Analyze evidence",
        primary_model_id=uuid.uuid4(),
        status="idle",
    )
    return task, agent


@pytest.mark.asyncio
async def test_todo_registration_updates_task_in_same_caller_session() -> None:
    task, agent = _records()
    session = _Session()
    handle = RunHandle(
        tenant_id=agent.tenant_id,
        run_id=uuid.uuid4(),
        thread_id=str(uuid.uuid4()),
        command_id=uuid.uuid4(),
        runtime_type="langgraph",
        created=True,
    )

    with patch(
        "app.services.task_executor.TransactionalAgentRuntimeAdapter.start_run",
        new=AsyncMock(return_value=handle),
    ) as start_run:
        result = await enqueue_task_runtime(
            session,  # type: ignore[arg-type]
            task=task,
            agent=agent,
            settings_override=_settings(enabled=True),
        )

    assert result == handle
    assert task.status == "doing"
    assert len(session.added) == 1
    assert isinstance(session.added[0], TaskLog)
    command = start_run.await_args.args[0]
    assert isinstance(command, StartRunCommand)
    assert command.source_type == "task"
    assert command.source_id == str(task.id)
    assert command.source_execution_id == f"task:{task.id}"
    assert command.model_id == agent.primary_model_id
    assert command.delivery_status == "not_required"
    assert command.payload["task_id"] == str(task.id)


@pytest.mark.asyncio
async def test_idempotent_task_retry_does_not_duplicate_queue_log() -> None:
    task, agent = _records()
    session = _Session()
    handle = RunHandle(
        tenant_id=agent.tenant_id,
        run_id=uuid.uuid4(),
        thread_id=str(uuid.uuid4()),
        command_id=uuid.uuid4(),
        runtime_type="langgraph",
        created=False,
    )

    with patch(
        "app.services.task_executor.TransactionalAgentRuntimeAdapter.start_run",
        new=AsyncMock(return_value=handle),
    ):
        await enqueue_task_runtime(
            session,  # type: ignore[arg-type]
            task=task,
            agent=agent,
            settings_override=_settings(enabled=True),
        )

    assert task.status == "doing"
    assert session.added == []


@pytest.mark.asyncio
async def test_supervision_and_disabled_rollout_stay_on_legacy_path() -> None:
    supervision, agent = _records(task_type="supervision")
    todo, _ = _records()
    todo.agent_id = agent.id
    session = _Session()

    with patch(
        "app.services.task_executor.TransactionalAgentRuntimeAdapter.start_run",
        new=AsyncMock(),
    ) as start_run:
        supervision_result = await enqueue_task_runtime(
            session,  # type: ignore[arg-type]
            task=supervision,
            agent=agent,
            settings_override=_settings(enabled=True),
        )
        disabled_result = await enqueue_task_runtime(
            session,  # type: ignore[arg-type]
            task=todo,
            agent=agent,
            settings_override=_settings(enabled=False),
        )

    assert supervision_result is None
    assert disabled_result is None
    start_run.assert_not_awaited()


@pytest.mark.asyncio
async def test_selected_task_requires_tenant_and_model() -> None:
    task, agent = _records()
    agent.tenant_id = None

    with pytest.raises(TaskRuntimeIntakeError, match="tenant") as raised:
        await enqueue_task_runtime(
            _Session(),  # type: ignore[arg-type]
            task=task,
            agent=agent,
            settings_override=_settings(enabled=True),
        )

    assert raised.value.code == "agent_tenant_missing"
