"""Atomic TriggerExecution queue and legacy-claim separation."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch
import uuid

import pytest
from sqlalchemy.dialects import postgresql

from app.models.agent import Agent
from app.models.trigger import AgentTrigger
from app.models.trigger_execution import TriggerExecution
from app.services.agent_runtime.contracts import RunHandle
from app.services.trigger_runtime.executions import claim_pending_trigger_executions
from app.services.trigger_runtime.intake import TriggerRuntimeIntakeError
from app.services.trigger_runtime.queue import enqueue_trigger_execution


class _ScalarResult:
    def __init__(self, value: object) -> None:
        self.value = value

    def scalar_one_or_none(self) -> object:
        return self.value


class _RowsResult:
    def all(self) -> list:
        return []


class _Nested:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class _QueueSession:
    def __init__(self, stored_trigger: AgentTrigger) -> None:
        self.stored_trigger = stored_trigger
        self.added: list[object] = []
        self.nested = 0
        self.flushes = 0
        self.commits = 0

    def begin_nested(self) -> _Nested:
        self.nested += 1
        return _Nested()

    def add(self, value: object) -> None:
        self.added.append(value)

    async def flush(self) -> None:
        self.flushes += 1

    async def execute(self, _statement) -> _ScalarResult:
        return _ScalarResult(self.stored_trigger)

    async def commit(self) -> None:
        self.commits += 1


class _ClaimSession:
    def __init__(self) -> None:
        self.statements: list[object] = []
        self.commits = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    async def execute(self, statement) -> _RowsResult:
        self.statements.append(statement)
        return _RowsResult()

    async def commit(self) -> None:
        self.commits += 1


class _SessionFactory:
    def __init__(self, session: _ClaimSession) -> None:
        self.session = session

    def __call__(self) -> _ClaimSession:
        return self.session


def _records() -> tuple[AgentTrigger, Agent]:
    agent_id = uuid.uuid4()
    trigger = AgentTrigger(
        id=uuid.uuid4(),
        agent_id=agent_id,
        name="poll-status",
        type="poll",
        config={},
        reason="Watch status",
        is_enabled=True,
        fire_count=0,
    )
    agent = Agent(
        id=agent_id,
        tenant_id=uuid.uuid4(),
        creator_id=uuid.uuid4(),
        name="Watcher",
        role_description="Watch status",
        primary_model_id=uuid.uuid4(),
        status="idle",
        is_expired=False,
    )
    return trigger, agent


@pytest.mark.asyncio
async def test_execution_and_runtime_start_commit_as_one_queue_transaction() -> None:
    trigger, agent = _records()
    db = _QueueSession(trigger)
    handle = RunHandle(
        tenant_id=agent.tenant_id,
        run_id=uuid.uuid4(),
        thread_id=str(uuid.uuid4()),
        command_id=uuid.uuid4(),
        runtime_type="langgraph",
        created=True,
    )

    async def accept_runtime(*_args, **kwargs):
        execution = kwargs["execution"]
        execution.status = "processing"
        return handle

    with (
        patch(
            "app.services.trigger_runtime.queue.load_trigger_agent",
            new=AsyncMock(return_value=agent),
        ),
        patch(
            "app.services.trigger_runtime.queue.enqueue_trigger_runtime",
            side_effect=accept_runtime,
        ),
    ):
        execution, created = await enqueue_trigger_execution(
            db,  # type: ignore[arg-type]
            trigger=trigger,
            source="poll",
            idempotency_key="poll:2026-07-13T16:00",
        )

    assert created is True
    assert isinstance(execution, TriggerExecution)
    assert execution.status == "processing"
    assert db.commits == 1
    assert db.nested == 2
    assert db.added == [execution]
    assert trigger.fire_count == 1
    assert trigger.last_fired_at is not None


@pytest.mark.asyncio
async def test_runtime_intake_rejection_settles_occurrence_without_legacy_fallback() -> None:
    trigger, agent = _records()
    db = _QueueSession(trigger)
    error = TriggerRuntimeIntakeError(
        "agent_model_missing",
        "Runtime Trigger Agent has no primary model",
    )

    with (
        patch(
            "app.services.trigger_runtime.queue.load_trigger_agent",
            new=AsyncMock(return_value=agent),
        ),
        patch(
            "app.services.trigger_runtime.queue.enqueue_trigger_runtime",
            new=AsyncMock(side_effect=error),
        ),
    ):
        execution, created = await enqueue_trigger_execution(
            db,  # type: ignore[arg-type]
            trigger=trigger,
            source="poll",
            idempotency_key="poll:2026-07-13T16:00",
        )

    assert created is True
    assert execution is not None
    assert execution.status == "failed"
    assert execution.last_error == "agent_model_missing: Runtime Trigger Agent has no primary model"
    assert execution.finished_at is not None
    assert trigger.fire_count == 0
    assert db.commits == 1


@pytest.mark.asyncio
async def test_legacy_claim_query_excludes_executions_owned_by_runtime_runs() -> None:
    db = _ClaimSession()
    with patch(
        "app.services.trigger_runtime.executions.async_session",
        _SessionFactory(db),
    ):
        claimed = await claim_pending_trigger_executions()

    assert claimed == []
    assert db.commits == 1
    sql = str(
        db.statements[0].compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    assert "NOT (EXISTS" in sql
    assert "FROM agent_runs" in sql
    assert "agent_runs.source_type = 'trigger'" in sql
    assert "agent_runs.source_execution_id = CAST(trigger_executions.id AS VARCHAR)" in sql
