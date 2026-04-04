from fastapi import APIRouter, WebSocket, WebSocketDisconnect, HTTPException, Query, Depends
import uuid
import json
import asyncio
from loguru import logger
from contextvars import ContextVar

from app.core.security import verify_api_key_or_token
from app.database import async_session
from app.models.agent import Agent as AgentModel
from app.models.llm import LLMModel
from sqlalchemy import select
from app.services import agent_tools
import app.api.websocket as ws_module
from app.models.chat_session import ChatSession
from app.models.audit import ChatMessage
from datetime import datetime, timezone as tz_

router = APIRouter(tags=["acp"])

current_acp_ws = ContextVar("current_acp_ws", default=None)
current_acp_pending_tools = ContextVar("current_acp_pending_tools", default={})

_original_get_tools = agent_tools.get_agent_tools_for_llm
_original_execute_tool = agent_tools.execute_tool

IDE_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "ide_read_file",
            "description": "Read a file from the IDE client's local filesystem.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Absolute or relative path to the file"}
                },
                "required": ["path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "ide_write_file",
            "description": "Write content to a file on the IDE client's local filesystem.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path to write to"},
                    "content": {"type": "string", "description": "Content to write"}
                },
                "required": ["path", "content"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "ide_execute_command",
            "description": "Execute a terminal command directly in the IDE client's environment.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Terminal command to execute"}
                },
                "required": ["command"]
            }
        }
    }
]

async def _custom_get_tools(agent_id):
    tools = await _original_get_tools(agent_id)
    if current_acp_ws.get() is not None:
        return tools + IDE_TOOLS
    return tools

async def _custom_execute_tool(tool_name, args, agent_id, user_id, session_id):
    ws = current_acp_ws.get()
    pending = current_acp_pending_tools.get()
    
    if ws and tool_name in ["ide_read_file", "ide_write_file", "ide_execute_command"]:
        call_id = str(uuid.uuid4())
        future = asyncio.get_event_loop().create_future()
        pending[call_id] = future
        
        try:
            await ws.send_json({
                "type": "execute_tool",
                "call_id": call_id,
                "name": tool_name,
                "args": args
            })
            
            # Wait for client to execute and return
            result = await asyncio.wait_for(future, timeout=120.0)
            return result
        except asyncio.TimeoutError:
            pending.pop(call_id, None)
            return f"IDE tool execution timed out: {tool_name}"
        except Exception as e:
            pending.pop(call_id, None)
            return f"IDE tool error: {e}"

    return await _original_execute_tool(tool_name, args, agent_id, user_id, session_id)

agent_tools.get_agent_tools_for_llm = _custom_get_tools
agent_tools.execute_tool = _custom_execute_tool

