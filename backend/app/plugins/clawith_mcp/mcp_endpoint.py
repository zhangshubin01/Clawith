# backend/app/plugins/clawith_mcp/mcp_endpoint.py
"""HTTP MCP 端点（Streamable HTTP + SSE 双协议）。

Cursor / Windsurf 等 MCP HTTP 客户端连接方式：
    POST http://localhost:8008/mcp
    Header: X-Api-Key: cw-xxx

Cursor ~/.cursor/mcp.json 配置示例:
    {
      "mcpServers": {
        "clawith": {
          "url": "http://localhost:8008/mcp",
          "headers": {"X-Api-Key": "cw-xxx"}
        }
      }
    }

Continue / Android Studio SSE 配置示例:
    {
      "mcpServers": [
        {
          "name": "clawith",
          "transport": {
            "type": "sse",
            "url": "http://localhost:8008/mcp/sse",
            "requestOptions": {"headers": {"X-Api-Key": "cw-xxx"}}
          }
        }
      ]
    }
"""
import asyncio as _asyncio
import uuid as _uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse as _SSEStreamingResponse
from loguru import logger
from sqlalchemy import select, func as _func
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import get_current_user
from app.database import get_db
from app.models.user import User
from app.plugins.clawith_mcp.tools import TOOL_DEFINITIONS

router = APIRouter(tags=["mcp"])


# ── JSON-RPC 2.0 帮助函数 ───────────────────────────────────────────────────

