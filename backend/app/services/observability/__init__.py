"""Clawith observability facade (trace-level LLM / agent tracing).

Call sites import from here and never touch the ``langfuse`` SDK directly.
"""

from app.services.observability.tracing import (
    GenerationHandle,
    RunHandle,
    current_observation_id,
    current_trace_id,
    flush,
    is_enabled,
    mask_text,
    observe_generation,
    observe_node,
    observe_run,
    observe_tool,
    set_run_identity,
)

__all__ = [
    "GenerationHandle",
    "RunHandle",
    "current_observation_id",
    "current_trace_id",
    "flush",
    "is_enabled",
    "mask_text",
    "observe_generation",
    "observe_node",
    "observe_run",
    "observe_tool",
    "set_run_identity",
]
