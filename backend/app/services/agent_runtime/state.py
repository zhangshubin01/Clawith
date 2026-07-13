"""Serializable state and transient context contracts for Agent Runtime graphs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, NotRequired, Protocol, TypeAlias, TypedDict


JsonScalar: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]
JsonObject: TypeAlias = dict[str, JsonValue]

LifecycleStatus: TypeAlias = Literal[
    "created",
    "queued",
    "running",
    "waiting_user",
    "waiting_external",
    "waiting_agent",
    "verifying",
    "completed",
    "failed",
    "cancelled",
]
ControlRoute: TypeAlias = Literal["model", "tool", "verify", "wait", "terminal"]
RuntimeNodeName: TypeAlias = Literal[
    "control_guard",
    "model",
    "tool",
    "verify",
    "wait",
    "terminal",
]


@dataclass(frozen=True, slots=True)
class RunRegistrySnapshot:
    """Immutable product-owned Run facts copied into the first checkpoint."""

    tenant_id: str
    run_id: str
    goal: str
    run_kind: str
    source_type: str
    model_id: str
    graph_name: str
    graph_version: str
    agent_id: str | None = None
    session_id: str | None = None
    system_role: str | None = None
    parent_run_id: str | None = None
    root_run_id: str | None = None


@dataclass(frozen=True, slots=True)
class RunInputSnapshots:
    """Versioned inputs fixed when a Run starts and reused when it resumes."""

    session_context: JsonObject
    session_context_version: int
    recent_session_messages: tuple[JsonObject, ...]
    related_run_summaries: tuple[JsonObject, ...]
    initial_input: JsonObject


class RuntimeLifecycle(TypedDict):
    """Authoritative, checkpointed lifecycle and resumable execution data."""

    status: LifecycleStatus
    next_route: ControlRoute
    reason: NotRequired[str | None]
    last_applied_command_ids: NotRequired[list[str]]
    run_messages: NotRequired[list[JsonObject]]
    run_summary: NotRequired[JsonObject | None]
    covered_through_run_message_id: NotRequired[str | None]
    pending_tool_calls: NotRequired[list[JsonObject]]
    waiting_request: NotRequired[JsonObject | None]
    verification_result: NotRequired[JsonObject | None]
    final_answer: NotRequired[str | None]
    result_summary: NotRequired[JsonObject | None]
    session_context_delta: NotRequired[JsonObject | None]
    delivery_request: NotRequired[JsonObject | None]
    error: NotRequired[JsonObject | None]


class RuntimeGraphState(TypedDict):
    """Checkpoint payload with immutable inputs isolated from mutable lifecycle."""

    registry: RunRegistrySnapshot
    snapshots: RunInputSnapshots
    lifecycle: RuntimeLifecycle


class RuntimeStateUpdate(TypedDict, total=False):
    """Node updates are intentionally limited to checkpoint lifecycle state."""

    lifecycle: RuntimeLifecycle


class RuntimeNodeExecutor(Protocol):
    """Application services behind deterministic graph nodes."""

    async def execute(
        self,
        node: RuntimeNodeName,
        state: RuntimeGraphState,
        context: "RuntimeContext",
        *,
        resume_value: JsonValue | None = None,
    ) -> RuntimeStateUpdate: ...


@dataclass(frozen=True, slots=True)
class RuntimeContext:
    """Per-invocation dependencies and authorization scope, never checkpointed."""

    tenant_id: str
    run_id: str
    command_id: str
    executor: RuntimeNodeExecutor
    actor_user_id: str | None = None
    actor_agent_id: str | None = None
