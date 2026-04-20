"""Router for Clawith IDE Bridge Plugin."""

from fastapi import APIRouter, WebSocket, Depends, HTTPException
from app.plugins.clawith_ide_bridge.tool_handler import resolve_ide_tool_result, send_ide_tool_request, cleanup_pending_calls
from app.core.security import get_current_user
from app.models.user import User
from app.models.agent import Agent
from app.database import async_session
from sqlalchemy import select
from loguru import logger

router = APIRouter(prefix="/api/ide-bridge", tags=["ide-bridge"])

# Store active IDE connections: { session_id: WebSocket }
_active_ide_connections = {}
# Store active chat connections for tool forwarding: { agent_id_str: [ (websocket, conv_id) ] }
_active_chat_connections = {}

def register_chat_connection(agent_id_str: str, websocket: WebSocket, conv_id: str):
    if agent_id_str not in _active_chat_connections:
        _active_chat_connections[agent_id_str] = []
    _active_chat_connections[agent_id_str].append((websocket, conv_id))

def unregister_chat_connection(agent_id_str: str, websocket: WebSocket):
    if agent_id_str in _active_chat_connections:
        _active_chat_connections[agent_id_str] = [
            (ws, cid) for ws, cid in _active_chat_connections[agent_id_str] if ws != websocket
        ]

@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    logger.info("[IDE-Bridge] Client connected")
    try:
        while True:
            data = await websocket.receive_json()
            msg_type = data.get("type")
            logger.info(f"[IDE-Bridge] Received message: type={msg_type}")
            
            if msg_type == "tool_call_result":
                tool_call_id = data.get("tool_call_id")
                result = data.get("result")
                if tool_call_id:
                    logger.info(f"[IDE-Bridge] Received result for {tool_call_id}")
                    resolve_ide_tool_result(tool_call_id, result)
            elif msg_type == "ping":
                await websocket.send_json({"type": "pong", "timestamp": __import__('time').time()})
            else:
                # Acknowledge other messages for debugging
                await websocket.send_json({"type": "ack", "received_type": msg_type})
    except Exception as e:
        logger.error(f"[IDE-Bridge] WS error: {e}")
    finally:
        logger.info("[IDE-Bridge] Client disconnected")
        cleanup_pending_calls()


@router.get("/status")
async def get_status():
    return {"status": "active", "plugin": "clawith-ide-bridge"}


@router.get("/agents")
async def list_agents(current_user: User = Depends(get_current_user)):
    """List available agents for the current user."""
    async with async_session() as db:
        result = await db.execute(
            select(Agent).where(Agent.creator_id == current_user.id).order_by(Agent.created_at.desc())
        )
        agents = result.scalars().all()
        return {
            "agents": [
                {
                    "id": str(a.id),
                    "name": a.name,
                    "status": a.status,
                    "avatar": a.avatar_url,
                    "description": a.role_description,
                }
                for a in agents
            ]
        }
