# backend/app/plugins/clawith_mcp/openai_compat.py
"""OpenAI 兼容的 /v1/chat/completions 端点。

model 字段映射到 Clawith 智能体（UUID 或名称）。
支持流式（stream=true）和非流式响应。

Cursor 配置:
    Settings > Models > Add Model
    Base URL: http://localhost:8008
    API Key:  cw-xxx
    Model:    <智能体名称或 UUID>

Continue 配置 (~/.continue/config.json):
    {
      "models": [
        {
          "title": "Clawith - 我的智能体",
          "provider": "openai",
          "model": "<智能体名称>",
          "apiBase": "http://localhost:8008",
          "apiKey": "cw-xxx"
        }
      ]
    }
"""
import asyncio
import json
import time
import uuid as _uuid
from datetime import datetime, timezone as tz_
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import select, func as _func
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import get_current_user
from app.database import get_db
from app.models.user import User

router = APIRouter(tags=["openai-compat"])


# ── GET /v1/models — 供 Android Studio / Cursor 拉取可用"模型"列表 ────────────

@router.get("/v1/models")
@router.get("/models")           # Android Studio omits /v1 prefix
async def list_models(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """OpenAI-compatible model list endpoint.

    Returns all agents accessible to the current user as "models".
    Android Studio and Cursor call this to populate the model picker.
    """
    from app.models.agent import Agent
    import time as _time
    result = await db.execute(
        select(Agent).where(Agent.creator_id == current_user.id).order_by(Agent.name)
    )
    agents = result.scalars().all()
    return {
        "object": "list",
        "data": [
            {
                "id": a.name,                    # 用名称作 model id，方便人类识别
                "object": "model",
                "created": int(_time.time()),
                "owned_by": "clawith",
                "description": a.role_description or "",
                "uuid": str(a.id),               # 扩展字段，方便按 UUID 调用
            }
            for a in agents
        ],
    }


# ── Pydantic 模型 ────────────────────────────────────────────────────────────

class OAIMessage(BaseModel):
    role: str
    content: str


class OAIChatRequest(BaseModel):
    model: str                            # 智能体 UUID 或名称
    messages: list[OAIMessage]
    stream: bool = False
    temperature: Optional[float] = None  # 接受但忽略（智能体使用自己的模型配置）
    max_tokens: Optional[int] = None     # 接受但忽略


# ── 格式化辅助函数 ────────────────────────────────────────────────────────────

def _completion_id() -> str:
    return f"chatcmpl-{_uuid.uuid4().hex[:20]}"


def _oai_response(cid: str, model: str, content: str) -> dict:
    return {
        "id": cid,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": content},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


def _oai_chunk(cid: str, model: str, content: str, finish_reason=None) -> str:
    chunk = {
        "id": cid,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "delta": {"content": content} if content else {},
            "finish_reason": finish_reason,
        }],
    }
    return f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"


def _oai_chunk_role(cid: str, model: str) -> str:
    """第一个 chunk 声明 role=assistant。"""
    chunk = {
        "id": cid,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "delta": {"role": "assistant", "content": ""},
            "finish_reason": None,
        }],
    }
    return f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"


# ── 智能体解析 ───────────────────────────────────────────────────────────────

async def _resolve_agent(model: str, db: AsyncSession):
    """按 UUID 或名称（不区分大小写）查找智能体。"""
    from app.models.agent import Agent
    # 先尝试 UUID
    try:
        aid = _uuid.UUID(model)
        r = await db.execute(select(Agent).where(Agent.id == aid))
        agent = r.scalar_one_or_none()
        if agent:
            return agent
    except ValueError:
        pass
    # 再尝试名称（不区分大小写）
    r = await db.execute(
        select(Agent).where(_func.lower(Agent.name) == model.lower())
    )
    agent = r.scalar_one_or_none()
    if not agent:
        raise HTTPException(
            status_code=404,
            detail=f"Agent '{model}' not found. Use GET /api/agents/ to list available agents.",
        )
    return agent


# ── 主路由 ───────────────────────────────────────────────────────────────────

