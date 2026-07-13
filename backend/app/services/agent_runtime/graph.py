"""Deterministic LangGraph control flow for the durable Agent Runtime."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, cast

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.runtime import Runtime
from langgraph.types import interrupt

from app.config import Settings, get_settings
from app.services.agent_runtime.state import (
    ControlRoute,
    JsonValue,
    RuntimeContext,
    RuntimeGraphState,
    RuntimeNodeName,
    RuntimeStateUpdate,
)


CONTROL_GUARD_NODE = "control_guard"
MODEL_NODE = "model"
TOOL_NODE = "tool"
VERIFY_NODE = "verify"
WAIT_NODE = "wait"
TERMINAL_NODE = "terminal"

_WAITING_STATUSES = frozenset({"waiting_user", "waiting_external", "waiting_agent"})
_TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})
_ROUTE_STATUSES = {
    "model": frozenset({"running"}),
    "tool": frozenset({"running"}),
    "verify": frozenset({"verifying"}),
    "wait": _WAITING_STATUSES,
    "terminal": _TERMINAL_STATUSES,
}


class RuntimeGraphContractError(RuntimeError):
    """Checkpoint state or invocation context violates the graph contract."""


@dataclass(frozen=True, slots=True)
class RuntimeGraphIdentity:
    """Pinned graph identity stored on every Run Registry row."""

    name: str
    version: str

    @property
    def compiled_name(self) -> str:
        return f"{self.name}@{self.version}"

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> "RuntimeGraphIdentity":
        runtime_settings = settings or get_settings()
        return cls(
            name=runtime_settings.AGENT_RUNTIME_GRAPH_NAME,
            version=runtime_settings.AGENT_RUNTIME_GRAPH_VERSION,
        )


@dataclass(frozen=True, slots=True)
class AgentRuntimeGraph:
    """Compiled graph paired with the version needed to resume its checkpoints."""

    identity: RuntimeGraphIdentity
    compiled: CompiledStateGraph


def _require_invocation_scope(
    state: RuntimeGraphState,
    context: RuntimeContext | None,
    identity: RuntimeGraphIdentity,
) -> RuntimeContext:
    if context is None:
        raise RuntimeGraphContractError("RuntimeContext is required")

    registry = state["registry"]
    if registry.tenant_id != context.tenant_id or registry.run_id != context.run_id:
        raise RuntimeGraphContractError("RuntimeContext tenant_id and run_id must match the checkpoint registry")
    if registry.graph_name != identity.name or registry.graph_version != identity.version:
        raise RuntimeGraphContractError("Run graph identity does not match the compiled graph version")
    return context


async def _execute_node(
    node: RuntimeNodeName,
    state: RuntimeGraphState,
    runtime: Runtime[RuntimeContext],
    identity: RuntimeGraphIdentity,
    *,
    resume_value: JsonValue | None = None,
) -> RuntimeStateUpdate:
    context = _require_invocation_scope(state, runtime.context, identity)
    update = await context.executor.execute(
        node,
        state,
        context,
        resume_value=resume_value,
    )
    unexpected_keys = set(update) - {"lifecycle"}
    if unexpected_keys:
        raise RuntimeGraphContractError(
            "Runtime nodes may only update lifecycle state: " + ", ".join(sorted(unexpected_keys))
        )
    if node == "terminal":
        terminal_lifecycle = update.get("lifecycle", state["lifecycle"])
        if (
            terminal_lifecycle.get("status") not in _TERMINAL_STATUSES
            or terminal_lifecycle.get("next_route") != "terminal"
        ):
            raise RuntimeGraphContractError("terminal node must preserve a terminal lifecycle")
    return update


def _make_node(
    node: RuntimeNodeName,
    identity: RuntimeGraphIdentity,
) -> Callable[[RuntimeGraphState, Runtime[RuntimeContext]], Any]:
    async def execute(
        state: RuntimeGraphState,
        runtime: Runtime[RuntimeContext],
    ) -> RuntimeStateUpdate:
        return await _execute_node(node, state, runtime, identity)

    return execute


def _make_wait_node(
    identity: RuntimeGraphIdentity,
) -> Callable[[RuntimeGraphState, Runtime[RuntimeContext]], Any]:
    async def wait_for_resume(
        state: RuntimeGraphState,
        runtime: Runtime[RuntimeContext],
    ) -> RuntimeStateUpdate:
        _require_invocation_scope(state, runtime.context, identity)
        waiting_request = state["lifecycle"].get("waiting_request")
        if not isinstance(waiting_request, dict):
            raise RuntimeGraphContractError("wait route requires a serializable waiting_request")
        resume_value = cast(JsonValue, interrupt(waiting_request))
        return await _execute_node(
            "wait",
            state,
            runtime,
            identity,
            resume_value=resume_value,
        )

    return wait_for_resume


def route_after_control(state: RuntimeGraphState) -> ControlRoute:
    """Route exclusively from authoritative lifecycle values in the checkpoint."""
    lifecycle = state["lifecycle"]
    route = lifecycle.get("next_route")
    status = lifecycle.get("status")
    if route not in _ROUTE_STATUSES:
        raise RuntimeGraphContractError(f"Unsupported control route: {route!r}")
    if status not in _ROUTE_STATUSES[route]:
        raise RuntimeGraphContractError(f"Lifecycle status {status!r} cannot use route {route!r}")
    return cast(ControlRoute, route)


def build_agent_runtime_graph(
    *,
    checkpointer: BaseCheckpointSaver[Any],
    settings: Settings | None = None,
) -> AgentRuntimeGraph:
    """Compile one reusable graph; callers select the pinned version per Run."""
    identity = RuntimeGraphIdentity.from_settings(settings)
    builder = StateGraph(RuntimeGraphState, context_schema=RuntimeContext)

    builder.add_node(CONTROL_GUARD_NODE, _make_node("control_guard", identity))
    builder.add_node(MODEL_NODE, _make_node("model", identity))
    builder.add_node(TOOL_NODE, _make_node("tool", identity))
    builder.add_node(VERIFY_NODE, _make_node("verify", identity))
    builder.add_node(WAIT_NODE, _make_wait_node(identity))
    builder.add_node(TERMINAL_NODE, _make_node("terminal", identity))

    builder.add_edge(START, CONTROL_GUARD_NODE)
    builder.add_conditional_edges(
        CONTROL_GUARD_NODE,
        route_after_control,
        {
            "model": MODEL_NODE,
            "tool": TOOL_NODE,
            "verify": VERIFY_NODE,
            "wait": WAIT_NODE,
            "terminal": TERMINAL_NODE,
        },
    )
    builder.add_edge(MODEL_NODE, CONTROL_GUARD_NODE)
    builder.add_edge(TOOL_NODE, CONTROL_GUARD_NODE)
    builder.add_edge(VERIFY_NODE, CONTROL_GUARD_NODE)
    builder.add_edge(WAIT_NODE, CONTROL_GUARD_NODE)
    builder.add_edge(TERMINAL_NODE, END)

    compiled = builder.compile(
        checkpointer=checkpointer,
        name=identity.compiled_name,
    )
    return AgentRuntimeGraph(identity=identity, compiled=compiled)
