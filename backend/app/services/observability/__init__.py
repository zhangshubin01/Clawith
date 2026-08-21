"""Clawith observability facade (trace-level LLM / agent tracing).

Call sites import from here and never touch the ``langfuse`` SDK directly.
"""

from app.services.observability.tracing import (
    GenerationHandle,
    flush,
    is_enabled,
    mask_text,
    observe_generation,
    set_run_identity,
)

__all__ = [
    "GenerationHandle",
    "flush",
    "is_enabled",
    "mask_text",
    "observe_generation",
    "set_run_identity",
]
