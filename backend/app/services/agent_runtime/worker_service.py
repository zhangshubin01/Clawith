"""Production composition and daemon loop for the durable Runtime worker."""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager, asynccontextmanager, suppress
from dataclasses import dataclass
import asyncio
import logging
import os
import socket
import time
from typing import AsyncIterator, Sequence
import uuid

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.errors import GraphRecursionError
from psycopg import AsyncConnection as PsycopgAsyncConnection
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.config import Settings, get_settings
from app.services.agent_runtime.a2a_completion import A2ARuntimeCompletionHandler
from app.services.agent_runtime.a2a_runtime import RuntimeA2AService
from app.services.agent_runtime.async_tool_poll import (
    AsyncToolPollResult,
    AsyncToolPollScheduler,
)
from app.services.agent_runtime.cancel_source import DatabaseRuntimeCancelSource
from app.services.agent_runtime.channel_delivery import (
    ChannelDeliveryWorkResult,
    ChannelDeliveryWorker,
)
from app.services.agent_runtime.channel_provider_delivery import (
    DatabaseChannelDeliverySender,
)
from app.services.agent_runtime.checkpoint_side_effects import RuntimeCheckpointSideEffects
from app.services.agent_runtime.checkpointer import (
    checkpoint_database_url,
    create_checkpointer,
)
from app.services.agent_runtime.command_worker import (
    CommandWorkResult,
    RuntimeCommandWorker,
    RuntimeSessionFactory,
)
from app.services.agent_runtime.context_builder import ContextBuilder
from app.services.agent_runtime.graph import (
    AgentRuntimeGraph,
    RuntimeGraphIdentity,
    build_agent_runtime_graph,
)
from app.services.agent_runtime.heartbeat_completion import (
    HeartbeatRuntimeCompletionHandler,
)
from app.services.agent_runtime.langgraph_driver import (
    LangGraphRuntimeDriver,
    RuntimeGraphRegistry,
    RuntimeInputSnapshotFactory,
)
from app.services.agent_runtime.model_step_service import RuntimeModelStepService
from app.services.agent_runtime.node_executor import DeterministicRuntimeNodeExecutor
from app.services.agent_runtime.onboarding_completion import (
    OnboardingRuntimeCompletionHandler,
)
from app.services.agent_runtime.planning import (
    PlanningModelService,
    PlanningRuntimeNodeExecutor,
    RuntimeNodeExecutorRouter,
)
from app.services.agent_runtime.planning_scheduler import PlanningCheckpointScheduler
from app.services.agent_runtime.persistence import release_rejected_start_lanes, release_completed_start_lanes
from app.services.agent_runtime.product_reconciler import (
    ProductReconcileResult,
    RuntimeProductReconciler,
)
from app.services.agent_runtime.run_compactor import RuntimeRunCompactorService
from app.services.agent_runtime.scheduling_lane import SchedulingLaneCompletionHandler
from app.services.agent_runtime.session_context_service import SessionContextService
from app.services.agent_runtime.session_context_compactor import LLMSessionContextCompactor
from app.services.agent_runtime.session_context_background import (
    SessionCompactPolicyResolver,
    SessionContextCompactionScanner,
    SessionContextMessageCompactionService,
)
from app.services.agent_runtime.session_context_completion import (
    SessionContextCompletionHandler,
)
from app.services.agent_runtime.task_completion import TaskRuntimeCompletionHandler
from app.services.agent_runtime.tool_lease_reconcile import (
    ToolLeaseReconcileResult,
    ToolLeaseReconcileScheduler,
)
from app.services.agent_runtime.tool_step_service import RuntimeToolStepService
from app.services.agent_runtime.tool_result_store import (
    ToolResultReconcileResult,
    ToolResultReconciler,
    ToolResultStore,
)
from app.services.agent_runtime.trigger_completion import TriggerRuntimeCompletionHandler
from app.services.agent_runtime.verification import (
    RuntimeToolReferenceReader,
    ToolLedgerRuntimeVerifier,
)


logger = logging.getLogger(__name__)

_REQUIRED_PRODUCT_TABLES = (
    "agent_runs",
    "agent_run_commands",
    "agent_run_events",
    "agent_tool_executions",
    "session_context_states",
    "channel_deliveries",
)
_EXPECTED_CHECKPOINT_MIGRATION = len(AsyncPostgresSaver.MIGRATIONS) - 1


