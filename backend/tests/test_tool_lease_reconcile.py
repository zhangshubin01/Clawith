"""Durable recovery tests for Tool executions orphaned by dead executor leases."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
import uuid

import pytest
from sqlalchemy.dialects import postgresql

from app.models.agent_tool_execution import AgentToolExecution
from app.services.agent_runtime import tool_lease_reconcile


_NOW = datetime(2026, 8, 15, 6, 50, tzinfo=UTC)


class _Scalars:
    def __init__(self, values):
        self._values = values

    def all(self):
        return list(self._values)


class _Result:
    def __init__(self, values):
        self._values = values

    def scalars(self):
        return _Scalars(self._values)

    def first(self):
        return self._values[0] if self._values else None

    def scalar_one_or_none(self):
        return self._values[0] if len(self._values) == 1 else None


class _Session:
    def __init__(
        self,
        values,
        *,
        owner_terminal: bool = False,
        holder_id=None,
    ):
        self.values = values
        self.owner_terminal = owner_terminal
        self.holder_id = holder_id
        self.flush_count = 0
        self.statements = []

    @asynccontextmanager
    async def begin(self):
        yield

    async def execute(self, statement):
        self.statements.append(statement)
        text = str(statement)
        if "scheduling_lane_key" in text:
            # Owner-run liveness probe.
            return _Result([(not self.owner_terminal, "lane-key")])
        if "lane_held" in text:
            # Thread-holder probe.
            return _Result([self.holder_id] if self.holder_id is not None else [])
        return _Result(self.values)

    async def flush(self):
        self.flush_count += 1


def _factory(session):
    @asynccontextmanager
    async def factory():
        yield session

    return factory


def _execution(
    *,
    status="started",
    effect="write",
    retry_policy="conditional",
    lease_expires_at=None,
    tool_name="execute_code",
    tool_call_id="call-orphaned",
):
    return AgentToolExecution(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        tool_call_id=tool_call_id,
        tool_name=tool_name,
        assistant_message_id="assistant-1",
        arguments_hash="hash",
        effect=effect,
        retry_policy=retry_policy,
        status=status,
        result_metadata={},
        lease_expires_at=lease_expires_at,
        started_at=_NOW - timedelta(seconds=400),
    )


def _scheduler(session):
    return tool_lease_reconcile.ToolLeaseReconcileScheduler(
        session_factory=_factory(session),
        lease_owner="reconcile-owner",
        clock=lambda: _NOW,
    )


@pytest.mark.asyncio
async def test_expired_non_reconcilable_write_lease_is_settled_failed(monkeypatch) -> None:
    execution = _execution(lease_expires_at=_NOW - timedelta(seconds=1))
    session = _Session([execution])
    calls = {"takeover": [], "settle": [], "enqueue": []}

    async def takeover(db, **kwargs):
        assert db is session
        calls["takeover"].append(kwargs)
        return SimpleNamespace(acquired=True, active=False, terminal_outcome=None)

    async def settle(db, **kwargs):
        assert db is session
        calls["settle"].append(kwargs)
        return execution

    async def enqueue(db, **kwargs):
        assert db is session
        calls["enqueue"].append(kwargs)
        return object()

    monkeypatch.setattr(
        tool_lease_reconcile,
        "takeover_tool_execution_for_reconciliation",
        takeover,
    )
    monkeypatch.setattr(
        tool_lease_reconcile,
        "mark_tool_execution_failed",
        settle,
    )
    monkeypatch.setattr(tool_lease_reconcile, "enqueue_resume", enqueue)

    result = await _scheduler(session).run_once()

    assert result.status == "settled"
    assert result.execution_id == execution.id
    assert result.run_id == execution.run_id
    assert calls["takeover"] == [
        {
            "tenant_id": execution.tenant_id,
            "execution_id": execution.id,
            "lease_owner": "reconcile-owner",
            "lease_ttl_seconds": 300,
        }
    ]
    assert calls["settle"] == [
        {
            "tenant_id": execution.tenant_id,
            "execution_id": execution.id,
            "lease_owner": "reconcile-owner",
            "result_summary": tool_lease_reconcile._ORPHAN_FAILED_SUMMARY,
            "error_code": "tool_execution_lease_expired",
            "retryable": False,
        }
    ]
    expected_correlation = str(
        uuid.uuid5(execution.run_id, "tool-reconcile:" + execution.tool_call_id)
    )
    assert calls["enqueue"] == [
        {
            "tenant_id": execution.tenant_id,
            "run_id": execution.run_id,
            "payload": {
                "resume_type": "timer",
                "correlation_id": expected_correlation,
                "payload": {
                    "reason": "tool_execution_lease_expired",
                    "tool_call_id": execution.tool_call_id,
                },
            },
            "idempotency_key": "tool-lease-reconcile:" + str(execution.id),
        }
    ]


@pytest.mark.asyncio
async def test_expired_reconcilable_write_lease_is_settled_unknown(monkeypatch) -> None:
    execution = _execution(
        tool_name="write_file",
        effect="write",
        retry_policy="conditional",
        lease_expires_at=_NOW - timedelta(seconds=1),
    )
    session = _Session([execution])
    settle_calls = []

    async def takeover(db, **kwargs):
        return SimpleNamespace(acquired=True, active=False, terminal_outcome=None)

    async def settle(db, **kwargs):
        settle_calls.append(kwargs)
        return execution

    async def enqueue(db, **kwargs):
        return object()

    monkeypatch.setattr(
        tool_lease_reconcile,
        "takeover_tool_execution_for_reconciliation",
        takeover,
    )
    monkeypatch.setattr(
        tool_lease_reconcile,
        "mark_tool_execution_unknown",
        settle,
    )
    monkeypatch.setattr(tool_lease_reconcile, "enqueue_resume", enqueue)

    result = await _scheduler(session).run_once()

    assert result.status == "settled"
    assert settle_calls == [
        {
            "tenant_id": execution.tenant_id,
            "execution_id": execution.id,
            "lease_owner": "reconcile-owner",
            "result_summary": tool_lease_reconcile._ORPHAN_UNKNOWN_SUMMARY,
            "error_code": "tool_execution_lease_expired",
        }
    ]


@pytest.mark.asyncio
async def test_expired_safe_read_lease_only_enqueues_resume(monkeypatch) -> None:
    execution = _execution(
        effect="read",
        retry_policy="safe",
        lease_expires_at=_NOW - timedelta(seconds=1),
    )
    session = _Session([execution])
    calls = []

    async def enqueue(db, **kwargs):
        assert db is session
        calls.append(kwargs)
        return object()

    async def forbidden_takeover(*args, **kwargs):
        raise AssertionError("safe reads must not be settled by the scheduler")

    async def forbidden_settle(*args, **kwargs):
        raise AssertionError("safe reads must not be settled by the scheduler")

    monkeypatch.setattr(
        tool_lease_reconcile,
        "takeover_tool_execution_for_reconciliation",
        forbidden_takeover,
    )
    monkeypatch.setattr(
        tool_lease_reconcile,
        "mark_tool_execution_unknown",
        forbidden_settle,
    )
    monkeypatch.setattr(tool_lease_reconcile, "enqueue_resume", enqueue)

    result = await _scheduler(session).run_once()

    assert result.status == "scheduled"
    assert len(calls) == 1
    assert calls[0]["payload"]["correlation_id"] == str(
        uuid.uuid5(execution.run_id, "tool-reconcile:" + execution.tool_call_id)
    )


@pytest.mark.asyncio
async def test_concurrent_reconciler_ownership_skips_settlement(monkeypatch) -> None:
    execution = _execution(lease_expires_at=_NOW - timedelta(seconds=1))
    session = _Session([execution])

    async def takeover(db, **kwargs):
        return SimpleNamespace(acquired=False, active=True, terminal_outcome=None)

    async def forbidden_enqueue(*args, **kwargs):
        raise AssertionError("lost takeover must not enqueue a resume")

    monkeypatch.setattr(
        tool_lease_reconcile,
        "takeover_tool_execution_for_reconciliation",
        takeover,
    )
    monkeypatch.setattr(tool_lease_reconcile, "enqueue_resume", forbidden_enqueue)

    result = await _scheduler(session).run_once()

    assert result.status == "idle"


@pytest.mark.asyncio
async def test_existing_resume_command_yields_idle_without_rescheduling(
    monkeypatch,
) -> None:
    execution = _execution(lease_expires_at=_NOW - timedelta(seconds=1))
    session = _Session([execution])

    async def takeover(db, **kwargs):
        return SimpleNamespace(acquired=True, active=False, terminal_outcome=None)

    async def settle(db, **kwargs):
        return execution

    async def enqueue(db, **kwargs):
        return SimpleNamespace(created=False)

    monkeypatch.setattr(
        tool_lease_reconcile,
        "takeover_tool_execution_for_reconciliation",
        takeover,
    )
    monkeypatch.setattr(
        tool_lease_reconcile,
        "mark_tool_execution_failed",
        settle,
    )
    monkeypatch.setattr(tool_lease_reconcile, "enqueue_resume", enqueue)

    result = await _scheduler(session).run_once()

    assert result.status == "idle"


@pytest.mark.asyncio
async def test_scan_query_filters_async_pending_and_group_workspace(monkeypatch) -> None:
    execution = _execution(lease_expires_at=_NOW - timedelta(seconds=1))
    session = _Session([execution])

    async def takeover(db, **kwargs):
        return SimpleNamespace(acquired=True, active=False, terminal_outcome=None)

    async def settle(db, **kwargs):
        return execution

    async def enqueue(db, **kwargs):
        return object()

    monkeypatch.setattr(
        tool_lease_reconcile,
        "takeover_tool_execution_for_reconciliation",
        takeover,
    )
    monkeypatch.setattr(
        tool_lease_reconcile,
        "mark_tool_execution_failed",
        settle,
    )
    monkeypatch.setattr(tool_lease_reconcile, "enqueue_resume", enqueue)

    await _scheduler(session).run_once()

    compiled = session.statements[0].compile(dialect=postgresql.dialect())
    params = compiled.params
    assert "runtime_async_pending" in params.values()
    assert "lease_expires_at" in str(compiled).lower()
    assert "not in" in str(compiled).lower()


@pytest.mark.asyncio
async def test_missing_lease_is_treated_as_expired(monkeypatch) -> None:
    execution = _execution(lease_expires_at=None)
    session = _Session([execution])
    calls = []

    async def takeover(db, **kwargs):
        return SimpleNamespace(acquired=True, active=False, terminal_outcome=None)

    async def settle(db, **kwargs):
        return execution

    async def enqueue(db, **kwargs):
        calls.append(kwargs)
        return object()

    monkeypatch.setattr(
        tool_lease_reconcile,
        "takeover_tool_execution_for_reconciliation",
        takeover,
    )
    monkeypatch.setattr(
        tool_lease_reconcile,
        "mark_tool_execution_failed",
        settle,
    )
    monkeypatch.setattr(tool_lease_reconcile, "enqueue_resume", enqueue)

    result = await _scheduler(session).run_once()

    assert result.status == "settled"
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_terminal_owner_safe_read_lease_is_settled_failed(monkeypatch) -> None:
    """A safe read from a dead Run can never self-heal; settle it failed."""
    execution = _execution(
        effect="read",
        retry_policy="safe",
        lease_expires_at=_NOW - timedelta(seconds=1),
    )
    session = _Session([execution], owner_terminal=True)
    calls = {"takeover": [], "settle": [], "enqueue": []}

    async def takeover(db, **kwargs):
        assert db is session
        calls["takeover"].append(kwargs)
        return SimpleNamespace(acquired=True, active=False, terminal_outcome=None)

    async def settle(db, **kwargs):
        assert db is session
        calls["settle"].append(kwargs)
        return execution

    async def enqueue(db, **kwargs):
        assert db is session
        calls["enqueue"].append(kwargs)
        return object()

    async def wake(db, **kwargs):
        del db, kwargs
        calls["enqueue"].append({"wake": True})
        return None

    monkeypatch.setattr(
        tool_lease_reconcile,
        "takeover_tool_execution_for_reconciliation",
        takeover,
    )
    monkeypatch.setattr(
        tool_lease_reconcile,
        "mark_tool_execution_failed",
        settle,
    )
    monkeypatch.setattr(tool_lease_reconcile, "enqueue_resume", enqueue)
    monkeypatch.setattr(
        tool_lease_reconcile,
        "enqueue_thread_holder_reconcile_wake",
        wake,
    )

    result = await _scheduler(session).run_once()

    assert result.status == "settled"
    assert len(calls["settle"]) == 1
    assert calls["settle"][0]["result_summary"] == (
        tool_lease_reconcile._ORPHAN_READ_FAILED_SUMMARY
    )
    assert calls["settle"][0]["error_code"] == "tool_execution_lease_expired"


@pytest.mark.asyncio
async def test_terminal_owner_settlement_wakes_thread_holder(monkeypatch) -> None:
    """Settling a dead Run's receipt must wake a waiting Run on its Thread."""
    execution = _execution(lease_expires_at=_NOW - timedelta(seconds=1))
    holder_id = uuid.uuid4()
    session = _Session([execution], owner_terminal=True, holder_id=holder_id)
    wakes = []

    async def takeover(db, **kwargs):
        del kwargs
        assert db is session
        return SimpleNamespace(acquired=True, active=False, terminal_outcome=None)

    async def settle(db, **kwargs):
        del kwargs
        assert db is session
        return execution

    async def enqueue(db, **kwargs):
        del db, kwargs
        return object()

    async def wake(db, **kwargs):
        assert db is session
        wakes.append(kwargs)
        return holder_id

    monkeypatch.setattr(
        tool_lease_reconcile,
        "takeover_tool_execution_for_reconciliation",
        takeover,
    )
    monkeypatch.setattr(
        tool_lease_reconcile,
        "mark_tool_execution_failed",
        settle,
    )
    monkeypatch.setattr(tool_lease_reconcile, "enqueue_resume", enqueue)
    monkeypatch.setattr(
        tool_lease_reconcile,
        "enqueue_thread_holder_reconcile_wake",
        wake,
    )

    result = await _scheduler(session).run_once()

    assert result.status == "settled"
    assert len(wakes) == 1
    assert wakes[0]["owner_run_id"] == execution.run_id
    assert wakes[0]["tool_call_id"] == execution.tool_call_id
    assert wakes[0]["reason"] == "tool_execution_lease_expired"
