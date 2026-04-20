"""Tool call handler for IDEA plugin integration."""

import asyncio
from typing import Dict, Any
from loguru import logger

# Store pending tool calls: { tool_call_id: asyncio.Event }
_pending_tool_calls: Dict[str, asyncio.Event] = {}
# Store results: { tool_call_id: result }
_tool_call_results: Dict[str, Any] = {}

async def send_ide_tool_request(agent_id_str: str, request: dict):
    """Send a tool call request to the connected IDE plugin."""
    from app.plugins.clawith_ide_bridge.router import _active_ide_connections
    # For now, we broadcast to all connected IDEs or find by agent_id if passed in request
    # In a real scenario, we might map agent_id -> specific websocket
    for ws in _active_ide_connections.values():
        try:
            await ws.send_json(request)
            logger.info(f"[IDE-Bridge] Sent tool request {request.get('tool_call_id')} to IDE")
            return True
        except Exception as e:
            logger.error(f"[IDE-Bridge] Failed to send tool request: {e}")
    return False


async def wait_for_ide_tool_result(tool_call_id: str, timeout: float = 30.0) -> Any:
    """Wait for the IDEA plugin to return a tool result."""
    import time
    start_time = time.time()
    event = asyncio.Event()
    _pending_tool_calls[tool_call_id] = event
    
    try:
        await asyncio.wait_for(event.wait(), timeout=timeout)
        duration = time.time() - start_time
        logger.info(f"[IDE-Bridge] Tool {tool_call_id} completed in {duration:.2f}s")
        return _tool_call_results.pop(tool_call_id, None)
    except asyncio.TimeoutError:
        duration = time.time() - start_time
        logger.warning(f"[IDE-Bridge] Tool {tool_call_id} timed out after {duration:.2f}s")
        return f"[Error] Tool call {tool_call_id} timed out after {timeout}s"
    finally:
        _pending_tool_calls.pop(tool_call_id, None)


def resolve_ide_tool_result(tool_call_id: str, result: Any):
    """Resolve a pending tool call with the result from the IDEA plugin."""
    _tool_call_results[tool_call_id] = result
    if tool_call_id in _pending_tool_calls:
        _pending_tool_calls[tool_call_id].set()

def cleanup_pending_calls():
    """Clean up all pending tool calls (e.g., when WebSocket disconnects)."""
    for tool_call_id, event in _pending_tool_calls.items():
        if not event.is_set():
            logger.warning(f"[IDE-Bridge] Cleaning up pending call {tool_call_id}")
            # Set a dummy result to unblock the waiting coroutine
            _tool_call_results[tool_call_id] = "[Error] Connection closed before result received"
            event.set()
    _pending_tool_calls.clear()
    _tool_call_results.clear()
