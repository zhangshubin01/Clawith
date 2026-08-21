"""Runtime worker composition and daemon lifecycle tests."""

from __future__ import annotations

from contextlib import asynccontextmanager
from collections import deque
from dataclasses import replace
import asyncio
import logging
import time
import uuid
from unittest.mock import AsyncMock, patch

from langgraph.checkpoint.memory import InMemorySaver
import pytest

from app.config import Settings
from app.services.agent_runtime.a2a_completion import A2ARuntimeCompletionHandler
from app.services.agent_runtime.command_worker import CommandWorkResult, RuntimeRunRecord
from app.services.agent_runtime.channel_delivery import ChannelDeliveryWorkResult
from app.services.agent_runtime.heartbeat_completion import (
    HeartbeatRuntimeCompletionHandler,
)
from app.services.agent_runtime.onboarding_completion import (
    OnboardingRuntimeCompletionHandler,
)
from app.services.agent_runtime.planning_scheduler import (
    PlanningCheckpointScheduler,
)
from app.services.agent_runtime.product_reconciler import ProductReconcileResult
from app.services.agent_runtime.scheduling_lane import SchedulingLaneCompletionHandler
from app.services.agent_runtime.session_context_completion import (
    SessionContextCompletionHandler,
)
from app.services.agent_runtime.state import RunRegistrySnapshot
from app.services.agent_runtime.task_completion import TaskRuntimeCompletionHandler
from app.services.agent_runtime.tool_result_store import ToolResultReconcileResult
from app.services.agent_runtime.trigger_completion import TriggerRuntimeCompletionHandler
from app.services.agent_runtime.verification import (
    RuntimeToolReferenceReader,
    ToolLedgerRuntimeVerifier,
)
from app.services.agent_runtime.worker_service import (
    ChannelDeliveryDaemon,
    ProductReconcileDaemon,
    RuntimeCommandDaemon,
    RuntimeCommandDaemonSupervisor,
    RuntimeSchemaNotReady,
    ToolResultReconcileDaemon,
    assert_runtime_schema_ready,
    build_runtime_worker_components,
    running_runtime_worker_context,
    runtime_worker_context,
)


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        AGENT_RUNTIME_GRAPH_NAME="worker_service_test",
        AGENT_RUNTIME_GRAPH_VERSION="v1",
    )


class _Worker:
    def __init__(self, stop: asyncio.Event, *results: object) -> None:
        self.stop = stop
        self.results = deque(results)
        self.calls = 0

    async def run_once(self) -> CommandWorkResult:
        self.calls += 1
        result = self.results.popleft()
        if not self.results:
            self.stop.set()
        if isinstance(result, Exception):
            raise result
        return result  # type: ignore[return-value]


class _Session:
    def __init__(self) -> None:
        self.statements: list[object] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    def begin(self):
        return self

    async def execute(self, statement):
        self.statements.append(statement)
        return type("MutationResult", (), {"rowcount": 0})()


class _SessionFactory:
    def __init__(self) -> None:
        self.sessions: list[_Session] = []

    def __call__(self) -> _Session:
        session = _Session()
        self.sessions.append(session)
        return session


class _Engine:
    pass


class _ScalarResult:
    def __init__(self, value: object) -> None:
        self.value = value

    def scalar_one_or_none(self) -> object:
        return self.value


class _SchemaConnection:
    def __init__(self, tables: set[str]) -> None:
        self.tables = tables

    async def __aenter__(self) -> "_SchemaConnection":
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> bool:
        return False

    async def execute(self, _statement, parameters) -> _ScalarResult:
        name = parameters["table_name"]
        return _ScalarResult(name if name in self.tables else None)


class _SchemaEngine:
    def __init__(self, tables: set[str]) -> None:
        self.connection = _SchemaConnection(tables)

    def connect(self) -> _SchemaConnection:
        return self.connection


@pytest.mark.asyncio
async def test_daemon_continues_after_iteration_error_until_stopped() -> None:
    stop = asyncio.Event()
    worker = _Worker(
        stop,
        RuntimeError("database unavailable"),
        CommandWorkResult(status="idle"),
    )
    daemon = RuntimeCommandDaemon(
        worker,  # type: ignore[arg-type]
        idle_delay_seconds=0.001,
        retry_delay_seconds=0.001,
        error_delay_seconds=0.001,
    )

    await asyncio.wait_for(daemon.run(stop), timeout=1)

    assert worker.calls == 2