@router.post("/v1/chat/completions")
@router.post("/chat/completions")   # Android Studio omits /v1 prefix
async def openai_chat_completions(
    body: OAIChatRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """OpenAI 兼容的聊天补全端点。

    将 model 字段映射到 Clawith 智能体（UUID 或名称）。
    支持 stream=true（SSE delta 格式）和 stream=false（完整 JSON）。

    认证:
        X-Api-Key: cw-xxx
        Authorization: Bearer <jwt>
    """
    from app.models.llm import LLMModel
    from app.models.chat_session import ChatSession
    from app.models.audit import ChatMessage
    from app.core.permissions import check_agent_access, is_agent_expired
    from app.api.websocket import call_llm

    agent = await _resolve_agent(body.model, db)
    await check_agent_access(db, current_user, agent.id)  # 权限检查，失败时抛 HTTPException

    if is_agent_expired(agent):
        raise HTTPException(status_code=403, detail="Agent has expired")
    if not agent.primary_model_id:
        raise HTTPException(status_code=400, detail="Agent has no LLM model configured")

    model_result = await db.execute(
        select(LLMModel).where(LLMModel.id == agent.primary_model_id)
    )
    llm_model = model_result.scalar_one_or_none()
    if not llm_model or not llm_model.enabled:
        raise HTTPException(status_code=400, detail="Agent LLM model is unavailable")

    # 将 OAI messages 转为 dict 列表（call_llm 直接接受 OpenAI 格式）
    messages = [{"role": m.role, "content": m.content} for m in body.messages]

    # 查找或创建会话
    sess_r = await db.execute(
        select(ChatSession)
        .where(
            ChatSession.agent_id == agent.id,
            ChatSession.user_id == current_user.id,
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
        now = datetime.now(tz_.utc)
        sess = ChatSession(
            id=_uuid.uuid4(),
            agent_id=agent.id,
            user_id=current_user.id,
            title=f"OAI {now.strftime('%m-%d %H:%M')}",
            source_channel="web",
            created_at=now,
        )
        db.add(sess)
        await db.commit()
        await db.refresh(sess)
    session_id_str = str(sess.id)

    # 保存最后一条用户消息
    last_user_content = next(
        (m["content"] for m in reversed(messages) if m["role"] == "user"), ""
    )
    db.add(ChatMessage(
        agent_id=agent.id,
        user_id=current_user.id,
        role="user",
        content=last_user_content,
        conversation_id=session_id_str,
    ))
    await db.commit()

    cid = _completion_id()

    # ── 非流式 ─────────────────────────────────────────────────────────────
    if not body.stream:
        reply = await call_llm(
            model=llm_model,
            messages=messages,
            agent_name=agent.name,
            role_description=agent.role_description or "",
            agent_id=str(agent.id),
            user_id=str(current_user.id),
            session_id=session_id_str,
        )
        db.add(ChatMessage(
            agent_id=agent.id,
            user_id=current_user.id,
            role="assistant",
            content=reply,
            conversation_id=session_id_str,
        ))
        sess.last_message_at = datetime.now(tz_.utc)
        await db.commit()
        return _oai_response(cid, body.model, reply)

    # ── 流式（SSE） ─────────────────────────────────────────────────────────
    queue: asyncio.Queue = asyncio.Queue()

    async def on_chunk(text: str) -> None:
        await queue.put(("chunk", text))

    async def run_llm() -> None:
        try:
            reply = await call_llm(
                model=llm_model,
                messages=messages,
                agent_name=agent.name,
                role_description=agent.role_description or "",
                agent_id=str(agent.id),
                user_id=str(current_user.id),
                session_id=session_id_str,
                on_chunk=on_chunk,
            )
            await queue.put(("done", reply))
        except Exception as exc:
            await queue.put(("error", str(exc)))

    asyncio.create_task(run_llm())

    async def event_stream():
        yield _oai_chunk_role(cid, body.model)   # 第一个 chunk：声明 role
        full_reply = ""
        while True:
            kind, payload = await queue.get()
            if kind == "chunk":
                full_reply += payload
                yield _oai_chunk(cid, body.model, payload)
            elif kind == "done":
                if not full_reply:
                    full_reply = payload
                # 持久化
                from app.database import get_db as _get_db
                async for _db in _get_db():
                    _db.add(ChatMessage(
                        agent_id=agent.id,
                        user_id=current_user.id,
                        role="assistant",
                        content=full_reply,
                        conversation_id=session_id_str,
                    ))
                    s = await _db.execute(
                        select(ChatSession).where(ChatSession.id == _uuid.UUID(session_id_str))
                    )
                    s_obj = s.scalar_one_or_none()
                    if s_obj:
                        s_obj.last_message_at = datetime.now(tz_.utc)
                    await _db.commit()
                    break
                yield _oai_chunk(cid, body.model, "", finish_reason="stop")
                yield "data: [DONE]\n\n"
                break
            elif kind == "error":
                yield f"data: {json.dumps({'error': {'message': payload}})}\n\n"
                break

    return StreamingResponse(event_stream(), media_type="text/event-stream")