def _ok(req_id, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _err(req_id, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


# ── 方法分发 ────────────────────────────────────────────────────────────────

async def _dispatch(method: str, params: dict, user: User, db: AsyncSession) -> dict:
    """分发单个 JSON-RPC 方法，返回 result dict。"""
    if method == "initialize":
        return {
            "protocolVersion": "2024-11-05",
            "serverInfo": {"name": "clawith", "version": "1.0.0"},
            "capabilities": {"tools": {}},
        }

    if method in ("notifications/initialized", "ping"):
        return {}

    if method == "tools/list":
        return {"tools": TOOL_DEFINITIONS}

    if method == "tools/call":
        tool_name = params.get("name", "")
        arguments = params.get("arguments", {})
        return await _execute_tool(tool_name, arguments, user, db)

    raise ValueError(f"Unknown method: {method}")


async def _execute_tool(name: str, arguments: dict, user: User, db: AsyncSession) -> dict:
    """执行工具，返回 {content: [{type, text}]}。"""
    from app.models.agent import Agent

    if name == "list_agents":
        result = await db.execute(
            select(Agent).where(Agent.creator_id == user.id).order_by(Agent.name)
        )
        agents = result.scalars().all()
        if not agents:
            lines = ["暂无可用智能体"]
        else:
            lines = ["可用智能体列表：\n"]
            for a in agents:
                status = "🟢" if a.status == "running" else "⚪"
                lines.append(f"{status} {a.name}")
                lines.append(f"   ID: {a.id}")
                if a.role_description:
                    lines.append(f"   职能: {a.role_description[:80]}")
                lines.append("")
        return {"content": [{"type": "text", "text": "\n".join(lines)}]}

    if name == "call_agent":
        agent_id_raw = arguments.get("agent_id", "")
        message = arguments.get("message", "")
        session_id_raw = arguments.get("session_id")

        if not message:
            raise ValueError("message is required")

        # 解析 agent（UUID 或名称）
        from app.models.agent import Agent as AgentModel
        agent = None
        if agent_id_raw:
            try:
                aid = _uuid.UUID(str(agent_id_raw))
                r = await db.execute(select(AgentModel).where(AgentModel.id == aid))
                agent = r.scalar_one_or_none()
            except ValueError:
                r = await db.execute(
                    select(AgentModel).where(
                        _func.lower(AgentModel.name) == agent_id_raw.lower()
                    )
                )
                agent = r.scalar_one_or_none()

        if not agent:
            raise ValueError(
                f"Agent '{agent_id_raw}' not found. Use list_agents to see available agents."
            )

        # 加载 LLM 模型
        from app.models.llm import LLMModel
        model_result = await db.execute(
            select(LLMModel).where(LLMModel.id == agent.primary_model_id)
        )
        llm_model = model_result.scalar_one_or_none()
        if not llm_model or not llm_model.enabled:
            raise ValueError("Agent LLM model is unavailable")

        # 查找或创建会话
        from app.models.chat_session import ChatSession
        from app.models.audit import ChatMessage

        if session_id_raw:
            session_id_str = str(session_id_raw)
        else:
            sess_r = await db.execute(
                select(ChatSession)
                .where(
                    ChatSession.agent_id == agent.id,
                    ChatSession.user_id == user.id,
                    ChatSession.source_channel == "web",
                )
                .order_by(
                    ChatSession.last_message_at.desc().nulls_last(),
                    ChatSession.created_at.desc(),
                )
                .limit(1)
            )
            sess = sess_r.scalar_one_or_none()
            if not sess:
                now = datetime.now(timezone.utc)
                sess = ChatSession(
                    id=_uuid.uuid4(),
                    agent_id=agent.id,
                    user_id=user.id,
                    title=f"MCP {now.strftime('%m-%d %H:%M')}",
                    source_channel="web",
                    created_at=now,
                )
                db.add(sess)
                await db.commit()
                await db.refresh(sess)
            session_id_str = str(sess.id)

        # 加载历史消息
        hist_r = await db.execute(
            select(ChatMessage)
            .where(ChatMessage.conversation_id == session_id_str)
            .order_by(ChatMessage.created_at.desc())
            .limit(20)
        )
        history = list(reversed(hist_r.scalars().all()))
        messages = [
            {"role": m.role, "content": m.content or ""}
            for m in history
            if m.role in ("user", "assistant")
        ]
        messages.append({"role": "user", "content": message})

        # 保存用户消息
        db.add(ChatMessage(
            agent_id=agent.id,
            user_id=user.id,
            role="user",
            content=message,
            conversation_id=session_id_str,
        ))
        await db.commit()

        # 调用 LLM
        from app.api.websocket import call_llm
        reply = await call_llm(
            model=llm_model,
            messages=messages,
            agent_name=agent.name,
            role_description=agent.role_description or "",
            agent_id=str(agent.id),
            user_id=str(user.id),
            session_id=session_id_str,
        )

        # 保存回复 + 更新会话时间
        db.add(ChatMessage(
            agent_id=agent.id,
            user_id=user.id,
            role="assistant",
            content=reply,
            conversation_id=session_id_str,
        ))
        sess_upd = await db.execute(
            select(ChatSession).where(ChatSession.id == _uuid.UUID(session_id_str))
        )
        sess_obj = sess_upd.scalar_one_or_none()
        if sess_obj:
            sess_obj.last_message_at = datetime.now(timezone.utc)
        await db.commit()

        text = reply + f"\n\n---\n_session_id: {session_id_str}_"
        return {"content": [{"type": "text", "text": text}]}

    raise ValueError(f"Unknown tool: {name}")


# ── 路由处理器 ───────────────────────────────────────────────────────────────

@router.post("/mcp")
async def mcp_handler(
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """MCP Streamable HTTP 端点。

    接受单个或批量 JSON-RPC 2.0 请求。
    兼容 Cursor、Windsurf 及任何 MCP HTTP 客户端。

    认证方式:
        X-Api-Key: cw-xxx
        Authorization: Bearer <jwt>
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}},
            status_code=400,
        )

    async def handle_one(req: dict) -> dict:
        req_id = req.get("id")
        method = req.get("method", "")
        params = req.get("params") or {}
        is_notification = req_id is None
        try:
            result = await _dispatch(method, params, current_user, db)
            if is_notification:
                return {}
            return _ok(req_id, result)
        except Exception as exc:
            logger.warning(f"[MCP] {method} error: {exc}")
            if is_notification:
                return {}
            return _err(req_id, -32603, str(exc))

    if isinstance(body, list):
        responses = [await handle_one(req) for req in body]
        responses = [r for r in responses if r]  # 过滤通知的空响应
        return JSONResponse(responses)

    response = await handle_one(body)
    if not response:
        return JSONResponse({}, status_code=202)
    return JSONResponse(response)


# ── SSE 传输（旧协议，供 Continue / Android Studio 使用）─────────────────────

# session_id → asyncio.Queue（用于 SSE 响应推送）
_sse_sessions: dict[str, _asyncio.Queue] = {}


@router.get("/mcp/sse")
async def mcp_sse_connect(
    request: Request,
    current_user: User = Depends(get_current_user),
):
    """SSE 传输：建立长连接，发送 endpoint 事件告知客户端 POST URL。

    适用于: Android Studio + Continue 插件, 旧版 Cursor, 任何使用 SSE MCP 的客户端。
    """
    session_id = str(_uuid.uuid4())
    queue: _asyncio.Queue = _asyncio.Queue()
    _sse_sessions[session_id] = queue

    # POST URL 告知客户端在哪里发消息
    post_url = f"/mcp/messages?session_id={session_id}"

    async def event_stream():
        # 发送 endpoint 事件（MCP SSE 协议规定的握手）
        yield f"event: endpoint\ndata: {post_url}\n\n"
        try:
            while True:
                # 检查客户端是否断开
                if await request.is_disconnected():
                    break
                try:
                    # 等待来自 /mcp/messages 的响应推送
                    message = await _asyncio.wait_for(queue.get(), timeout=30.0)
                    import json as _json
                    yield f"event: message\ndata: {_json.dumps(message)}\n\n"
                except _asyncio.TimeoutError:
                    # 心跳，保持连接
                    yield ": ping\n\n"
        finally:
            _sse_sessions.pop(session_id, None)

    return _SSEStreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # 禁止 nginx 缓冲
        },
    )


@router.post("/mcp/messages")
async def mcp_sse_messages(
    request: Request,
    session_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """SSE 传输：接收客户端 JSON-RPC 消息，处理后通过 SSE 推送响应。"""
    import json as _json

    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}},
            status_code=400,
        )

    req_id = body.get("id")
    method = body.get("method", "")
    params = body.get("params") or {}
    is_notification = req_id is None

    try:
        result = await _dispatch(method, params, current_user, db)
        if not is_notification:
            response = _ok(req_id, result)
            queue = _sse_sessions.get(session_id)
            if queue:
                await queue.put(response)
    except Exception as exc:
        logger.warning(f"[MCP/SSE] {method} error: {exc}")
        if not is_notification:
            response = _err(req_id, -32603, str(exc))
            queue = _sse_sessions.get(session_id)
            if queue:
                await queue.put(response)

    return JSONResponse({"status": "accepted"}, status_code=202)