def test_command_daemon_idle_delay_backs_off_to_ceiling() -> None:
    daemon = RuntimeCommandDaemon(
        _Worker(asyncio.Event()),  # type: ignore[arg-type]
        idle_delay_seconds=0.25,
        retry_delay_seconds=0.1,
        error_delay_seconds=1.0,
        max_backoff_seconds=1.0,
    )

    delays = [daemon._delay_after(CommandWorkResult(status="idle")) for _ in range(5)]

    assert delays == [0.25, 0.5, 1.0, 1.0, 1.0]


def test_command_daemon_busy_result_resets_backoff() -> None:
    daemon = RuntimeCommandDaemon(
        _Worker(asyncio.Event()),  # type: ignore[arg-type]
        idle_delay_seconds=0.25,
        retry_delay_seconds=0.1,
        error_delay_seconds=1.0,
        max_backoff_seconds=1.0,
    )

    assert daemon._delay_after(CommandWorkResult(status="idle")) == 0.25
    assert daemon._delay_after(CommandWorkResult(status="idle")) == 0.5
    assert daemon._delay_after(CommandWorkResult(status="applied")) == 0.0
    assert daemon._delay_after(CommandWorkResult(status="idle")) == 0.25


def test_command_daemon_retry_backs_off_from_retry_base() -> None:
    daemon = RuntimeCommandDaemon(
        _Worker(asyncio.Event()),  # type: ignore[arg-type]
        idle_delay_seconds=1.0,
        retry_delay_seconds=0.1,
        error_delay_seconds=1.0,
        max_backoff_seconds=1.0,
    )

    delays = [daemon._delay_after(CommandWorkResult(status="retry")) for _ in range(6)]

    assert delays == [0.1, 0.2, 0.4, 0.8, 1.0, 1.0]


def test_command_daemon_backoff_does_not_overflow_after_long_idle_streak() -> None:
    """2 ** quiet_steps must not overflow float after a very long idle streak.

    Regresses the 2026-08-21 outage: an unbounded quiet streak reached
    ~1025 idle polls and ``2 ** (quiet_steps - 1)`` raised ``OverflowError``,
    crashing every command daemon.
    """
    daemon = RuntimeCommandDaemon(
        _Worker(asyncio.Event()),  # type: ignore[arg-type]
        idle_delay_seconds=1.0,
        retry_delay_seconds=0.1,
        error_delay_seconds=1.0,
        max_backoff_seconds=4.0,
    )

    # Far past the previous ~1024-polls overflow threshold.
    for _ in range(3000):
        daemon._delay_after(CommandWorkResult(status="idle"))

    # Still pinned at the ceiling, never OverflowError.
    assert daemon._delay_after(CommandWorkResult(status="idle")) == 4.0


def test_command_daemon_rejects_nonpositive_backoff_ceiling() -> None:
    with pytest.raises(ValueError):
        RuntimeCommandDaemon(
            _Worker(asyncio.Event()),  # type: ignore[arg-type]
            max_backoff_seconds=0,
        )


@pytest.mark.asyncio
async def test_channel_delivery_daemon_continues_after_retry() -> None:
    stop = asyncio.Event()

    class DeliveryWorker:
        def __init__(self) -> None:
            self.calls = 0

        async def run_once(self) -> ChannelDeliveryWorkResult:
            self.calls += 1
            if self.calls == 2:
                stop.set()
                return ChannelDeliveryWorkResult(status="idle")
            return ChannelDeliveryWorkResult(status="retry")

    worker = DeliveryWorker()
    daemon = ChannelDeliveryDaemon(
        worker,  # type: ignore[arg-type]
        scan_delay_seconds=0.001,
        error_delay_seconds=0.001,
    )

    await asyncio.wait_for(daemon.run(stop), timeout=1)

    assert worker.calls == 2


@pytest.mark.asyncio
async def test_product_reconcile_daemon_retries_independently() -> None:
    stop = asyncio.Event()

    class Reconciler:
        def __init__(self) -> None:
            self.calls = 0

        async def run_once(self) -> ProductReconcileResult:
            self.calls += 1
            if self.calls == 2:
                stop.set()
                return ProductReconcileResult(status="idle")
            return ProductReconcileResult(status="retry")

    reconciler = Reconciler()
    daemon = ProductReconcileDaemon(
        reconciler,  # type: ignore[arg-type]
        scan_delay_seconds=0.001,
        error_delay_seconds=0.001,
    )

    await asyncio.wait_for(daemon.run(stop), timeout=1)

    assert reconciler.calls == 2


