"""Concrete LangGraph driver for Runtime Command Worker inputs."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import cast
import uuid

from langgraph.types import Command
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from app.services.agent_runtime.checkpointer import runtime_thread_config
from app.services.agent_runtime.command_worker import (
    CheckpointObservation,
    CommandExecutionRejected,
    RetryableCommandError,
    RuntimeCommandRecord,
    RuntimeRunRecord,
)
from app.services.agent_runtime.context_builder import ContextBuilder
from app.services.agent_runtime.graph import (
    CONTROL_GUARD_NODE,
    AgentRuntimeGraph,
)
from app.services.agent_runtime.state import (
    JsonObject,
    RunInputSnapshots,
    RuntimeContext,
    RuntimeGraphState,
    RuntimeNodeExecutor,
)


_TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})
_WAITING_RESUME_TYPES = {
    "waiting_user": frozenset({"user_input"}),
    "waiting_agent": frozenset({"agent_result"}),
    "waiting_external": frozenset({"external_event", "timer"}),
}
_MAX_APPLIED_COMMAND_IDS = 64


class RuntimeGraphRegistry:
    """Resolve only explicitly installed, version-pinned Runtime graphs."""

    def __init__(self, graphs: Sequence[AgentRuntimeGraph]) -> None:
        resolved: dict[tuple[str, str], AgentRuntimeGraph] = {}
        for graph in graphs:
            key = (graph.identity.name, graph.identity.version)
            if key in resolved:
                raise ValueError(f"duplicate Runtime graph identity {key[0]}@{key[1]}")
            resolved[key] = graph
        if not resolved:
            raise ValueError("at least one Runtime graph must be installed")
        self._graphs = resolved

    def resolve(self, run: RuntimeRunRecord) -> AgentRuntimeGraph:
        key = (run.registry.graph_name, run.registry.graph_version)
        graph = self._graphs.get(key)
        if graph is None:
            raise RetryableCommandError(
                "graph_version_unavailable",
                f"Runtime graph {key[0]}@{key[1]} is not installed",
            )
        return graph


class RuntimeInputSnapshotFactory:
    """Capture immutable new-Run inputs on the advisory-lock connection."""

    def __init__(self, context_builder: ContextBuilder) -> None:
        self._context_builder = context_builder

    async def capture(
        self,
        *,
        connection: AsyncConnection,
        run: RuntimeRunRecord,
        command: RuntimeCommandRecord,
    ) -> RunInputSnapshots:
        if command.command_type != "start":
            raise ValueError("Runtime input snapshots can only be captured for start")
        session_id = uuid.UUID(run.registry.session_id) if run.registry.session_id is not None else None
        related = command.payload.get("related_run_summaries", [])
        if not isinstance(related, Sequence) or isinstance(related, (str, bytes, bytearray)):
            raise CommandExecutionRejected(
                "invalid_related_run_summaries",
                "related_run_summaries must be an array",
            )
        if any(not isinstance(summary, Mapping) for summary in related):
            raise CommandExecutionRejected(
                "invalid_related_run_summaries",
                "each related Run summary must be an object",
            )
        async with AsyncSession(bind=connection, expire_on_commit=False) as db:
            return await self._context_builder.capture_run_inputs(
                db,
                tenant_id=run.tenant_id,
                session_id=session_id,
                initial_input=command.payload,
                related_run_summaries=cast(Sequence[Mapping[str, object]], related),
            )


@dataclass(frozen=True, slots=True)
class StaticRuntimeInputSnapshotFactory:
    """A concrete factory for callers that already captured trusted snapshots."""

    snapshots: RunInputSnapshots

    async def capture(
        self,
        *,
        connection: AsyncConnection,
        run: RuntimeRunRecord,
        command: RuntimeCommandRecord,
    ) -> RunInputSnapshots:
        del connection, run
        if command.command_type != "start":
            raise ValueError("Runtime input snapshots can only be captured for start")
        return self.snapshots


def _checkpoint_id(snapshot: object) -> str:
    config = getattr(snapshot, "config", None)
    if not isinstance(config, dict):
        raise RetryableCommandError(
            "invalid_checkpoint_config",
            "LangGraph snapshot has no checkpoint configuration",
        )
    configurable = config.get("configurable")
    if not isinstance(configurable, dict):
        raise RetryableCommandError(
            "invalid_checkpoint_config",
            "LangGraph snapshot has no configurable checkpoint identity",
        )
    checkpoint_id = configurable.get("checkpoint_id")
    if not isinstance(checkpoint_id, str) or not checkpoint_id:
        raise RetryableCommandError(
            "invalid_checkpoint_id",
            "LangGraph snapshot has no checkpoint ID",
        )
    return checkpoint_id


def _require_scope(run: RuntimeRunRecord, command: RuntimeCommandRecord) -> None:
    if command.tenant_id != run.tenant_id or command.run_id != run.run_id:
        raise CommandExecutionRejected(
            "command_scope_mismatch",
            "Runtime command does not belong to the locked Run",
        )
    if run.thread_id != str(run.run_id):
        raise RetryableCommandError(
            "runtime_identity_mismatch",
            "Runtime thread_id must equal run_id",
        )


def _runtime_context(
    run: RuntimeRunRecord,
    command: RuntimeCommandRecord,
    executor: RuntimeNodeExecutor,
) -> RuntimeContext:
    return RuntimeContext(
        tenant_id=str(run.tenant_id),
        run_id=str(run.run_id),
        command_id=str(command.id),
        executor=executor,
        actor_user_id=(str(command.actor_user_id) if command.actor_user_id is not None else None),
        actor_agent_id=(str(command.actor_agent_id) if command.actor_agent_id is not None else None),
    )


def _resume_value(checkpoint: CheckpointObservation, command: RuntimeCommandRecord) -> JsonObject:
    lifecycle = checkpoint.state["lifecycle"]
    status = lifecycle["status"]
    allowed_resume_types = _WAITING_RESUME_TYPES.get(status)
    if allowed_resume_types is None:
        raise CommandExecutionRejected(
            "run_not_waiting",
            "resume requires a waiting checkpoint",
        )

    resume_type = command.payload.get("resume_type")
    correlation_id = command.payload.get("correlation_id")
    payload = command.payload.get("payload")
    if resume_type not in allowed_resume_types:
        raise CommandExecutionRejected(
            "resume_type_mismatch",
            "resume type does not match the checkpoint waiting type",
        )
    if not isinstance(correlation_id, str) or not correlation_id:
        raise CommandExecutionRejected(
            "invalid_resume_correlation",
            "resume correlation_id must be a non-empty string",
        )
    if not isinstance(payload, dict):
        raise CommandExecutionRejected(
            "invalid_resume_payload",
            "resume payload must be an object",
        )
    waiting_request = lifecycle.get("waiting_request")
    if not isinstance(waiting_request, dict):
        raise RetryableCommandError(
            "invalid_waiting_checkpoint",
            "waiting checkpoint has no waiting request",
        )
    expected_correlation = waiting_request.get("correlation_id")
    if expected_correlation != correlation_id:
        raise CommandExecutionRejected(
            "resume_correlation_mismatch",
            "resume correlation_id does not match the waiting checkpoint",
        )
    return dict(command.payload)


def _cancelled_lifecycle(
    checkpoint: CheckpointObservation,
    command: RuntimeCommandRecord,
) -> dict[str, object]:
    lifecycle = dict(checkpoint.state["lifecycle"])
    command_ids = lifecycle.get("last_applied_command_ids", [])
    if not isinstance(command_ids, list) or any(
        not isinstance(command_id, str) or not command_id for command_id in command_ids
    ):
        raise RetryableCommandError(
            "invalid_checkpoint_command_ids",
            "checkpoint command reconciliation IDs are malformed",
        )
    current_command_id = str(command.id)
    command_ids = [command_id for command_id in command_ids if command_id != current_command_id]
    command_ids.append(current_command_id)
    reason = command.payload.get("reason")
    if reason is not None and not isinstance(reason, str):
        raise CommandExecutionRejected(
            "invalid_cancel_reason",
            "cancel reason must be a string when provided",
        )
    lifecycle.update(
        {
            "status": "cancelled",
            "next_route": "terminal",
            "reason": reason or "cancelled_by_command",
            "last_applied_command_ids": command_ids[-_MAX_APPLIED_COMMAND_IDS:],
            "waiting_request": None,
            "pending_tool_calls": [],
        }
    )
    return lifecycle


class LangGraphRuntimeDriver:
    """Read and advance only the graph version pinned by the Run Registry."""

    def __init__(
        self,
        *,
        graph_registry: RuntimeGraphRegistry,
        snapshot_factory: RuntimeInputSnapshotFactory | StaticRuntimeInputSnapshotFactory,
        node_executor: RuntimeNodeExecutor,
    ) -> None:
        self._graph_registry = graph_registry
        self._snapshot_factory = snapshot_factory
        self._node_executor = node_executor

    async def read_latest(
        self,
        *,
        connection: AsyncConnection,
        run: RuntimeRunRecord,
    ) -> CheckpointObservation | None:
        del connection
        if run.thread_id != str(run.run_id):
            raise RetryableCommandError(
                "runtime_identity_mismatch",
                "Runtime thread_id must equal run_id",
            )
        graph = self._graph_registry.resolve(run)
        snapshot = await graph.compiled.aget_state(runtime_thread_config(run.run_id))
        values = snapshot.values
        if not values:
            return None
        if not isinstance(values, dict):
            raise RetryableCommandError(
                "invalid_checkpoint_state",
                "LangGraph checkpoint values must be an object",
            )
        state = cast(RuntimeGraphState, dict(values))
        return CheckpointObservation(
            checkpoint_id=_checkpoint_id(snapshot),
            state=state,
        )

    async def execute(
        self,
        *,
        connection: AsyncConnection,
        run: RuntimeRunRecord,
        command: RuntimeCommandRecord,
        checkpoint: CheckpointObservation | None,
    ) -> None:
        _require_scope(run, command)
        graph = self._graph_registry.resolve(run)
        config = runtime_thread_config(run.run_id)
        context = _runtime_context(run, command, self._node_executor)

        if command.command_type == "start":
            if checkpoint is not None:
                raise RetryableCommandError(
                    "start_checkpoint_conflict",
                    "start cannot replace an existing checkpoint",
                )
            snapshots = await self._snapshot_factory.capture(
                connection=connection,
                run=run,
                command=command,
            )
            initial_state: RuntimeGraphState = {
                "registry": run.registry,
                "snapshots": snapshots,
                "lifecycle": {
                    "status": "running",
                    "next_route": "model",
                    "last_applied_command_ids": [str(command.id)],
                    "run_messages": [],
                    "pending_tool_calls": [],
                },
            }
            await graph.compiled.ainvoke(
                initial_state,
                config,
                context=context,
                durability="sync",
            )
            return

        if checkpoint is None:
            raise CommandExecutionRejected(
                "thread_not_started",
                "resume and cancel require an existing checkpoint",
            )
        status = checkpoint.state["lifecycle"]["status"]
        if status in _TERMINAL_STATUSES:
            raise CommandExecutionRejected(
                "terminal_run",
                "terminal Runtime threads cannot accept new commands",
            )

        if command.command_type == "resume":
            resume_value = _resume_value(checkpoint, command)
            await graph.compiled.ainvoke(
                Command(resume=resume_value),
                config,
                context=context,
                durability="sync",
            )
            return

        if command.command_type == "cancel":
            await graph.compiled.aupdate_state(
                config,
                {"lifecycle": _cancelled_lifecycle(checkpoint, command)},
                as_node=CONTROL_GUARD_NODE,
            )
            await graph.compiled.ainvoke(
                None,
                config,
                context=context,
                durability="sync",
            )
            return

        raise CommandExecutionRejected(
            "unsupported_command",
            f"unsupported Runtime command {command.command_type!r}",
        )


__all__ = [
    "LangGraphRuntimeDriver",
    "RuntimeGraphRegistry",
    "RuntimeInputSnapshotFactory",
    "StaticRuntimeInputSnapshotFactory",
]
