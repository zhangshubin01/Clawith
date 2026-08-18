"""Short-TTL cache for the Durable Runtime tool workset.

P2 hardening: `get_runtime_agent_tools_for_llm` re-resolves the full tool
catalog (DB tools + assignments + dynamic MCP names + per-tool readiness
checks) on every model step and every tool step.  A single conversation turn
therefore re-parses the same ~60 tool schemas several times and floods the
log with per-tool "hid ..." lines.

This module wraps that resolver in an in-process TTL cache with per-key
in-flight deduplication.  The resolver itself stays uncached so direct
callers (and the typed-outcome test contracts) keep their freshness
semantics; the runtime services consume the cached wrapper as their default
``tool_provider`` and the tool write paths invalidate it explicitly.

Production runs uvicorn with a single worker, so an in-process cache is
fully effective there.  With several workers each worker holds its own
cache; the TTL bounds staleness in that case.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections import OrderedDict
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

# Freshness window. Longer than one model/tool round so a conversation turn
# resolves tools once instead of per step; short enough that configuration
# written without explicit invalidation (e.g. channel settings) propagates
# within this window.
TOOL_RESOLUTION_TTL_SECONDS = 30.0

# Upper bound on cached agents to keep memory flat for long-lived workers.
_MAX_ENTRIES = 1024

_entries: "OrderedDict[uuid.UUID, tuple[float, Any]]" = OrderedDict()
_inflight: dict[uuid.UUID, "asyncio.Task[Any]"] = {}

_Resolver = Callable[[uuid.UUID], Awaitable[Any]]


def _resolver() -> _Resolver:
    # Lazy import: agent_tools pulls in heavy modules and this module is
    # imported by low-level services.
    from app.services import agent_tools

    return agent_tools.get_runtime_agent_tools_for_llm


def _store(agent_id: uuid.UUID, value: Any) -> None:
    _entries[agent_id] = (time.monotonic() + TOOL_RESOLUTION_TTL_SECONDS, value)
    _entries.move_to_end(agent_id)
    while len(_entries) > _MAX_ENTRIES:
        _entries.popitem(last=False)


async def cached_runtime_agent_tools(agent_id: uuid.UUID) -> list[dict]:
    """Return the runtime tool workset, reusing a fresh cached resolution."""
    now = time.monotonic()
    hit = _entries.get(agent_id)
    if hit is not None:
        expires_at, value = hit
        if now < expires_at:
            _entries.move_to_end(agent_id)
            return value
        del _entries[agent_id]

    task = _inflight.get(agent_id)
    if task is None:
        logger.debug("[Tools] resolving runtime tool workset (cache miss) agent=%s", agent_id)
        task = asyncio.ensure_future(_resolver()(agent_id))
        _inflight[agent_id] = task

    try:
        value = await task
    finally:
        if _inflight.get(agent_id) is task:
            _inflight.pop(agent_id, None)

    # Failures are never cached: the next step retries resolution.
    _store(agent_id, value)
    return value


def invalidate_agent_tool_resolution(agent_id: uuid.UUID) -> None:
    """Drop the cached workset for one agent (assignment/config writes)."""
    _entries.pop(agent_id, None)


def invalidate_all_tool_resolutions() -> None:
    """Drop every cached workset (tenant/global writes that may hit any agent)."""
    _entries.clear()


def clear_tool_resolution_cache() -> None:
    """Test hook — alias of invalidate_all_tool_resolutions."""
    invalidate_all_tool_resolutions()