@pytest.mark.asyncio
async def test_tool_result_reconcile_daemon_defers_without_busy_looping() -> None:
    stop = asyncio.Event()

    class Reconciler:
        def __init__(self) -> None:
            self.calls = 0

        async def run_once(self) -> ToolResultReconcileResult:
            self.calls += 1
            if self.calls == 2:
                stop.set()
                return ToolResultReconcileResult(status="idle")
            return ToolResultReconcileResult(status="deferred")

    reconciler = Reconciler()
    daemon = ToolResultReconcileDaemon(
        reconciler,  # type: ignore[arg-type]
        scan_delay_seconds=0.001,
        error_delay_seconds=0.001,
    )

    await asyncio.wait_for(daemon.run(stop), timeout=1)

    assert reconciler.calls == 2


def test_component_builder_installs_current_agent_and_planning_graphs() -> None:
    components = build_runtime_worker_components(
        checkpointer=InMemorySaver(),
        session_factory=_SessionFactory(),  # type: ignore[arg-type]
        lock_engine=_Engine(),  # type: ignore[arg-type]
        claimant="worker-test",
        settings=_settings(),
    )

    assert components.graph.identity.name == "worker_service_test"
    assert components.graph.identity.version == "v1"
    assert components.planning_graph.identity.name == "worker_service_test_group_planning"
    assert components.planning_graph.identity.version == "v1"
    tenant_id = uuid.uuid4()
    run_id = uuid.uuid4()
    registry = RunRegistrySnapshot(
        tenant_id=str(tenant_id),
        run_id=str(run_id),
        goal="test",
        run_kind="foreground",
        source_type="chat",
        model_id=str(uuid.uuid4()),
        graph_name="worker_service_test",
        graph_version="v1",
    )
    run = RuntimeRunRecord(
        tenant_id=tenant_id,
        run_id=run_id,
        thread_id=str(run_id),
        runtime_type="langgraph",
        goal=registry.goal,
        run_kind=registry.run_kind,
        source_type=registry.source_type,
        model_id=registry.model_id,
        graph_name=registry.graph_name,
        graph_version=registry.graph_version,
        agent_id=registry.agent_id,
        session_id=registry.session_id,
        system_role=registry.system_role,
        parent_run_id=registry.parent_run_id,
        root_run_id=registry.root_run_id,
    )
    assert components.graph_registry.resolve(run) is components.graph
    planning_run = replace(
        run,
        run_kind="orchestration",
        system_role="group_planning",
        graph_name="legacy-planning-name",
        graph_version="old-version",
    )
    assert components.graph_registry.resolve(planning_run) is components.planning_graph
    assert components.worker._checkpoint_reader is components.driver
    assert components.worker._command_executor is components.driver
    agent_executor = components.driver._node_executor._agent_executor
    assert isinstance(agent_executor._verifier, ToolLedgerRuntimeVerifier)
    reference_exists = agent_executor._verifier._reference_exists
    assert reference_exists is not None
    assert isinstance(reference_exists.__self__, RuntimeToolReferenceReader)
    assert agent_executor._verifier._result_store is not None
    assert agent_executor._verifier._result_store is agent_executor._tool_service._tool_result_store
    assert components.tool_result_reconciler._result_store is agent_executor._tool_service._tool_result_store
    assert components.product_reconciler._checkpoint_reader is components.driver
    assert components.product_reconciler._handler is components.worker._post_checkpoint_handler
    assert components.channel_delivery_worker._claimant == "worker-test"
    assert components.async_tool_poll_scheduler._session_factory is not None
    assert components.worker._pre_command_handler is None
    terminal_handlers = components.worker._post_checkpoint_handler._terminal_handlers
    assert [type(handler) for handler in terminal_handlers] == [
        SessionContextCompletionHandler,
        TaskRuntimeCompletionHandler,
        TriggerRuntimeCompletionHandler,
        HeartbeatRuntimeCompletionHandler,
        OnboardingRuntimeCompletionHandler,
        A2ARuntimeCompletionHandler,
        SchedulingLaneCompletionHandler,
    ]
    checkpoint_handlers = components.worker._post_checkpoint_handler._checkpoint_handlers
    assert [type(handler) for handler in checkpoint_handlers] == [
        PlanningCheckpointScheduler,
    ]


