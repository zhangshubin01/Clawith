"""In-memory IDE tool registry for /ws/chat WebSocket-proxied tools.

Replaces the prior ``ws-proxy://`` DB-based approach. IDE tools are
inherently connection-scoped — when the WebSocket disconnects they must
disappear. Storing them in memory (like ACP's ContextVar pattern) avoids
orphan DB rows on crash and eliminates the pseudo-URL overloading of the
``mcp_server_url`` column.

Lifecycle:
    IDE connects → register(agent_id, tools, handler)
    IDE disconnects → unregister(agent_id)

The WebSocket message protocol (mcp_register / mcp_tool_call / mcp_tool_result)
is unchanged.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# ── Per-agent in-memory stores ──────────────────────────────────────
# agent_id (str) → list of OpenAI function-calling tool schemas
_tools_by_agent: dict[str, list[dict]] = {}

# agent_id (str) → {"handler": WebSocketChatHandler, "project_path": str}
_handlers_by_agent: dict[str, dict[str, Any]] = {}


def register(
    agent_id: str,
    tools: list[dict],
    handler: Any,
    project_path: str = "",
) -> None:
    """Register IDE tools for an agent, replacing any previous registration.

    Called from ``WebSocketChatHandler._register_local_mcp_tools`` when the
    IDE plugin sends an ``mcp_register`` message.
    """
    if not tools:
        unregister(agent_id)
        return

    _tools_by_agent[agent_id] = tools
    _handlers_by_agent[agent_id] = {
        "handler": handler,
        "project_path": project_path,
    }
    logger.info(
        "[IdeToolRegistry] Registered %d IDE tools for agent=%s project=%s",
        len(tools),
        agent_id,
        project_path or "-",
    )


def unregister(agent_id: str) -> None:
    """Remove all IDE tools for an agent (called on WebSocket disconnect)."""
    removed = _tools_by_agent.pop(agent_id, None)
    _handlers_by_agent.pop(agent_id, None)
    if removed is not None:
        logger.info(
            "[IdeToolRegistry] Unregistered %d IDE tools for agent=%s",
            len(removed),
            agent_id,
        )


def get_tool_schemas(agent_id: str) -> list[dict]:
    """Return the OpenAI function-calling tool schemas registered for an agent.

    Used by ``get_agent_tools_for_llm`` to inject IDE tools into the LLM
    tool list at runtime (without touching the DB).
    """
    return _tools_by_agent.get(agent_id, [])


def get_tool_names(agent_id: str) -> frozenset[str]:
    """Return the set of tool names registered for an agent.

    Used to detect IDE-mode and hide overlapping server built-in tools.
    """
    tools = _tools_by_agent.get(agent_id, [])
    if not tools:
        return frozenset()
    return frozenset(
        t.get("function", {}).get("name", "") for t in tools if t.get("function", {}).get("name")
    )


def is_ide_tool(agent_id: str, tool_name: str) -> bool:
    """Check whether *tool_name* is an IDE-proxied tool for this agent."""
    return tool_name in get_tool_names(agent_id)


def find_handler(agent_id: str) -> Any | None:
    """Return the WebSocket handler for this agent's IDE connection, or None."""
    entry = _handlers_by_agent.get(agent_id)
    if entry is None:
        return None
    return entry["handler"]


def has_active_connection(agent_id: str) -> bool:
    """Return True if there is an active IDE WebSocket connection for this agent."""
    return agent_id in _handlers_by_agent