class RuntimeSchemaNotReady(RuntimeError):
    """Runtime code is enabled before its explicit migrations are complete."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class RuntimeWorkerComponents:
    """Long-lived Runtime objects sharing one installed Checkpointer."""

    graph: AgentRuntimeGraph
    planning_graph: AgentRuntimeGraph
    graph_registry: RuntimeGraphRegistry
    driver: LangGraphRuntimeDriver
    worker: RuntimeCommandWorker
    async_tool_poll_scheduler: AsyncToolPollScheduler
    tool_lease_reconciler: ToolLeaseReconcileScheduler
    tool_result_reconciler: ToolResultReconciler
    product_reconciler: RuntimeProductReconciler
    channel_delivery_worker: ChannelDeliveryWorker
    session_context_scanner: SessionContextCompactionScanner


def runtime_worker_claimant() -> str:
    """Return a process-unique claimant that fits the persisted column."""
    hostname = socket.gethostname().strip() or "unknown-host"
    return f"{hostname}:{os.getpid()}:{uuid.uuid4().hex}"[:128]


async def _checkpoint_migration_version(settings: Settings) -> int | None:
    try:
        connection = await PsycopgAsyncConnection.connect(
            checkpoint_database_url(settings),
            autocommit=True,
        )
        async with connection:
            async with connection.cursor() as cursor:
                await cursor.execute("SELECT max(v) FROM checkpoint_migrations")
                row = await cursor.fetchone()
    except Exception as exc:
        raise RuntimeSchemaNotReady(
            "checkpoint_schema_unavailable",
            "LangGraph checkpoint schema is unavailable; run the explicit setup command",
        ) from exc
    if row is None or row[0] is None:
        return None
    return int(row[0])


async def assert_runtime_schema_ready(
    engine: AsyncEngine,
    *,
    settings: Settings | None = None,
) -> None:
    """Fail startup unless product Alembic and official saver setup both ran."""
    runtime_settings = settings or get_settings()
    missing: list[str] = []
    try:
        async with engine.connect() as connection:
            for table_name in _REQUIRED_PRODUCT_TABLES:
                result = await connection.execute(
                    text("SELECT to_regclass(:table_name)"),
                    {"table_name": table_name},
                )
                if result.scalar_one_or_none() is None:
                    missing.append(table_name)
    except Exception as exc:
        raise RuntimeSchemaNotReady(
            "product_schema_unavailable",
            "Agent Runtime product schema could not be inspected",
        ) from exc
    if missing:
        raise RuntimeSchemaNotReady(
            "product_schema_incomplete",
            "Agent Runtime migration is required; missing tables: " + ", ".join(missing),
        )

    checkpoint_version = await _checkpoint_migration_version(runtime_settings)
    if checkpoint_version != _EXPECTED_CHECKPOINT_MIGRATION:
        raise RuntimeSchemaNotReady(
            "checkpoint_schema_outdated",
            "LangGraph checkpoint setup version does not match the pinned package "
            f"(expected {_EXPECTED_CHECKPOINT_MIGRATION}, found {checkpoint_version})",
        )


def build_runtime_worker_components(
    *,
    checkpointer: BaseCheckpointSaver,
    session_factory: RuntimeSessionFactory,
    lock_engine: AsyncEngine,
    claimant: str | None = None,
    settings: Settings | None = None,
) -> RuntimeWorkerComponents:
    """Compose one Graph and Worker without opening connections or starting tasks."""
    runtime_settings = settings or get_settings()
    session_context_service = SessionContextService(settings=runtime_settings)
    session_context_compactor = LLMSessionContextCompactor(
        session_factory=session_factory,
        settings=runtime_settings,
    )
    context_builder = ContextBuilder(
        session_context_service,
        settings=runtime_settings,
        session_context_compactor=session_context_compactor,
    )
    cancel_source = DatabaseRuntimeCancelSource(session_factory=session_factory)
    model_service = RuntimeModelStepService(
        session_factory=session_factory,
        context_builder=context_builder,
    )
    tool_result_store = ToolResultStore(session_factory=session_factory)
    tool_result_reconciler = ToolResultReconciler(
        session_factory=session_factory,
        result_store=tool_result_store,
    )
    reference_reader = RuntimeToolReferenceReader(
        session_factory=session_factory,
    )
    tool_service = RuntimeToolStepService(
        session_factory=session_factory,
        cancel_source=cancel_source,
        a2a_service=RuntimeA2AService(
            session_factory=session_factory,
            settings=runtime_settings,
        ),
        tool_result_store=tool_result_store,
        tool_result_reconciler=tool_result_reconciler,
    )
    run_compactor = RuntimeRunCompactorService(
        settings=runtime_settings,
        input_loader=model_service.compact_inputs,
    )
    agent_node_executor = DeterministicRuntimeNodeExecutor(
        cancel_source=cancel_source,
        model_service=model_service,
        tool_service=tool_service,
        run_compactor=run_compactor,
        verifier=ToolLedgerRuntimeVerifier(
            session_factory=session_factory,
            result_store=tool_result_store,
            reference_exists=reference_reader.reference_exists,
        ),
    )
    graph = build_agent_runtime_graph(
        checkpointer=checkpointer,
        settings=runtime_settings,
    )
    planning_graph = build_agent_runtime_graph(
        checkpointer=checkpointer,
        settings=runtime_settings,
        identity=RuntimeGraphIdentity.planning_from_settings(runtime_settings),
    )
    planning_node_executor = PlanningRuntimeNodeExecutor(
        cancel_source=cancel_source,
        model_service=PlanningModelService(session_factory=session_factory),
    )
    node_executor = RuntimeNodeExecutorRouter(
        agent_executor=agent_node_executor,
        planning_executor=planning_node_executor,
    )
    graph_registry = RuntimeGraphRegistry([graph, planning_graph])
    driver = LangGraphRuntimeDriver(
        graph_registry=graph_registry,
        snapshot_factory=RuntimeInputSnapshotFactory(context_builder),
        node_executor=node_executor,
    )
    session_context_scanner = SessionContextCompactionScanner(
        session_factory=session_factory,
        service=SessionContextMessageCompactionService(
            lock_engine=lock_engine,
            compactor=session_context_compactor,
            context_service=session_context_service,
            policy_resolver=SessionCompactPolicyResolver(
                settings=runtime_settings,
            ),
        ),
        settings=runtime_settings,
    )
    post_checkpoint_handler = RuntimeCheckpointSideEffects(
        session_factory=session_factory,
        checkpoint_handlers=(
            PlanningCheckpointScheduler(
                session_factory=session_factory,
                settings=runtime_settings,
            ),
        ),
        terminal_handlers=(
            SessionContextCompletionHandler(
                session_factory=session_factory,
                context_service=session_context_service,
            ),
            TaskRuntimeCompletionHandler(session_factory=session_factory),
            TriggerRuntimeCompletionHandler(session_factory=session_factory),
            HeartbeatRuntimeCompletionHandler(session_factory=session_factory),
            OnboardingRuntimeCompletionHandler(session_factory=session_factory),
            A2ARuntimeCompletionHandler(session_factory=session_factory),
            SchedulingLaneCompletionHandler(session_factory=session_factory),
        ),
    )
    resolved_claimant = claimant or runtime_worker_claimant()
    worker = RuntimeCommandWorker(
        session_factory=session_factory,
        lock_engine=lock_engine,
        checkpoint_reader=driver,
        command_executor=driver,
        pre_command_handler=None,
        post_checkpoint_handler=post_checkpoint_handler,
        rejection_handler=post_checkpoint_handler,
        claimant=resolved_claimant,
        settings=runtime_settings,
    )
    async_tool_poll_scheduler = AsyncToolPollScheduler(
        session_factory=session_factory,
    )
    tool_lease_reconciler = ToolLeaseReconcileScheduler(
        session_factory=session_factory,
    )
    product_reconciler = RuntimeProductReconciler(
        session_factory=session_factory,
        checkpoint_reader=driver,
        handler=post_checkpoint_handler,
    )
    channel_delivery_worker = ChannelDeliveryWorker(
        session_factory=session_factory,
        sender=DatabaseChannelDeliverySender(session_factory=session_factory),
        claimant=resolved_claimant,
        settings=runtime_settings,
    )
    return RuntimeWorkerComponents(
        graph=graph,
        planning_graph=planning_graph,
        graph_registry=graph_registry,
        driver=driver,
        worker=worker,
        async_tool_poll_scheduler=async_tool_poll_scheduler,
        tool_lease_reconciler=tool_lease_reconciler,
        tool_result_reconciler=tool_result_reconciler,
        product_reconciler=product_reconciler,
        channel_delivery_worker=channel_delivery_worker,
        session_context_scanner=session_context_scanner,
    )


# Ceiling for the idle/retry backoff streak. The backoff is already pinned at
# max_backoff_seconds after a tiny exponent, so this only exists to keep
# ``2 ** quiet_steps`` from overflowing float (OverflowError) during long idle
# periods. 16 is far below the ~1024 overflow threshold and far above the ~3-7
# steps any configured base/max actually needs.
_QUIET_STEPS_CEILING = 16


class RuntimeCommandDaemon:
    """Continuously drain the Command Inbox with bounded idle/error polling."""

    def __init__(
        self,
        worker: RuntimeCommandWorker,
        *,
        # 10 个并发 daemon 共用 claim_next_command（SKIP LOCKED）——0.25s 空闲轮询曾
        # 是全库第一热查询（占 pg_stat_statements 总执行时间 ~47%，而命令表稳态无
        # pending 行）。idle 只影响"新命令入队→开始处理"的延迟；忙时 delay=0 连续
        # 处理，吞吐不受影响。
        idle_delay_seconds: float = 1.0,
        retry_delay_seconds: float = 0.1,
        error_delay_seconds: float = 1.0,
        # Consecutive quiet results (idle / retry) double the delay up to this
        # ceiling; any busy outcome resets the backoff to zero. Concurrent
        # daemons drift apart naturally (run_once durations vary under SKIP
        # LOCKED), so expected pickup latency while quiet stays far below the
        # ceiling without any explicit phase spread.
        max_backoff_seconds: float = 4.0,
    ) -> None:
        delays = (idle_delay_seconds, retry_delay_seconds, error_delay_seconds)
        if any(delay <= 0 for delay in delays):
            raise ValueError("Runtime daemon delays must be positive")
        if max_backoff_seconds <= 0:
            raise ValueError("Runtime daemon backoff ceiling must be positive")
        self._worker = worker
        self._idle_delay_seconds = idle_delay_seconds
        self._retry_delay_seconds = retry_delay_seconds
        self._error_delay_seconds = error_delay_seconds
        self._max_backoff_seconds = max_backoff_seconds
        self._quiet_steps = 0
        # Monotonic clock of the last completed ``run_once`` iteration, read by
        # ``RuntimeCommandDaemonSupervisor`` to detect silent stalls. A stalled
        # daemon (stuck acquiring a DB session or in the claim query) never
        # advances this, so the supervisor can dump its stack and surface it.
        # It is also advanced mid-command by ``_touch_liveness`` (via the claim
        # heartbeat) so a long graph execution is not mistaken for a stall.
        self.last_active_at = time.monotonic()

    def _touch_liveness(self) -> None:
        """Advance ``last_active_at`` from the running command's heartbeat.

        A long graph execution keeps ``run_once`` from returning, which would
        otherwise freeze ``last_active_at`` and trip the supervisor's false
        STALLED alarm. The claim heartbeat calls this through the whole
        command, so the daemon is flagged only when it genuinely stops running.
        """
        self.last_active_at = time.monotonic()

    @staticmethod
    async def _wait(stop: asyncio.Event, delay: float) -> None:
        try:
            await asyncio.wait_for(stop.wait(), timeout=delay)
        except TimeoutError:
            pass

    async def run(self, stop: asyncio.Event) -> None:
        """Run until stopped; individual command failures never kill the daemon."""
        while not stop.is_set():
            delay = 0.0
            try:
                result = await self._worker.run_once(
                    liveness_touch=self._touch_liveness,
                )
            except asyncio.CancelledError:
                raise
            except GraphRecursionError:
                logger.warning(
                    "Runtime Command Worker iteration hit recursion limit (%s steps) — run may be stuck in a loop",
                    get_settings().AGENT_RUNTIME_RECURSION_LIMIT,
                )
                delay = self._error_delay_seconds
                self._advance_backoff(None)
            except Exception:
                logger.exception("Runtime Command Worker iteration failed")
                delay = self._error_delay_seconds
                self._advance_backoff(None)
            else:
                delay = self._delay_after(result)
            self.last_active_at = time.monotonic()
            if delay:
                await self._wait(stop, delay)

    def _advance_backoff(self, result: CommandWorkResult | None) -> None:
        """Grow the quiet streak on idle/retry; any other outcome resets it.

        The streak is capped: the delay is already pinned at
        ``max_backoff_seconds`` once the exponent exceeds
        ``log2(max_backoff_seconds / base)``, so a larger streak contributes
        nothing — and ``2 ** n`` would overflow ``float`` (``OverflowError``)
        at ``n ≈ 1024``, which a long idle period reaches (~1025 idle polls)
        and crashes the daemon (the 2026-08-21 outage).
        """
        if result is not None and result.status in ("idle", "retry"):
            self._quiet_steps = min(self._quiet_steps + 1, _QUIET_STEPS_CEILING)
        else:
            self._quiet_steps = 0

    def _delay_after(self, result: CommandWorkResult) -> float:
        self._advance_backoff(result)
        if result.status not in ("idle", "retry"):
            return 0.0
        base = self._idle_delay_seconds if result.status == "idle" else self._retry_delay_seconds
        return min(base * (2 ** (self._quiet_steps - 1)), self._max_backoff_seconds)


class RuntimeCommandDaemonSupervisor:
    """Watch command daemons for silent stalls and dump their stacks.

    A command daemon should complete ``run_once`` at least every few seconds
    (idle backoff ≤ ``max_backoff_seconds``). When it stops advancing
    ``last_active_at`` for ``stall_seconds`` — e.g. it is stuck acquiring a DB
    session from an exhausted pool, or in the claim query — the supervisor
    surfaces the stall with the daemon's coroutine stack so the exact await
    point is visible in logs instead of vanishing silently.
    """

    def __init__(
        self,
        daemons: Sequence[tuple[asyncio.Task, RuntimeCommandDaemon]],
        *,
        scan_seconds: float,
        stall_seconds: float,
        heartbeat_seconds: float = 300.0,
    ) -> None:
        if scan_seconds <= 0 or stall_seconds <= 0 or heartbeat_seconds <= 0:
            raise ValueError("supervisor delays must be positive")
        self._daemons = list(daemons)
        self._scan_seconds = scan_seconds
        self._stall_seconds = stall_seconds
        self._heartbeat_seconds = heartbeat_seconds
        self._reported: set[int] = set()
        self._last_heartbeat_at = time.monotonic()

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=self._scan_seconds)
                return
            except TimeoutError:
                self._check()

    def _check(self) -> None:
        now = time.monotonic()
        stalled = 0
        alive = 0
        for idx, (task, daemon) in enumerate(self._daemons):
            if task.done():
                # Already surfaced by the done-callback; not a stall.
                continue
            stale = now - daemon.last_active_at
            if stale >= self._stall_seconds:
                stalled += 1
                if idx not in self._reported:
                    self._reported.add(idx)
                    self._report_stall(task, stale)
            else:
                self._reported.discard(idx)
                alive += 1
        if stalled == 0 and now - self._last_heartbeat_at >= self._heartbeat_seconds:
            self._last_heartbeat_at = now
            logger.info(
                "Runtime command daemons alive: %d/%d (pool: %s)",
                alive,
                len(self._daemons),
                self._pool_status(),
            )

    @staticmethod
    def _pool_status() -> str:
        """Snapshot the SQLAlchemy async pool so a leak is visible over time."""
        try:
            from app.database import engine

            return engine.pool.status()
        except Exception as exc:  # pragma: no cover - diagnostic only
            return f"unknown ({exc})"

    def _report_stall(self, task: asyncio.Task, stale: float) -> None:
        frames = task.get_stack(limit=12)
        if frames:
            stack = "".join(
                f'  File "{frame.f_code.co_filename}", line {frame.f_lineno}, '
                f"in {frame.f_code.co_name}\n"
                for frame in frames
            )
        else:
            stack = "<no stack>"
        logger.error(
            "Runtime command daemon %r STALLED for %.0fs (run_once not completing). "
            "Pool: %s. May be a long-running command or a stuck DB session; stack:\n%s",
            task.get_name(),
            stale,
            self._pool_status(),
            stack,
        )


class ChannelDeliveryDaemon:
    """Continuously drain provider deliveries independently of Graph execution."""

    def __init__(
        self,
        worker: ChannelDeliveryWorker,
        *,
        scan_delay_seconds: float,
        error_delay_seconds: float = 1.0,
    ) -> None:
        if scan_delay_seconds <= 0 or error_delay_seconds <= 0:
            raise ValueError("Channel delivery daemon delays must be positive")
        self._worker = worker
        self._scan_delay_seconds = scan_delay_seconds
        self._error_delay_seconds = error_delay_seconds

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                result = await self._worker.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Runtime channel delivery iteration failed")
                delay = self._error_delay_seconds
            else:
                delay = self._delay_after(result)
            if delay:
                await RuntimeCommandDaemon._wait(stop, delay)

    def _delay_after(self, result: ChannelDeliveryWorkResult) -> float:
        if result.status in {"idle", "retry", "failed"}:
            return self._scan_delay_seconds
        return 0.0


class AsyncToolPollDaemon:
    """Schedule timer resumes without executing Tools outside LangGraph."""

    def __init__(
        self,
        scheduler: AsyncToolPollScheduler,
        *,
        scan_delay_seconds: float,
        error_delay_seconds: float = 1.0,
    ) -> None:
        if scan_delay_seconds <= 0 or error_delay_seconds <= 0:
            raise ValueError("async Tool poll daemon delays must be positive")
        self._scheduler = scheduler
        self._scan_delay_seconds = scan_delay_seconds
        self._error_delay_seconds = error_delay_seconds

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                result = await self._scheduler.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Runtime async Tool poll scheduling iteration failed")
                delay = self._error_delay_seconds
            else:
                delay = self._delay_after(result)
            if delay:
                await RuntimeCommandDaemon._wait(stop, delay)

    def _delay_after(self, result: AsyncToolPollResult) -> float:
        if result.status in {"idle", "deferred"}:
            return self._scan_delay_seconds
        return 0.0


class ToolLeaseReconcileDaemon:
    """Recover Runs parked behind expired Tool executor leases."""

    def __init__(
        self,
        scheduler: ToolLeaseReconcileScheduler,
        *,
        scan_delay_seconds: float,
        error_delay_seconds: float = 1.0,
    ) -> None:
        if scan_delay_seconds <= 0 or error_delay_seconds <= 0:
            raise ValueError("tool lease reconcile daemon delays must be positive")
        self._scheduler = scheduler
        self._scan_delay_seconds = scan_delay_seconds
        self._error_delay_seconds = error_delay_seconds

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                result = await self._scheduler.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Runtime tool lease reconciliation iteration failed")
                delay = self._error_delay_seconds
            else:
                delay = self._delay_after(result)
            if delay:
                await RuntimeCommandDaemon._wait(stop, delay)

    def _delay_after(self, result: ToolLeaseReconcileResult) -> float:
        if result.status == "idle":
            return self._scan_delay_seconds
        return 0.0


class ProductReconcileDaemon:
    """Retry products independently from Command and Graph execution."""

    def __init__(
        self,
        reconciler: RuntimeProductReconciler,
        *,
        scan_delay_seconds: float = 0.5,
        error_delay_seconds: float = 1.0,
    ) -> None:
        if scan_delay_seconds <= 0 or error_delay_seconds <= 0:
            raise ValueError("product reconciliation daemon delays must be positive")
        self._reconciler = reconciler
        self._scan_delay_seconds = scan_delay_seconds
        self._error_delay_seconds = error_delay_seconds

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                result = await self._reconciler.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Runtime product reconciliation iteration failed")
                delay = self._error_delay_seconds
            else:
                delay = self._delay_after(result)
            if delay:
                await RuntimeCommandDaemon._wait(stop, delay)

    def _delay_after(self, result: ProductReconcileResult) -> float:
        if result.status in {"idle", "retry"}:
            return self._scan_delay_seconds
        return 0.0


class ToolResultReconcileDaemon:
    """Recover archived results independently without re-executing tools."""

    def __init__(
        self,
        reconciler: ToolResultReconciler,
        *,
        scan_delay_seconds: float = 0.5,
        error_delay_seconds: float = 1.0,
    ) -> None:
        if scan_delay_seconds <= 0 or error_delay_seconds <= 0:
            raise ValueError("tool result reconciliation daemon delays must be positive")
        self._reconciler = reconciler
        self._scan_delay_seconds = scan_delay_seconds
        self._error_delay_seconds = error_delay_seconds

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                result = await self._reconciler.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Runtime tool result reconciliation iteration failed")
                delay = self._error_delay_seconds
            else:
                delay = self._delay_after(result)
            if delay:
                await RuntimeCommandDaemon._wait(stop, delay)

    def _delay_after(self, result: ToolResultReconcileResult) -> float:
        if result.status in {"idle", "deferred"}:
            return self._scan_delay_seconds
        return 0.0


@asynccontextmanager
async def runtime_worker_context(
    *,
    settings: Settings | None = None,
    checkpointer_manager: AbstractAsyncContextManager[BaseCheckpointSaver] | None = None,
    session_factory: RuntimeSessionFactory | None = None,
    lock_engine: AsyncEngine | None = None,
    claimant: str | None = None,
    verify_schema: bool = True,
) -> AsyncIterator[RuntimeWorkerComponents]:
    """Keep the Checkpointer open for exactly the Worker component lifetime."""
    runtime_settings = settings or get_settings()
    if session_factory is None or lock_engine is None:
        from app.database import async_session, engine

        session_factory = session_factory or async_session
        lock_engine = lock_engine or engine
    if verify_schema:
        await assert_runtime_schema_ready(lock_engine, settings=runtime_settings)
    async with session_factory() as db:
        async with db.begin():
            rejected_lanes = await release_rejected_start_lanes(db)
            completed_lanes = await release_completed_start_lanes(db)
    total_lanes = (rejected_lanes or 0) + (completed_lanes or 0)
    if total_lanes:
        logger.warning(
            f"Released {total_lanes} stuck scheduling lane(s) on startup "
            f"(rejected={rejected_lanes}, completed={completed_lanes})",
        )
    manager = checkpointer_manager or create_checkpointer(runtime_settings)
    async with manager as checkpointer:
        yield build_runtime_worker_components(
            checkpointer=checkpointer,
            session_factory=session_factory,
            lock_engine=lock_engine,
            claimant=claimant,
            settings=runtime_settings,
        )


def _surface_task_crash(task: asyncio.Task) -> None:
    """Log a daemon task that died silently.

    These daemon loops run forever until ``stop`` is set, so a task that ends
    any other way — an exception raised during construction, a ``BaseException``
    escaping the loop, or the event loop dropping it — previously vanished
    without a trace, leaving the Command Inbox undrained and runs stuck
    "thinking" (see run 5899881f / command 67901e70, 2026-08-20). Unlike the
    app-level background tasks in ``app.main``, these tasks had no
    ``add_done_callback``, so nothing surfaced the crash.
    """
    try:
        exc = task.exception()
    except asyncio.CancelledError:
        # Normal shutdown path: the context manager cancels each daemon.
        return
    if exc is None:
        return
    logger.error(
        "Runtime daemon task %r CRASHED: %s",
        task.get_name(),
        exc,
        exc_info=(type(exc), exc, exc.__traceback__),
    )


@asynccontextmanager
async def running_runtime_worker_context(
    *,
    settings: Settings | None = None,
    checkpointer_manager: AbstractAsyncContextManager[BaseCheckpointSaver] | None = None,
    session_factory: RuntimeSessionFactory | None = None,
    lock_engine: AsyncEngine | None = None,
    claimant: str | None = None,
    verify_schema: bool = True,
) -> AsyncIterator[RuntimeWorkerComponents]:
    """Run and cancel the daemon within the Checkpointer component lifetime."""
    runtime_settings = settings or get_settings()
    async with runtime_worker_context(
        settings=runtime_settings,
        checkpointer_manager=checkpointer_manager,
        session_factory=session_factory,
        lock_engine=lock_engine,
        claimant=claimant,
        verify_schema=verify_schema,
    ) as components:
        stop = asyncio.Event()
        command_daemons = [
            RuntimeCommandDaemon(components.worker)
            for _ in range(runtime_settings.AGENT_RUNTIME_COMMAND_CONCURRENCY)
        ]
        command_tasks = [
            asyncio.create_task(
                daemon.run(stop),
                name=f"agent-runtime-command-worker-{slot + 1}",
            )
            for slot, daemon in enumerate(command_daemons)
        ]
        supervisor_task = asyncio.create_task(
            RuntimeCommandDaemonSupervisor(
                list(zip(command_tasks, command_daemons)),
                scan_seconds=runtime_settings.AGENT_RUNTIME_COMMAND_SUPERVISOR_SCAN_SECONDS,
                stall_seconds=runtime_settings.AGENT_RUNTIME_COMMAND_STALL_SECONDS,
                heartbeat_seconds=runtime_settings.AGENT_RUNTIME_COMMAND_HEARTBEAT_SECONDS,
            ).run(stop),
            name="agent-runtime-command-supervisor",
        )
        compact_task = asyncio.create_task(
            components.session_context_scanner.run(stop),
            name="agent-runtime-session-context-compact",
        )
        channel_delivery_task = asyncio.create_task(
            ChannelDeliveryDaemon(
                components.channel_delivery_worker,
                scan_delay_seconds=(
                    runtime_settings.AGENT_RUNTIME_CHANNEL_DELIVERY_SCAN_SECONDS
                ),
            ).run(stop),
            name="agent-runtime-channel-delivery",
        )
        async_tool_poll_task = asyncio.create_task(
            AsyncToolPollDaemon(
                components.async_tool_poll_scheduler,
                scan_delay_seconds=(
                    runtime_settings.AGENT_RUNTIME_ASYNC_TOOL_POLL_SCAN_SECONDS
                ),
            ).run(stop),
            name="agent-runtime-async-tool-poll",
        )
        tool_lease_reconcile_task = asyncio.create_task(
            ToolLeaseReconcileDaemon(
                components.tool_lease_reconciler,
                scan_delay_seconds=(
                    runtime_settings.AGENT_RUNTIME_TOOL_LEASE_RECONCILE_SCAN_SECONDS
                ),
            ).run(stop),
            name="agent-runtime-tool-lease-reconcile",
        )
        product_reconcile_task = asyncio.create_task(
            ProductReconcileDaemon(components.product_reconciler).run(stop),
            name="agent-runtime-product-reconcile",
        )
        tool_result_reconcile_task = asyncio.create_task(
            ToolResultReconcileDaemon(components.tool_result_reconciler).run(stop),
            name="agent-runtime-tool-result-reconcile",
        )
        # Each daemon loops forever until stopped; if one terminates outside its
        # own try/except (construction error, BaseException, etc.) it would
        # otherwise vanish silently and leave the Command Inbox undrained.
        for task in (
            *command_tasks,
            supervisor_task,
            compact_task,
            channel_delivery_task,
            async_tool_poll_task,
            tool_lease_reconcile_task,
            product_reconcile_task,
            tool_result_reconcile_task,
        ):
            task.add_done_callback(_surface_task_crash)
        try:
            yield components
        finally:
            stop.set()
            for task in command_tasks:
                task.cancel()
            supervisor_task.cancel()
            compact_task.cancel()
            channel_delivery_task.cancel()
            async_tool_poll_task.cancel()
            tool_lease_reconcile_task.cancel()
            product_reconcile_task.cancel()
            tool_result_reconcile_task.cancel()
            for task in command_tasks:
                with suppress(asyncio.CancelledError):
                    await task
            with suppress(asyncio.CancelledError):
                await supervisor_task
            with suppress(asyncio.CancelledError):
                await compact_task
            with suppress(asyncio.CancelledError):
                await channel_delivery_task
            with suppress(asyncio.CancelledError):
                await async_tool_poll_task
            with suppress(asyncio.CancelledError):
                await tool_lease_reconcile_task
            with suppress(asyncio.CancelledError):
                await product_reconcile_task
            with suppress(asyncio.CancelledError):
                await tool_result_reconcile_task


__all__ = [
    "ChannelDeliveryDaemon",
    "AsyncToolPollDaemon",
    "ToolLeaseReconcileDaemon",
    "ProductReconcileDaemon",
    "ToolResultReconcileDaemon",
    "RuntimeCommandDaemon",
    "RuntimeCommandDaemonSupervisor",
    "RuntimeSchemaNotReady",
    "RuntimeWorkerComponents",
    "assert_runtime_schema_ready",
    "build_runtime_worker_components",
    "running_runtime_worker_context",
    "runtime_worker_claimant",
    "runtime_worker_context",
]
