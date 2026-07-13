"""Caller-transaction intake and product queries for the durable Runtime."""

from __future__ import annotations

from typing import cast
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.models.agent_run import AgentRun
from app.models.agent_run_command import AgentRunCommand
from app.services.agent_runtime.config import RuntimeGateDecision, RuntimeRolloutPolicy
from app.services.agent_runtime.contracts import (
    CancelRunCommand,
    DeliveryStatus,
    ResumeRunCommand,
    RunHandle,
    RunKind,
    RuntimeSourceType,
    RuntimeType,
    RunView,
    StartRunCommand,
)
from app.services.agent_runtime.graph import RuntimeGraphIdentity
from app.services.agent_runtime.persistence import (
    RunRegistration,
    enqueue_cancel,
    enqueue_resume,
    register_run_with_start,
)
from app.services.agent_runtime.state import LifecycleStatus


class RuntimeAdapterError(RuntimeError):
    """A Runtime command cannot be accepted through the v2 adapter."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class TransactionalAgentRuntimeAdapter:
    """Persist Runtime inputs inside an AsyncSession owned by the caller.

    This layer accepts commands and returns stable identities. It never commits,
    invokes a Graph, or passes an ``AgentRun`` ORM instance into execution code.
    """

    def __init__(
        self,
        db: AsyncSession,
        *,
        settings: Settings | None = None,
    ) -> None:
        runtime_settings = settings or get_settings()
        self._db = db
        self._rollout = RuntimeRolloutPolicy.from_settings(runtime_settings)
        self._current_graph = RuntimeGraphIdentity.from_settings(runtime_settings)

    @staticmethod
    def _require_v2(decision: RuntimeGateDecision) -> None:
        if not decision.use_v2:
            raise RuntimeAdapterError(
                "runtime_v2_disabled",
                f"Agent Runtime v2 is not enabled for this command ({decision.reason})",
            )

    async def _find_start_retry(self, command: StartRunCommand) -> AgentRun | None:
        if command.source_execution_id is None:
            return None
        result = await self._db.execute(
            select(AgentRun).where(
                AgentRun.tenant_id == command.tenant_id,
                AgentRun.source_type == command.source_type,
                AgentRun.source_execution_id == command.source_execution_id,
            )
        )
        return result.scalar_one_or_none()

    async def _get_run(self, *, tenant_id: uuid.UUID, run_id: uuid.UUID) -> AgentRun:
        result = await self._db.execute(
            select(AgentRun).where(
                AgentRun.tenant_id == tenant_id,
                AgentRun.id == run_id,
            )
        )
        run = result.scalar_one_or_none()
        if run is None:
            raise RuntimeAdapterError(
                "run_not_found",
                f"run {run_id} does not exist in tenant {tenant_id}",
            )
        if run.tenant_id != tenant_id:
            raise RuntimeAdapterError("run_scope_mismatch", "loaded Run is outside the requested tenant")
        return run

    def _require_existing_v2(self, run: AgentRun) -> None:
        decision = self._rollout.decide(
            agent_id=run.agent_id,
            source_type=run.source_type,
            existing_runtime_type=run.runtime_type,
        )
        self._require_v2(decision)
        self._require_run_identity(run)

    @staticmethod
    def _require_run_identity(run: AgentRun) -> None:
        if run.runtime_type != "langgraph":
            raise RuntimeAdapterError(
                "runtime_type_mismatch",
                "v2 adapter may only return handles for LangGraph Runs",
            )
        if run.runtime_thread_id != str(run.id):
            raise RuntimeAdapterError(
                "runtime_identity_mismatch",
                "Run thread_id must equal run_id",
            )

    @staticmethod
    def _handle(
        run: AgentRun,
        command: AgentRunCommand,
        *,
        created: bool,
    ) -> RunHandle:
        TransactionalAgentRuntimeAdapter._require_run_identity(run)
        if command.tenant_id != run.tenant_id or command.run_id != run.id:
            raise RuntimeAdapterError(
                "command_scope_mismatch",
                "accepted command does not belong to the returned Run",
            )
        return RunHandle(
            tenant_id=run.tenant_id,
            run_id=run.id,
            thread_id=run.runtime_thread_id,
            command_id=command.id,
            runtime_type="langgraph",
            created=created,
        )

    async def start_run(self, command: StartRunCommand) -> RunHandle:
        """Atomically register one Run and its start command without committing."""
        existing = await self._find_start_retry(command)
        if existing is None:
            decision = self._rollout.decide(
                agent_id=command.agent_id,
                source_type=command.source_type,
            )
            runtime_type = "langgraph"
            graph_identity = self._current_graph
        else:
            decision = self._rollout.decide(
                agent_id=existing.agent_id,
                source_type=existing.source_type,
                existing_runtime_type=existing.runtime_type,
            )
            runtime_type = existing.runtime_type
            graph_identity = RuntimeGraphIdentity(
                name=existing.graph_name,
                version=existing.graph_version,
            )
        self._require_v2(decision)
        if existing is not None:
            self._require_run_identity(existing)

        registered = await register_run_with_start(
            self._db,
            RunRegistration(
                tenant_id=command.tenant_id,
                agent_id=command.agent_id,
                session_id=command.session_id,
                source_type=command.source_type,
                source_id=command.source_id,
                source_execution_id=command.source_execution_id,
                correlation_id=command.correlation_id,
                origin_user_id=command.origin_user_id,
                origin_agent_id=command.origin_agent_id,
                parent_run_id=command.parent_run_id,
                root_run_id=command.root_run_id,
                goal=command.goal,
                run_kind=command.run_kind,
                system_role=command.system_role,
                model_id=command.model_id,
                runtime_type=runtime_type,
                graph_name=graph_identity.name,
                graph_version=graph_identity.version,
                scheduling_lane_key=command.scheduling_lane_key,
                scheduling_position_created_at=command.scheduling_position_created_at,
                scheduling_position_id=command.scheduling_position_id,
                delivery_status=command.delivery_status,
                delivery_target=command.delivery_target,
            ),
            start_payload=command.payload,
            start_idempotency_key=command.idempotency_key,
            actor_user_id=command.actor_user_id,
            actor_agent_id=command.actor_agent_id,
        )
        return self._handle(
            registered.run,
            registered.start_command,
            created=registered.created,
        )

    async def resume_run(self, command: ResumeRunCommand) -> RunHandle:
        """Persist a resume for an existing LangGraph Run without committing."""
        run = await self._get_run(tenant_id=command.tenant_id, run_id=command.run_id)
        self._require_existing_v2(run)
        enqueued = await enqueue_resume(
            self._db,
            tenant_id=command.tenant_id,
            run_id=command.run_id,
            payload=command.payload,
            idempotency_key=command.idempotency_key,
            actor_user_id=command.actor_user_id,
            actor_agent_id=command.actor_agent_id,
        )
        return self._handle(run, enqueued.command, created=enqueued.created)

    async def cancel_run(self, command: CancelRunCommand) -> RunHandle:
        """Persist cooperative cancellation without committing or mutating projections."""
        run = await self._get_run(tenant_id=command.tenant_id, run_id=command.run_id)
        self._require_existing_v2(run)
        enqueued = await enqueue_cancel(
            self._db,
            tenant_id=command.tenant_id,
            run_id=command.run_id,
            idempotency_key=command.idempotency_key,
            reason=command.reason,
            actor_user_id=command.actor_user_id,
            actor_agent_id=command.actor_agent_id,
        )
        return self._handle(run, enqueued.command, created=enqueued.created)

    async def get_run_state(self, tenant_id: uuid.UUID, run_id: uuid.UUID) -> RunView:
        """Return the product query projection without treating it as execution state."""
        run = await self._get_run(tenant_id=tenant_id, run_id=run_id)
        return RunView(
            tenant_id=run.tenant_id,
            run_id=run.id,
            source_type=cast(RuntimeSourceType, run.source_type),
            run_kind=cast(RunKind, run.run_kind),
            goal=run.goal,
            runtime_type=cast(RuntimeType, run.runtime_type),
            execution_status=cast(LifecycleStatus | None, run.projected_execution_status),
            waiting_type=run.projected_waiting_type,
            result_summary=run.projected_result_summary,
            error_code=run.projected_error_code,
            last_error=run.projected_last_error,
            delivery_status=cast(DeliveryStatus, run.delivery_status),
            projection_checkpoint_id=run.projected_checkpoint_id,
            projection_updated_at=run.projection_updated_at,
            created_at=run.created_at,
            updated_at=run.updated_at,
        )
