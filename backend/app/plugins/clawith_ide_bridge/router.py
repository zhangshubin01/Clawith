"""Router for Clawith IDE Bridge Plugin."""

from fastapi import APIRouter, WebSocket
from app.plugins.clawith_ide_bridge.tool_handler import resolve_ide_tool_result
from loguru import logger

router = APIRouter(prefix="/api/ide-bridge", tags=["ide-bridge"])

# Store active IDE connections: { session_id: WebSocket }
_active_ide_connections = {}

@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    # Simple handshake to get session_id or agent_id if needed
    # For now, we'll just listen for results
    try:
        while True:
            data = await websocket.receive_json()
            if data.get("type") == "tool_call_result":
                tool_call_id = data.get("tool_call_id")
                result = data.get("result")
                if tool_call_id:
                    logger.info(f"[IDE-Bridge] Received result for {tool_call_id}")
                    resolve_ide_tool_result(tool_call_id, result)
    except Exception as e:
        logger.error(f"[IDE-Bridge] WS error: {e}")


@router.get("/status")
async def get_status():
    return {"status": "active", "plugin": "clawith-ide-bridge"}
