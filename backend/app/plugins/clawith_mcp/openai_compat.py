# backend/app/plugins/clawith_mcp/openai_compat.py
"""OpenAI 兼容的 /v1/chat/completions 端点。

model 字段映射到 Clawith 智能体（UUID 或名称）。
支持流式（stream=true）和非流式响应。

前提: 后端已加载 clawith-mcp 插件；本路由挂在应用根路径（与 API_PREFIX=/api 无关），
客户端会请求 {Base URL}/v1/chat/completions 与 {Base URL}/v1/models。

Base URL 填写「后端 HTTP 根地址」即可（不要带 /api；一般也不要带 /v1，除非你的客户端
文档明确要求 base 以 /v1 结尾）。

本地端口（以本仓库为准）:
  - 源码 + restart.sh: 默认 BACKEND_PORT=8008 → http://127.0.0.1:8008
  - Docker entrypoint: uvicorn --port 8000（容器内）；宿主机 URL 取决于 compose 端口映射

生产: https://你的 Clawith 后端域名

鉴权: Web 生成的用户 API Key（cw-...）。支持 Authorization: Bearer cw-... 或 X-Api-Key: cw-...

Cursor 配置（路径以当前 Cursor 版本为准）:
    Settings > Models > Add Model（或等效的 OpenAI 兼容 / Custom Base URL）
    Base URL:  见上文（本地示例: http://127.0.0.1:8008）
    API Key:   cw-xxx
    Model:     智能体名称或 UUID（须与聊天里选用的模型一致；若客户端不发 model，服务端可设
               环境变量 CLAWITH_OPENAI_COMPAT_DEFAULT_AGENT）

Continue 配置 (~/.continue/config.json):
    {
      "models": [
        {
          "title": "Clawith - 我的智能体",
          "provider": "openai",
          "model": "<智能体名称>",
          "apiBase": "http://127.0.0.1:8008",
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

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import select, func as _func
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import get_db
from app.models.user import User

router = APIRouter(tags=["openai-compat"])

security = HTTPBearer(auto_error=False)


# ── GET /v1/models — 供 Android Studio / Cursor 拉取可用"模型"列表 ────────────

@router.get("/v1/models")
@router.get("/models")           # Android Studio omits /v1 prefix
async def list_models(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
    db: AsyncSession = Depends(get_db),
):
    """Authenticate with API key (cw-...) or JWT token."""
    from app.core.security import verify_api_key_or_token
    user_id = await verify_api_key_or_token(
        request.headers.get("X-Api-Key")
        or (credentials.credentials if credentials else None)
    )
    from app.models.user import User
    result = await db.execute(select(User).where(User.id == user_id))
    current_user = result.scalar_one_or_none()
    if not current_user or not current_user.is_active:
        raise HTTPException(status_code=401, detail="Invalid or inactive user")
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
    model_config = {"extra": "ignore"}   # 忽略未知字段（name, tool_calls 等）

    role: str
    # content 可以是字符串、list（vision/multipart）或 null
    content: Optional[str | list] = None

    def text(self) -> str:
        """提取纯文本内容，兼容多模态 list 格式。"""
        if self.content is None:
            return ""
        if isinstance(self.content, str):
            return self.content
        # list 格式: [{"type":"text","text":"..."},...]
        parts = []
        for part in self.content:
            if isinstance(part, dict) and part.get("type") == "text":
                parts.append(part.get("text", ""))
        return "".join(parts)


class OAIChatRequest(BaseModel):
    model_config = {"extra": "ignore"}   # 忽略 Android Studio 发的额外字段

    # 智能体 UUID 或名称；客户端可省略，此时使用 CLAWITH_OPENAI_COMPAT_DEFAULT_AGENT
    model: Optional[str] = None
    messages: list[OAIMessage]
    stream: bool = False
    temperature: Optional[float] = None             # 接受但忽略
    max_tokens: Optional[int] = None                # 接受但忽略
    max_completion_tokens: Optional[int] = None     # 新版 OpenAI 字段，忽略
    store: Optional[bool] = None                    # 新版 OpenAI 字段，忽略


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
    return f"data: {json.dumps(chunk, ensure_ascii=True)}\n\n"


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
    return f"data: {json.dumps(chunk, ensure_ascii=True)}\n\n"


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
    request: Request,
    body: OAIChatRequest,
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
    db: AsyncSession = Depends(get_db),
):
    """Authenticate with API key (cw-...) or JWT token."""
    from app.core.security import verify_api_key_or_token
    user_id = await verify_api_key_or_token(
        request.headers.get("X-Api-Key")
        or (credentials.credentials if credentials else None)
    )
    from app.models.user import User
    result = await db.execute(select(User).where(User.id == user_id))
    current_user = result.scalar_one_or_none()
    if not current_user or not current_user.is_active:
        raise HTTPException(status_code=401, detail="Invalid or inactive user")
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

    model_ref = (body.model or "").strip()
    if not model_ref:
        model_ref = (get_settings().CLAWITH_OPENAI_COMPAT_DEFAULT_AGENT or "").strip()
    if not model_ref:
        raise HTTPException(
            status_code=400,
            detail=(
                "model is required: set OpenAI `model` to a Clawith agent name or UUID "
                "(see GET /v1/models), or set env CLAWITH_OPENAI_COMPAT_DEFAULT_AGENT on the server. "
                "In Cursor: pick that agent in the model dropdown for this custom endpoint."
            ),
        )

    agent = await _resolve_agent(model_ref, db)
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
    if not (llm_model.model or "").strip():
        raise HTTPException(
            status_code=400,
            detail=(
                "LLM model pool entry has an empty provider model ID; open Admin → LLM models "
                "and set Model (e.g. gpt-4o) — API key alone is not enough."
            ),
        )

    # 将 OAI messages 转为 dict 列表（call_llm 直接接受 OpenAI 格式）
    # role 映射：developer(新版 OpenAI) → system；过滤空消息
    _ROLE_MAP = {"developer": "system"}
    messages = [
        {"role": _ROLE_MAP.get(m.role, m.role), "content": m.text()}
        for m in body.messages
        if m.text().strip()
    ]

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

    # Android Studio AI 插件的流式渲染会把每个 SSE chunk 显示为独立行，
    # 导致逐字换行的视觉问题。检测到 Android Studio 时强制使用非流式响应。
    _ua = request.headers.get("user-agent", "").lower()
    _is_android_studio = any(k in _ua for k in ("android studio", "google-aiplugin", "aiplugin", "gemini-plugin"))

    # ── 非流式 ─────────────────────────────────────────────────────────────
    if not body.stream or _is_android_studio:
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
        return _oai_response(cid, model_ref, reply)

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
        yield _oai_chunk_role(cid, model_ref)   # 第一个 chunk：声明 role
        full_reply = ""
        while True:
            kind, payload = await queue.get()
            if kind == "chunk":
                full_reply += payload
                yield _oai_chunk(cid, model_ref, payload)
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
                yield _oai_chunk(cid, model_ref, "", finish_reason="stop")
                yield "data: [DONE]\n\n"
                break
            elif kind == "error":
                yield f"data: {json.dumps({'error': {'message': payload}})}\n\n"
                break

    return StreamingResponse(event_stream(), media_type="text/event-stream")
