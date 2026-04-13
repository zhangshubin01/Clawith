"""Tool call handler for IDEA plugin integration."""

import asyncio
from typing import Dict, Any

# Store pending tool calls: { tool_call_id: asyncio.Event }
_pending_tool_calls: Dict[str, asyncio.Event] = {}
# Store results: { tool_call_id: result }
_tool_call_results: Dict[str, Any] = {}


async def wait_for_ide_tool_result(tool_call_id: str, timeout: float = 30.0) -> Any:
    """Wait for the IDEA plugin to return a tool result."""
    event = asyncio.Event()
    _pending_tool_calls[tool_call_id] = event
    
    try:
        await asyncio.wait_for(event.wait(), timeout=timeout)
        return _tool_call_results.pop(tool_call_id, None)
    except asyncio.TimeoutError:
        return f"[Error] Tool call {tool_call_id} timed out after {timeout}s"
    finally:
        _pending_tool_calls.pop(tool_call_id, None)


def resolve_ide_tool_result(tool_call_id: str, result: Any):
    """Resolve a pending tool call with the result from the IDEA plugin."""
    _tool_call_results[tool_call_id] = result
    if tool_call_id in _pending_tool_calls:
        _pending_tool_calls[tool_call_id].set()