@pytest.mark.asyncio
async def test_worker_context_keeps_supplied_checkpointer_open() -> None:
    timeline: list[str] = []
    session_factory = _SessionFactory()

    @asynccontextmanager
    async def manager():
        timeline.append("checkpointer_enter")
        yield InMemorySaver()
        timeline.append("checkpointer_exit")

    async with runtime_worker_context(
        settings=_settings(),
        checkpointer_manager=manager(),
        session_factory=session_factory,  # type: ignore[arg-type]
        lock_engine=_Engine(),  # type: ignore[arg-type]
        claimant="worker-test",
        verify_schema=False,
    ):
        timeline.append("worker_active")

    assert timeline == [
        "checkpointer_enter",
        "worker_active",
        "checkpointer_exit",
    ]
    assert len(session_factory.sessions) == 1
    # Two statements: release_rejected_start_lanes + release_completed_start_lanes
    # (the latter added by 373d0c00 as part of the lane-leak fix).
    assert len(session_factory.sessions[0].statements) == 2


@pytest.mark.asyncio
async def test_running_context_stops_daemon_before_closing_checkpointer() -> None:
    timeline: list[str] = []

    @asynccontextmanager
    async def manager():
        timeline.append("checkpointer_enter")
        yield InMemorySaver()
        timeline.append("checkpointer_exit")

    async with running_runtime_worker_context(
        settings=_settings(),
        checkpointer_manager=manager(),
        session_factory=_SessionFactory(),  # type: ignore[arg-type]
        lock_engine=_Engine(),  # type: ignore[arg-type]
        claimant="worker-test",
        verify_schema=False,
    ):
        timeline.append("daemon_active")
        await asyncio.sleep(0)

    assert timeline == [
        "checkpointer_enter",
        "daemon_active",
        "checkpointer_exit",
    ]


@pytest.mark.asyncio
async def test_running_context_starts_configured_command_concurrency() -> None:
    started: list[str] = []
    all_started = asyncio.Event()

    async def record_daemon_start(_daemon, stop: asyncio.Event) -> None:
        task = asyncio.current_task()
        assert task is not None
        started.append(task.get_name())
        if len(started) == 3:
            all_started.set()
        await stop.wait()

    @asynccontextmanager
    async def manager():
        yield InMemorySaver()

    settings = Settings(
        _env_file=None,
        AGENT_RUNTIME_GRAPH_NAME="worker_service_test",
        AGENT_RUNTIME_GRAPH_VERSION="v1",
        AGENT_RUNTIME_COMMAND_CONCURRENCY=3,
    )
    with patch.object(RuntimeCommandDaemon, "run", new=record_daemon_start):
        async with running_runtime_worker_context(
            settings=settings,
            checkpointer_manager=manager(),
            session_factory=_SessionFactory(),  # type: ignore[arg-type]
            lock_engine=_Engine(),  # type: ignore[arg-type]
            claimant="worker-test",
            verify_schema=False,
        ):
            await asyncio.wait_for(all_started.wait(), timeout=1)

    assert sorted(started) == [
        "agent-runtime-command-worker-1",
        "agent-runtime-command-worker-2",
        "agent-runtime-command-worker-3",
    ]


@pytest.mark.asyncio
async def test_running_context_surfaces_crashed_command_daemon(caplog) -> None:
    """A command daemon that dies is logged at crash time, not silently swallowed.

    Regresses the 2026-08-20 outage where the command daemons had no
    done-callback, so a terminated task left the Command Inbox undrained and
    runs stuck "thinking" with no log line to explain why.
    """

    async def crash(_daemon, stop: asyncio.Event) -> None:
        raise RuntimeError("command worker exploded")

    @asynccontextmanager
    async def manager():
        yield InMemorySaver()

    settings = Settings(
        _env_file=None,
        AGENT_RUNTIME_GRAPH_NAME="worker_service_test",
        AGENT_RUNTIME_GRAPH_VERSION="v1",
        AGENT_RUNTIME_COMMAND_CONCURRENCY=1,
    )
    with (
        patch.object(RuntimeCommandDaemon, "run", new=crash),
        caplog.at_level(
            logging.ERROR,
            logger="app.services.agent_runtime.worker_service",
        ),
    ):
        # The exception escapes the daemon loop entirely, so the task ends and
        # the done-callback must surface it; cleanup re-raises the stored error.
        with pytest.raises(RuntimeError, match="command worker exploded"):
            async with running_runtime_worker_context(
                settings=settings,
                checkpointer_manager=manager(),
                session_factory=_SessionFactory(),  # type: ignore[arg-type]
                lock_engine=_Engine(),  # type: ignore[arg-type]
                claimant="worker-test",
                verify_schema=False,
            ):
                await asyncio.sleep(0)
        # Flush the done-callback, which is scheduled via call_soon when the
        # task transitions to done and runs on the next loop iteration.
        await asyncio.sleep(0)

    assert "CRASHED" in caplog.text
    assert "command worker exploded" in caplog.text