@router.websocket("/ws")
async def acp_websocket(
    websocket: WebSocket,
    agent_id: str = Query(...),
    token: str = Query(None),
):
    await websocket.accept()
    
    # 验证 token
    try:
        user_id = await verify_api_key_or_token(token)
    except Exception as e:
        await websocket.send_json({"type": "error", "content": "Unauthorized"})
        await websocket.close(code=4001)
        return

    # Lookup agent
    async with async_session() as db:
        agent_uuid = uuid.UUID(agent_id) if len(agent_id) > 20 else None
        if agent_uuid:
            ar = await db.execute(select(AgentModel).where(AgentModel.id == agent_uuid))
        else:
            ar = await db.execute(select(AgentModel).where(AgentModel.name == agent_id))
        
        agent_obj = ar.scalar_one_or_none()
        if not agent_obj:
            await websocket.send_json({"type": "error", "content": "Agent not found"})
            await websocket.close(code=4004)
            return
            
        mr = await db.execute(select(LLMModel).where(LLMModel.id == agent_obj.primary_model_id))
        model_obj = mr.scalar_one_or_none()
        if not model_obj:
            await websocket.send_json({"type": "error", "content": "LLM model not found"})
            await websocket.close(code=4000)
            return

    # Set context variables
    current_acp_ws.set(websocket)
    pending_tools = {}
    current_acp_pending_tools.set(pending_tools)
    
    session_messages = {}

    try:
        while True:
            # We expect text data from the thin client (websockets lib usually sends text)
            raw_data = await websocket.receive_text()
            data = json.loads(raw_data)
            msg_type = data.get("type")
            
            if msg_type == "tool_result":
                call_id = data.get("call_id")
                result = data.get("result")
                if call_id in pending_tools:
                    pending_tools[call_id].set_result(result)
                    del pending_tools[call_id]
                continue
                
            if msg_type == "prompt":
                session_id = data.get("session_id")
                user_text = data.get("text", "")
                
                if session_id not in session_messages:
                    session_messages[session_id] = []
                history = session_messages[session_id]
                history.append({"role": "user", "content": user_text})
                
                async def on_chunk(text: str):
                    if text:
                        await websocket.send_json({"type": "chunk", "content": text})
                
                async def on_tool_call(tdata: dict):
                    status = tdata.get("status")
                    name = tdata.get("name")
                    if status == "running":
                        msg = f"\n\n🛠️ *正在调用工具: {name}...*\n\n"
                        await websocket.send_json({"type": "chunk", "content": msg})
                        
                ide_prompt = (
                    "\n\n[IDE 环境提示]\n"
                    "你现在正在通过 Agent Client Protocol (ACP) 连接到用户的本地 IDE。\n"
                    "你拥有以下额外的 IDE 专用工具：\n"
                    "- `ide_read_file`: 读取用户本地电脑上的文件代码\n"
                    "- `ide_write_file`: 在用户本地电脑上新建或修改文件\n"
                    "- `ide_execute_command`: 在用户的本地终端中执行命令（如 ./gradlew build 等）\n"
                    "遇到需要修改代码或查看本地环境时，请优先使用这些 `ide_` 开头的工具！"
                )
                
                try:
                    logger.info(f"ACP calling LLM for agent {agent_obj.name}")
                    reply = await ws_module.call_llm(
                        model=model_obj,
                        messages=history,
                        agent_name=agent_obj.name,
                        role_description=(agent_obj.role_description or "") + ide_prompt,
                        agent_id=agent_obj.id,
                        user_id=user_id,
                        session_id=session_id,
                        on_chunk=on_chunk,
                        on_tool_call=on_tool_call,
                    )
                    
                    history.append({"role": "assistant", "content": reply})
                    
                    # 异步持久化
                    asyncio.create_task(_persist_chat_turn(
                        agent_id=agent_obj.id,
                        session_id=session_id,
                        user_text=user_text,
                        reply_text=reply,
                        user_id=user_id
                    ))
                    
                    await websocket.send_json({"type": "done"})
                except Exception as e:
                    logger.error(f"Error calling LLM: {e}")
                    await websocket.send_json({"type": "error", "content": str(e)})
                    
    except WebSocketDisconnect:
        logger.info(f"ACP WebSocket disconnected for agent {agent_id}")
    finally:
        current_acp_ws.set(None)

async def _persist_chat_turn(agent_id, session_id: str, user_text: str, reply_text: str, user_id):
    try:
        from app.models.chat_session import ChatSession
        from app.models.audit import ChatMessage
        from app.models.participant import Participant  # noqa
        from datetime import datetime, timezone as tz_

        async with async_session() as db:
            try:
                sid_uuid = uuid.UUID(session_id)
            except ValueError:
                return

            sr = await db.execute(select(ChatSession).where(ChatSession.id == sid_uuid))
            sess = sr.scalar_one_or_none()
            now = datetime.now(tz_.utc)
            local_now = datetime.now()
            
            if not sess:
                sess = ChatSession(
                    id=sid_uuid,
                    agent_id=agent_id,
                    user_id=user_id,
                    title=f"IDE {local_now.strftime('%m-%d %H:%M')}",
                    source_channel="web",
                    created_at=now,
                    last_message_at=now
                )
                db.add(sess)
            else:
                sess.last_message_at = now

            if user_text:
                db.add(ChatMessage(agent_id=agent_id, user_id=user_id, role="user", content=user_text, conversation_id=str(sid_uuid)))

            if reply_text:
                db.add(ChatMessage(agent_id=agent_id, user_id=user_id, role="assistant", content=reply_text, conversation_id=str(sid_uuid)))

            await db.commit()
            
            from app.services.activity_logger import log_activity
            await log_activity(
                agent_id=agent_id,
                action_type="chat_reply",
                summary=f"Replied to IDE chat: {reply_text[:80]}...",
                detail={"channel": "ide_acp", "user_text": user_text[:200], "reply": reply_text[:500]},
                related_id=sid_uuid,
            )
    except Exception as e:
        logger.error(f"Failed to persist chat turn: {e}")