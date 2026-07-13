"""Stable product-facing contracts for the durable Agent Runtime."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Protocol
import uuid

from app.services.agent_runtime.state import JsonObject, LifecycleStatus


RuntimeSourceType = Literal["chat", "trigger", "task", "a2a", "heartbeat"]
RunKind = Literal["foreground", "background", "delegated", "orchestration"]
RuntimeType = Literal["legacy", "langgraph"]
DeliveryStatus = Literal["not_required", "pending", "delivered", "failed"]
RuntimeEventType = Literal[
    "run_created",
    "status_changed",
    "waiting_started",
    "resumed",
    "evidence_added",
    "verification_updated",
    "run_completed",
    "run_failed",
    "run_cancelled",
    "delivery_succeeded",
    "delivery_failed",
]


@dataclass(frozen=True, slots=True)
class StartRunCommand:
    """One product input that must create exactly one Run and start command."""

    tenant_id: uuid.UUID
    source_type: RuntimeSourceType
    goal: str
    run_kind: RunKind
    idempotency_key: str
    payload: JsonObject = field(default_factory=dict)
    agent_id: uuid.UUID | None = None
    session_id: uuid.UUID | None = None
    source_id: str | None = None
    source_execution_id: str | None = None
    correlation_id: str | None = None
    origin_user_id: uuid.UUID | None = None
    origin_agent_id: uuid.UUID | None = None
    parent_run_id: uuid.UUID | None = None
    root_run_id: uuid.UUID | None = None
    system_role: str | None = None
    model_id: uuid.UUID | None = None
    scheduling_lane_key: str | None = None
    scheduling_position_created_at: datetime | None = None
    scheduling_position_id: uuid.UUID | None = None
    delivery_status: DeliveryStatus = "not_required"
    delivery_target: JsonObject | None = None
    actor_user_id: uuid.UUID | None = None
    actor_agent_id: uuid.UUID | None = None


@dataclass(frozen=True, slots=True)
class ResumeRunCommand:
    """An explicit input for an existing Run thread."""

    tenant_id: uuid.UUID
    run_id: uuid.UUID
    idempotency_key: str
    payload: JsonObject
    actor_user_id: uuid.UUID | None = None
    actor_agent_id: uuid.UUID | None = None


@dataclass(frozen=True, slots=True)
class CancelRunCommand:
    """A cooperative cancellation request for an existing Run thread."""

    tenant_id: uuid.UUID
    run_id: uuid.UUID
    idempotency_key: str
    reason: str | None = None
    actor_user_id: uuid.UUID | None = None
    actor_agent_id: uuid.UUID | None = None


@dataclass(frozen=True, slots=True)
class RunHandle:
    """Stable identity returned after a Runtime command is durably accepted."""

    tenant_id: uuid.UUID
    run_id: uuid.UUID
    thread_id: str
    command_id: uuid.UUID
    runtime_type: RuntimeType
    created: bool


@dataclass(frozen=True, slots=True)
class RunView:
    """Product query view; never an input to Graph routing or recovery."""

    tenant_id: uuid.UUID
    run_id: uuid.UUID
    source_type: RuntimeSourceType
    run_kind: RunKind
    goal: str
    runtime_type: RuntimeType
    execution_status: LifecycleStatus | None
    waiting_type: str | None
    result_summary: str | None
    error_code: str | None
    last_error: str | None
    delivery_status: DeliveryStatus
    projection_checkpoint_id: str | None
    projection_updated_at: datetime | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class RuntimeEvent:
    """Stable product event emitted independently of checkpoint internals."""

    tenant_id: uuid.UUID
    run_id: uuid.UUID
    event_type: RuntimeEventType
    payload: JsonObject = field(default_factory=dict)
    checkpoint_id: str | None = None
    created_at: datetime | None = None


class AgentRuntimeAdapter(Protocol):
    """The only Runtime surface available to channels and product services."""

    async def start_run(self, command: StartRunCommand) -> RunHandle: ...

    async def resume_run(self, command: ResumeRunCommand) -> RunHandle: ...

    async def cancel_run(self, command: CancelRunCommand) -> RunHandle: ...

    async def get_run_state(self, tenant_id: uuid.UUID, run_id: uuid.UUID) -> RunView: ...

    def stream_run(self, handle: RunHandle) -> AsyncIterator[RuntimeEvent]: ...