@pytest.mark.asyncio
async def test_supervisor_reports_stalled_daemon_with_stack(caplog) -> None:
    """A daemon that stops advancing last_active_at is surfaced with its stack."""
    daemon = RuntimeCommandDaemon(object())  # type: ignore[arg-type]
    daemon.last_active_at = time.monotonic() - 1000.0

    async def hang() -> None:
        await asyncio.sleep(3600)

    task = asyncio.create_task(hang(), name="agent-runtime-command-worker-1")
    await asyncio.sleep(0)  # let the task start and suspend at the sleep

    supervisor = RuntimeCommandDaemonSupervisor(
        [(task, daemon)],
        scan_seconds=1.0,
        stall_seconds=60.0,
        heartbeat_seconds=300.0,
    )
    with caplog.at_level(
        logging.ERROR,
        logger="app.services.agent_runtime.worker_service",
    ):
        supervisor._check()

    assert "STALLED" in caplog.text
    assert "agent-runtime-command-worker-1" in caplog.text

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_supervisor_heartbeat_logs_when_healthy(caplog) -> None:
    """All daemons fresh ⇒ a low-noise alive heartbeat fires, no stall report."""
    daemon = RuntimeCommandDaemon(object())  # type: ignore[arg-type]
    daemon.last_active_at = time.monotonic()

    async def hang() -> None:
        await asyncio.sleep(3600)

    task = asyncio.create_task(hang(), name="agent-runtime-command-worker-1")
    await asyncio.sleep(0)

    supervisor = RuntimeCommandDaemonSupervisor(
        [(task, daemon)],
        scan_seconds=1.0,
        stall_seconds=60.0,
        heartbeat_seconds=300.0,
    )
    supervisor._last_heartbeat_at = time.monotonic() - 1000.0
    with caplog.at_level(
        logging.INFO,
        logger="app.services.agent_runtime.worker_service",
    ):
        supervisor._check()

    assert "alive" in caplog.text
    assert "STALLED" not in caplog.text

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_schema_readiness_requires_every_product_table() -> None:
    with (
        patch(
            "app.services.agent_runtime.worker_service._checkpoint_migration_version",
            new=AsyncMock(return_value=9),
        ),
        pytest.raises(RuntimeSchemaNotReady, match="agent_tool_executions") as raised,
    ):
        await assert_runtime_schema_ready(
            _SchemaEngine(
                {
                    "agent_runs",
                    "agent_run_commands",
                    "agent_run_events",
                    "session_context_states",
                    "channel_deliveries",
                }
            ),  # type: ignore[arg-type]
            settings=_settings(),
        )

    assert raised.value.code == "product_schema_incomplete"


@pytest.mark.asyncio
async def test_schema_readiness_requires_pinned_checkpoint_version() -> None:
    with (
        patch(
            "app.services.agent_runtime.worker_service._checkpoint_migration_version",
            new=AsyncMock(return_value=8),
        ),
        pytest.raises(RuntimeSchemaNotReady, match="expected 9") as raised,
    ):
        await assert_runtime_schema_ready(
            _SchemaEngine(
                {
                    "agent_runs",
                    "agent_run_commands",
                    "agent_run_events",
                    "agent_tool_executions",
                    "session_context_states",
                    "channel_deliveries",
                }
            ),  # type: ignore[arg-type]
            settings=_settings(),
        )

    assert raised.value.code == "checkpoint_schema_outdated"


@pytest.mark.asyncio
async def test_schema_readiness_accepts_complete_pinned_schema() -> None:
    checkpoint_version = AsyncMock(return_value=9)
    with patch(
        "app.services.agent_runtime.worker_service._checkpoint_migration_version",
        new=checkpoint_version,
    ):
        await assert_runtime_schema_ready(
            _SchemaEngine(
                {
                    "agent_runs",
                    "agent_run_commands",
                    "agent_run_events",
                    "agent_tool_executions",
                    "session_context_states",
                    "channel_deliveries",
                }
            ),  # type: ignore[arg-type]
            settings=_settings(),
        )

    checkpoint_version.assert_awaited_once()
