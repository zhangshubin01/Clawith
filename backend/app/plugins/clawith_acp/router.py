"""ACP WebSocket bridge (IDE thin client ↔ cloud).

Verbose backend tracing (uvicorn process):
  CLAWITH_ACP_VERBOSE=1 — stream summary, tool/thinking traces, inbound WS types
  CLAWITH_ACP_LOG_CHUNKS=1 — log every outbound text chunk (very noisy; implies detailed streaming)
"""

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, HTTPException, Query, Depends
import uuid
import json
import asyncio
import contextlib
import os
import time
from datetime import timezone as tz_
from typing import Any

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

router = APIRouter(tags=["acp"])

# Thin client ↔ cloud JSON envelope (ACP stdio JSON-RPC is separate). Bump when breaking WS shape.
# v2: background recv loop + cancel + permission_request/result + extended ide_* tools.
# v3: cross-connection cancel registry + `cancelled` message (distinct from `done`) + stream abort support in call_llm.
ACP_WS_SCHEMA_VERSION = 3

# Strong refs so asyncio.create_task(persist) is not GC'd before running (see gateway.py pattern).
_acp_background_tasks: set[asyncio.Task] = set()

# (agent_id_hex, user_id_hex, session_id) -> asyncio.Event for the in-flight prompt (any WS may send `cancel`).
_acp_cancel_registry: dict[tuple[str, str, str], asyncio.Event] = {}
_acp_cancel_registry_lock = asyncio.Lock()

_acp_hooks_installed = False


def _acp_ws_envelope(payload: dict[str, Any]) -> dict[str, Any]:
    """Outbound message with schema version for forward-compatible clients."""
    return {"schemaVersion": ACP_WS_SCHEMA_VERSION, **payload}


def _build_acp_user_turn_from_ws(data: dict[str, Any]) -> tuple[Any, str]:
    """Map thin-client `prompt_parts` (+ legacy `text`) to call_llm history content and DB display text.

    Returns (content, persist_text) where content is str or OpenAI-style multipart list.
    """
    parts_in = data.get("prompt_parts")
    legacy = (data.get("text") or "").strip()
    if not parts_in:
        return legacy, legacy
    if not isinstance(parts_in, list):
        return legacy, legacy

    openai_parts: list[dict[str, Any]] = []
    persist_chunks: list[str] = []
    n_images = 0

    for p in parts_in:
        if not isinstance(p, dict):
            continue
        t = p.get("type")
        if t == "text":
            tx = (p.get("text") or "").strip()
            if tx:
                openai_parts.append({"type": "text", "text": tx})
                persist_chunks.append(tx)
        elif t == "image":
            mime = p.get("mime_type") or p.get("mimeType") or "image/png"
            raw = (p.get("data") or "").strip() if isinstance(p.get("data"), str) else ""
            if not raw:
                msg = "[用户消息含图片占位，但无有效 base64 数据；模型无法读图。]"
                openai_parts.append({"type": "text", "text": msg})
                persist_chunks.append("[图片/空]\n" + msg)
                continue
            n_images += 1
            if raw.startswith("data:"):
                url = raw
            else:
                url = f"data:{mime};base64,{raw}"
            openai_parts.append({"type": "image_url", "image_url": {"url": url}})
            persist_chunks.append(f"[图片 {n_images}]")
        elif t == "resource_link":
            uri = p.get("uri") or ""
            name = p.get("name") or "resource"
            note = f"[资源链接: {name} {uri}]".strip()
            openai_parts.append({"type": "text", "text": note})
            persist_chunks.append(note)
        elif t == "resource":
            res = p.get("resource")
            if isinstance(res, dict):
                txt = res.get("text")
                blob = res.get("blob")
                mime = res.get("mimeType") or res.get("mime_type")
                if txt:
                    line = f"[嵌入资源文本]\n{txt}"
                    openai_parts.append({"type": "text", "text": line})
                    persist_chunks.append(f"[嵌入文本 {len(txt)} 字符]")
                elif blob and mime and str(mime).startswith("image/"):
                    n_images += 1
                    url = f"data:{mime};base64,{blob}"
                    openai_parts.append({"type": "image_url", "image_url": {"url": url}})
                    persist_chunks.append(f"[图片 {n_images}]")
                else:
                    persist_chunks.append("[嵌入资源：未传模型]")
            else:
                persist_chunks.append("[嵌入资源]")

    if not openai_parts:
        return legacy, legacy
    if len(openai_parts) == 1 and openai_parts[0].get("type") == "text":
        c = openai_parts[0].get("text") or ""
        disp = "\n".join(persist_chunks).strip() or legacy or c
        return c, disp
    return openai_parts, "\n".join(persist_chunks).strip() or legacy


def install_acp_tool_hooks() -> None:
    """Register IDE tool monkey-patches once (idempotent). Called from plugin.register()."""
    global _acp_hooks_installed
    if _acp_hooks_installed:
        return
    agent_tools.get_agent_tools_for_llm = _custom_get_tools
    agent_tools.execute_tool = _custom_execute_tool
    _acp_hooks_installed = True

current_acp_ws = ContextVar("current_acp_ws", default=None)
current_acp_pending_tools = ContextVar("current_acp_pending_tools", default={})
current_acp_pending_permissions = ContextVar("current_acp_pending_permissions", default={})
# While `call_llm` runs for a prompt, recv_loop can correlate `tool_result` / `permission_result` with this session.
current_acp_prompt_session_id = ContextVar("current_acp_prompt_session_id", default=None)


def _acp_verbose() -> bool:
    return (os.environ.get("CLAWITH_ACP_VERBOSE") or "").strip().lower() in ("1", "true", "yes", "on")


def _acp_log_chunks() -> bool:
    return (os.environ.get("CLAWITH_ACP_LOG_CHUNKS") or "").strip().lower() in ("1", "true", "yes", "on")

# IDE tools that must receive an affirmative `permission_result` before `execute_tool` is sent.
_IDE_TOOLS_REQUIRING_PERMISSION = frozenset(
    {
        "ide_write_file",
        "ide_execute_command",
        "ide_kill_terminal",
        "ide_release_terminal",
        "ide_create_terminal",
    }
)

_IDE_BRIDGE_TOOL_NAMES = frozenset(
    {
        "ide_read_file",
        "ide_write_file",
        "ide_execute_command",
        "ide_kill_terminal",
        "ide_release_terminal",
        "ide_create_terminal",
        "ide_terminal_output",
    }
)

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
                    "path": {"type": "string", "description": "Absolute or relative path to the file"},
                    "limit": {
                        "type": "integer",
                        "description": "Optional max characters to return (maps to IDE read_text_file limit).",
                    },
                    "line": {
                        "type": "integer",
                        "description": "Optional 1-based start line (maps to IDE read_text_file line).",
                    },
                },
                "required": ["path"],
            },
        },
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
                    "content": {"type": "string", "description": "Content to write"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ide_execute_command",
            "description": "Execute a terminal command directly in the IDE client's environment.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Terminal command to execute"},
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ide_create_terminal",
            "description": "Create a terminal in the IDE without waiting for exit; returns a terminal_id for output/kill/release.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Executable or shell entry"},
                    "args": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional argv after command",
                    },
                    "cwd": {
                        "type": "string",
                        "description": "Optional working directory (defaults to session cwd when omitted)",
                    },
                    "env": {
                        "type": "object",
                        "description": "Optional extra environment variables as key→value dict (N14)",
                        "additionalProperties": {"type": "string"},
                    },
                    "output_byte_limit": {
                        "type": "integer",
                        "description": "Optional max bytes to buffer in terminal output (N14)",
                    },
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ide_kill_terminal",
            "description": "Force-kill a terminal created in the IDE session.",
            "parameters": {
                "type": "object",
                "properties": {
                    "terminal_id": {"type": "string", "description": "Terminal id from ide_create_terminal or the IDE"},
                },
                "required": ["terminal_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ide_release_terminal",
            "description": "Release a terminal in the IDE (graceful teardown when supported).",
            "parameters": {
                "type": "object",
                "properties": {
                    "terminal_id": {"type": "string", "description": "Terminal id from ide_create_terminal or the IDE"},
                },
                "required": ["terminal_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ide_terminal_output",
            "description": "Retrieve the current output of a terminal created with ide_create_terminal.",
            "parameters": {
                "type": "object",
                "properties": {
                    "terminal_id": {"type": "string", "description": "Terminal id from ide_create_terminal"},
                },
                "required": ["terminal_id"],
            },
        },
    },
]


async def _acp_await_client_permission(
    websocket: WebSocket,
    pending_permissions: dict[str, asyncio.Future],
    tool_name: str,
    args: dict[str, Any],
    session_id: str = "",
    timeout: float = 120.0,
    extra_payload: dict | None = None,
) -> bool:
    """Ask the thin client / IDE to confirm a sensitive ide_* action; returns True if allowed."""
    perm_id = str(uuid.uuid4())
    loop = asyncio.get_running_loop()
    fut = loop.create_future()
    pending_permissions[perm_id] = fut
    try:
        summary = json.dumps(args, ensure_ascii=False)[:800]
        payload: dict[str, Any] = {
            "type": "permission_request",
            "permission_id": perm_id,
            "tool_name": tool_name,
            "args_summary": summary,
        }
        if extra_payload:
            payload.update(extra_payload)
        await websocket.send_json(_acp_ws_envelope(payload))
        logger.info(
            "ACP permission_request sent session_id={} tool={} perm_id={}",
            session_id or "-",
            tool_name,
            perm_id,
        )
        allowed = bool(await asyncio.wait_for(fut, timeout=timeout))
        logger.info(
            "ACP permission resolved session_id={} tool={} perm_id={} allowed={}",
            session_id or "-",
            tool_name,
            perm_id,
            allowed,
        )
        return allowed
    except asyncio.TimeoutError:
        logger.warning(
            "ACP permission timeout session_id={} tool={}",
            session_id or "-",
            tool_name,
        )
        return False
    finally:
        pending_permissions.pop(perm_id, None)

async def _custom_get_tools(agent_id):
    tools = await _original_get_tools(agent_id)
    if current_acp_ws.get() is not None:
        return tools + IDE_TOOLS
    return tools

async def _custom_execute_tool(
    tool_name, args, agent_id, user_id, session_id: str = ""
):
    """Must match ``agent_tools.execute_tool`` arity: session_id defaults for scheduler/heartbeat/A2A callers."""
    ws = current_acp_ws.get()
    pending = current_acp_pending_tools.get()
    pending_perm: dict[str, asyncio.Future] = current_acp_pending_permissions.get()

    # IDE 工具必须走当前请求关联的 ACP WebSocket；网页聊天没有该连接，IDE 里点同意也不会作用到这条请求上。
    if not ws and tool_name in _IDE_BRIDGE_TOOL_NAMES:
        logger.info(
            "[ACP] ide tool skipped: no active ACP ws session_id={} tool={}",
            session_id or "-",
            tool_name,
        )
        return (
            "[无 IDE 桥接] ide_write_file / ide_read_file 等仅在与本后端建立了 ACP WebSocket 的 "
            "JetBrains 智能体会话中可用。若你在浏览器里聊天，请在 IntelliJ / Android Studio 的 "
            "Clawith Agent 对话里重试同一指令；若要在网页会话里写文件，请让智能体使用 write_file "
            "（写入智能体服务器工作区，不是本机工程目录）。"
        )

    if ws and tool_name in _IDE_BRIDGE_TOOL_NAMES:
        if tool_name in _IDE_TOOLS_REQUIRING_PERMISSION:
            allowed = await _acp_await_client_permission(
                ws, pending_perm, tool_name, args, session_id=session_id
            )
            if not allowed:
                logger.info(
                    "ACP ide tool blocked by permission session_id={} tool={}",
                    session_id or "-",
                    tool_name,
                )
                return f"Permission denied for {tool_name} (IDE user rejected or timed out)."

        call_id = str(uuid.uuid4())
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        pending[call_id] = future

        try:
            logger.info(
                "ACP execute_tool outbound session_id={} name={} call_id={} path_or_cmd={}",
                session_id or "-",
                tool_name,
                call_id,
                (args.get("path") or args.get("command") or "")[:120],
            )
            await ws.send_json(
                _acp_ws_envelope(
                    {
                        "type": "execute_tool",
                        "call_id": call_id,
                        "name": tool_name,
                        "args": args,
                    }
                )
            )
            logger.info(
                "[ACP] execute_tool SENT; blocking until tool_result call_id={} pending_keys_ct={}",
                call_id,
                len(pending),
            )

            result = await asyncio.wait_for(future, timeout=120.0)
            logger.info(
                "ACP tool_result received session_id={} name={} call_id={} result_len={}",
                session_id or "-",
                tool_name,
                call_id,
                len(str(result or "")),
            )
            return result
        except asyncio.TimeoutError:
            logger.error(
                "[ACP] execute_tool TIMEOUT waiting tool_result call_id={} tool={} pending_keys={}",
                call_id,
                tool_name,
                list(pending.keys())[:8] if pending else [],
            )
            pending.pop(call_id, None)
            return f"IDE tool execution timed out: {tool_name}"
        except Exception as e:
            pending.pop(call_id, None)
            return f"IDE tool error: {e}"

    return await _original_execute_tool(tool_name, args, agent_id, user_id, session_id)


install_acp_tool_hooks()


async def _hydrate_if_needed_acp(
    session_id: str,
    agent_obj: AgentModel,
    user_id: uuid.UUID,
    session_messages: dict[str, list],
    parent_session_id: str | None = None,
) -> None:
    """First prompt for this session on this WebSocket: load user/assistant turns from DB.

    O4: if this is a forked session (parent_session_id set) and no own history exists yet,
    copy the parent session's history as the starting context.
    """
    if session_id in session_messages and len(session_messages[session_id]) > 0:
        return
    own = await _load_acp_history_from_db(session_id, agent_obj.id, user_id)
    if own:
        session_messages[session_id] = own
        return
    # O4: new fork — seed from parent history (user-owned, any agent)
    if parent_session_id:
        parent_history = await _load_acp_history_by_owner(parent_session_id, user_id)
        if parent_history:
            logger.info(
                "ACP fork hydrate: session=%s seeded from parent=%s turns=%d",
                session_id,
                parent_session_id,
                len(parent_history),
            )
            session_messages[session_id] = list(parent_history)
            return
    session_messages[session_id] = []


async def _load_acp_history_by_owner(session_id: str, user_id: uuid.UUID) -> list[dict]:
    """O4: Load chat history for any session owned by user_id (no agent constraint)."""
    try:
        sid_uuid = uuid.UUID(session_id)
    except ValueError:
        return []
    async with async_session() as db:
        sr = await db.execute(select(ChatSession).where(ChatSession.id == sid_uuid))
        sess = sr.scalar_one_or_none()
        if not sess or sess.user_id != user_id:
            return []
        mr = await db.execute(
            select(ChatMessage)
            .where(ChatMessage.conversation_id == str(sid_uuid))
            .where(ChatMessage.user_id == user_id)
            .where(ChatMessage.role.in_(("user", "assistant")))
            .order_by(ChatMessage.created_at.asc())
        )
        rows = mr.scalars().all()
        return [{"role": m.role, "content": m.content} for m in rows]


async def _resolve_agent_override(
    override: str, user_id: uuid.UUID
) -> tuple[AgentModel, LLMModel] | None:
    """O5: Look up agent by UUID or name, return (agent, model) or None if not found."""
    async with async_session() as db:
        # Try UUID first, fall back to name
        agent = None
        try:
            aid = uuid.UUID(override)
            ar = await db.execute(select(AgentModel).where(AgentModel.id == aid))
            agent = ar.scalar_one_or_none()
        except ValueError:
            pass
        if agent is None:
            ar = await db.execute(select(AgentModel).where(AgentModel.name == override))
            agent = ar.scalar_one_or_none()
        if agent is None:
            logger.warning("ACP agent_override %r not found, ignoring", override)
            return None
        mr = await db.execute(select(LLMModel).where(LLMModel.id == agent.primary_model_id))
        model = mr.scalar_one_or_none()
        if model is None:
            logger.warning("ACP agent_override %r has no model, ignoring", override)
            return None
        return agent, model


async def _load_acp_history_from_db(
    session_id: str, agent_id: uuid.UUID, user_id: uuid.UUID
) -> list[dict]:
    try:
        sid_uuid = uuid.UUID(session_id)
    except ValueError:
        return []

    async with async_session() as db:
        sr = await db.execute(select(ChatSession).where(ChatSession.id == sid_uuid))
        sess = sr.scalar_one_or_none()
        if not sess or sess.user_id != user_id or sess.agent_id != agent_id:
            if sess:
                logger.warning(
                    "ACP hydrate denied: session=%s user=%s agent=%s",
                    session_id,
                    user_id,
                    agent_id,
                )
            return []

        mr = await db.execute(
            select(ChatMessage)
            .where(ChatMessage.conversation_id == str(sid_uuid))
            .where(ChatMessage.agent_id == agent_id)
            .where(ChatMessage.user_id == user_id)
            .where(ChatMessage.role.in_(("user", "assistant")))
            .order_by(ChatMessage.created_at.asc())
        )
        rows = mr.scalars().all()
        return [{"role": m.role, "content": m.content} for m in rows]


async def _list_acp_chat_sessions(
    user_id: uuid.UUID, agent_id: uuid.UUID, limit: int = 50
) -> list[ChatSession]:
    async with async_session() as db:
        r = await db.execute(
            select(ChatSession)
            .where(ChatSession.user_id == user_id)
            .where(ChatSession.agent_id == agent_id)
            .where(ChatSession.source_channel == "ide_acp")
            .order_by(ChatSession.last_message_at.desc().nulls_last(), ChatSession.created_at.desc())
            .limit(limit)
        )
        return list(r.scalars().all())


@router.websocket("/ws")
async def acp_websocket(
    websocket: WebSocket,
    agent_id: str = Query(...),
    token: str = Query(None),
):
    await websocket.accept()

    # 验证 token（query 已由 Starlette 解码；瘦客户端应对 token 做 URL 编码）
    try:
        user_id = await verify_api_key_or_token(token)
    except HTTPException as e:
        msg = e.detail if isinstance(e.detail, str) else "Unauthorized"
        logger.warning("ACP WS auth failed: {}", msg)
        await websocket.send_json(_acp_ws_envelope({"type": "error", "content": msg}))
        await websocket.close(code=4001)
        return
    except Exception:
        logger.exception("ACP WebSocket auth error")
        await websocket.send_json(
            _acp_ws_envelope({"type": "error", "content": "Unauthorized"})
        )
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

    logger.info(
        "ACP WS connected agent_id={} agent_name={} user_id={} model_id={}",
        agent_obj.id,
        agent_obj.name,
        user_id,
        model_obj.id,
    )

    # Per-connection state: a dedicated receive task must keep reading while `call_llm` awaits IDE tools.
    current_acp_ws.set(websocket)
    pending_tools: dict[str, asyncio.Future] = {}
    pending_permissions: dict[str, asyncio.Future] = {}
    current_acp_pending_tools.set(pending_tools)
    current_acp_pending_permissions.set(pending_permissions)

    session_messages: dict[str, list] = {}
    main_queue: asyncio.Queue = asyncio.Queue()

    async def receive_loop() -> None:
        try:
            while True:
                raw_data = await websocket.receive_text()
                data = json.loads(raw_data)
                msg_type = data.get("type")
                # Always log inbound frames (thin client → cloud); required for hang repro.
                _extra = data.get("call_id") or data.get("permission_id") or data.get("tool_name")
                logger.info(
                    "[ACP] ws-recv type={} session_id={} extra={}",
                    msg_type,
                    data.get("session_id"),
                    _extra,
                )
                if _acp_verbose() and msg_type not in ("tool_result", "permission_result"):
                    if msg_type == "prompt":
                        parts = data.get("prompt_parts")
                        logger.info(
                            "ACP WS inbound type=prompt session_id={} has_parts={} text_len={}",
                            data.get("session_id"),
                            isinstance(parts, list) and len(parts),
                            len((data.get("text") or "") if isinstance(data.get("text"), str) else ""),
                        )
                    else:
                        logger.info("ACP WS inbound type={}", msg_type)
                client_sv = data.get("schemaVersion")
                if client_sv is not None and client_sv > ACP_WS_SCHEMA_VERSION:
                    logger.warning(
                        "ACP WS client schemaVersion %s newer than server %s — upgrade Clawith",
                        client_sv,
                        ACP_WS_SCHEMA_VERSION,
                    )

                if msg_type == "tool_result":
                    # JSON may parse numeric ids; dict keys are str — normalize to match pending_tools.
                    _raw_cid = data.get("call_id")
                    call_id = str(_raw_cid) if _raw_cid is not None else ""
                    result = data.get("result")
                    if not call_id:
                        logger.warning("ACP WS tool_result missing call_id")
                        continue
                    fut = pending_tools.pop(call_id, None)
                    sid_ctx = current_acp_prompt_session_id.get()
                    if fut is not None and not fut.done():
                        fut.set_result(result)
                        logger.info(
                            "ACP WS tool_result matched session_id={} call_id={} result_len={}",
                            sid_ctx or "-",
                            call_id,
                            len(str(result or "")),
                        )
                    else:
                        logger.error(
                            "[ACP] tool_result UNMATCHED session_id={} call_id={} pending_keys={}",
                            sid_ctx or "-",
                            call_id,
                            list(pending_tools.keys())[:8],
                        )
                    continue

                if msg_type == "permission_result":
                    _raw_pid = data.get("permission_id")
                    perm_id = str(_raw_pid) if _raw_pid is not None else ""
                    granted = bool(data.get("granted"))
                    if not perm_id:
                        logger.warning("[ACP] permission_result missing permission_id")
                        continue
                    fut = pending_permissions.pop(perm_id, None)
                    sid_ctx = current_acp_prompt_session_id.get()
                    if fut is not None and not fut.done():
                        fut.set_result(granted)
                        logger.info(
                            "ACP WS permission_result matched session_id={} perm_id={} granted={}",
                            sid_ctx or "-",
                            perm_id,
                            granted,
                        )
                    else:
                        logger.warning(
                            "ACP WS permission_result unmatched session_id={} perm_id={}",
                            sid_ctx or "-",
                            perm_id,
                        )
                    continue

                if msg_type == "cancel":
                    sid = (data.get("session_id") or "").strip()
                    if sid:
                        ck = (str(agent_obj.id), str(user_id), sid)
                        async with _acp_cancel_registry_lock:
                            ev = _acp_cancel_registry.get(ck)
                        if ev is not None:
                            ev.set()
                            logger.info("ACP WS cancel applied session_id={}", sid)
                        else:
                            logger.warning(
                                "ACP WS cancel ignored: no in-flight prompt session_id={}",
                                sid,
                            )
                    else:
                        logger.warning("ACP cancel ignored: missing session_id")
                    continue

                await main_queue.put(data)
        except WebSocketDisconnect:
            logger.info(
                "ACP WebSocket disconnected agent_query={} user_id={}",
                agent_id,
                user_id,
            )
        except Exception as e:
            logger.exception("ACP WebSocket receive_loop error: %s", e)
        finally:
            for _cid, fut in list(pending_tools.items()):
                if not fut.done():
                    fut.set_result("Error: WebSocket closed")
            pending_tools.clear()
            for _pid, fut in list(pending_permissions.items()):
                if not fut.done():
                    fut.set_result(False)
            pending_permissions.clear()
            await main_queue.put({"type": "__acp_shutdown__"})

    recv_task = asyncio.create_task(receive_loop())

    try:
        while True:
            data = await main_queue.get()
            if data.get("type") == "__acp_shutdown__":
                break

            msg_type = data.get("type")
            logger.info(
                "[ACP] main dispatch type={} session_id={}",
                msg_type,
                data.get("session_id"),
            )

            if msg_type == "list_sessions":
                if _acp_verbose():
                    logger.info(
                        "ACP list_sessions user_id={} agent_id={}",
                        user_id,
                        agent_obj.id,
                    )
                rows = await _list_acp_chat_sessions(user_id, agent_obj.id)
                cwd_fallback = data.get("cwd") or "/"
                sessions_out: list[dict[str, Any]] = []
                for s in rows:
                    ts = s.last_message_at or s.created_at
                    iso = None
                    if ts:
                        if ts.tzinfo is None:
                            ts = ts.replace(tzinfo=tz_.utc)
                        iso = ts.astimezone(tz_.utc).isoformat()
                    sessions_out.append(
                        {
                            "sessionId": s.id.hex,
                            "cwd": cwd_fallback,
                            "title": s.title,
                            "updatedAt": iso,
                        }
                    )
                await websocket.send_json(
                    _acp_ws_envelope(
                        {
                            "type": "list_sessions_result",
                            "sessions": sessions_out,
                            "nextCursor": None,
                        }
                    )
                )
                continue

            if msg_type == "prompt":
                session_id = data.get("session_id")
                session_cwd = (data.get("cwd") or "").strip() or "/"
                parent_session_id = (data.get("parent_session_id") or "").strip() or None  # O4
                agent_override_id = (data.get("agent_override") or "").strip() or None      # O5
                # A: IDE-declared MCP servers forwarded by thin client (stored for future MCP bridging)
                mcp_servers_from_ide = data.get("mcp_servers") or []
                if mcp_servers_from_ide:
                    logger.info(
                        "ACP mcp_servers_from_ide session_id={} count={} servers={}",
                        session_id,
                        len(mcp_servers_from_ide),
                        [s.get("url") or s.get("command") for s in mcp_servers_from_ide[:5]],
                    )
                user_content, user_display = _build_acp_user_turn_from_ws(data)

                # O5: resolve agent/model override for this turn
                turn_agent = agent_obj
                turn_model = model_obj
                if agent_override_id:
                    resolved = await _resolve_agent_override(agent_override_id, user_id)
                    if resolved:
                        turn_agent, turn_model = resolved
                        logger.info(
                            "ACP agent_override session_id={} override={} -> agent={} model={}",
                            session_id, agent_override_id, turn_agent.name, turn_model.id,
                        )

                # O4: hydrate history, seeding from parent for forked sessions
                await _hydrate_if_needed_acp(session_id, turn_agent, user_id, session_messages, parent_session_id)
                if session_id not in session_messages:
                    session_messages[session_id] = []
                history = session_messages[session_id]
                history.append({"role": "user", "content": user_content})

                _tool_ui_seen: set[str] = set()
                stream_stats: dict[str, int] = {"chunks": 0, "chars": 0}

                async def on_chunk(text: str):
                    if text:
                        stream_stats["chunks"] += 1
                        stream_stats["chars"] += len(text)
                        n = stream_stats["chunks"]
                        if n == 1 or n % 25 == 0 or _acp_log_chunks():
                            logger.info(
                                "[ACP] ws-send chunk n={} len={} session_id={}",
                                n,
                                len(text),
                                session_id,
                            )
                        await websocket.send_json(_acp_ws_envelope({"type": "chunk", "content": text}))

                async def on_thinking(text: str):
                    if text:
                        if _acp_verbose():
                            logger.info(
                                "ACP outbound thinking session_id={} len={}",
                                session_id,
                                len(text),
                            )
                        await websocket.send_json(
                            _acp_ws_envelope({"type": "thinking", "content": text})
                        )

                async def on_tool_call(tdata: dict):
                    status = tdata.get("status")
                    name = tdata.get("name") or "tool"
                    tid = (tdata.get("tool_call_id") or tdata.get("id") or "").strip()
                    logger.info(
                        "[ACP] tool_round status={} name={} tool_call_id={} session_id={}",
                        status,
                        name,
                        tid or "-",
                        session_id,
                    )
                    if _acp_verbose():
                        logger.info(
                            "ACP tool_ui session_id={} status={} name={} tool_call_id={}",
                            session_id,
                            status,
                            name,
                            tid or "-",
                        )
                    if status == "running":
                        if tid and tid not in _tool_ui_seen:
                            _tool_ui_seen.add(tid)
                            await websocket.send_json(
                                _acp_ws_envelope(
                                    {
                                        "type": "tool_call_start",
                                        "tool_call_id": tid,
                                        "title": name,
                                    }
                                )
                            )
                        elif not tid:
                            msg = f"\n\n🛠️ *正在调用工具: {name}...*\n\n"
                            await websocket.send_json(_acp_ws_envelope({"type": "chunk", "content": msg}))
                    elif status == "done" and tid:
                        await websocket.send_json(
                            _acp_ws_envelope(
                                {
                                    "type": "tool_call_update",
                                    "tool_call_id": tid,
                                    "status": "completed",
                                    "title": name,
                                }
                            )
                        )
                        _tool_ui_seen.discard(tid)

                supports_vision = bool(getattr(turn_model, "supports_vision", False))

                ide_prompt = (
                    "\n\n[IDE 环境提示]\n"
                    f"当前会话工作目录（项目根）: {session_cwd}\n"
                    "你现在正在通过 Agent Client Protocol (ACP) 连接到用户的本地 IDE。\n"
                    "你拥有以下额外的 IDE 专用工具：\n"
                    "- `ide_read_file`: 读取本地文件（可选 `limit` / `line` 控制长度与起始行）\n"
                    "- `ide_write_file`: 新建或修改本地文件（需用户在 IDE 确认）\n"
                    "- `ide_execute_command`: 在本地终端执行命令（需确认）\n"
                    "- `ide_create_terminal`: 创建不阻塞的终端会话，返回 `terminal_id`（需确认）\n"
                    "- `ide_kill_terminal` / `ide_release_terminal`: 结束或释放终端（需确认）\n"
                    "若用户本条消息已含 **图片/截图**（多模态），请直接根据图像回答；**不要**用 `read_file` / `ide_read_file` "
                    "去读取 `.png` `.jpg` 等二进制图片路径来「看图」。\n"
                    "遇到需要修改代码或查看本地**源码文本**时，请优先使用这些 `ide_` 开头的工具！"
                )

                cancel_key = (str(turn_agent.id), str(user_id), str(session_id))
                cancel_prompt = asyncio.Event()
                async with _acp_cancel_registry_lock:
                    if cancel_key in _acp_cancel_registry:
                        await websocket.send_json(
                            _acp_ws_envelope(
                                {
                                    "type": "error",
                                    "content": "This session already has a prompt in progress on another connection.",
                                }
                            )
                        )
                        history.pop()
                        continue
                    _acp_cancel_registry[cancel_key] = cancel_prompt
                _prompt_sid_token = current_acp_prompt_session_id.set(str(session_id))
                try:
                    logger.info(
                        "ACP calling LLM session_id={} agent={} history_len={}",
                        session_id,
                        turn_agent.name,
                        len(history),
                    )
                    t_llm = time.monotonic()
                    reply = await ws_module.call_llm(
                        model=turn_model,
                        messages=history,
                        agent_name=turn_agent.name,
                        role_description=(turn_agent.role_description or "") + ide_prompt,
                        agent_id=turn_agent.id,
                        user_id=user_id,
                        session_id=session_id,
                        on_chunk=on_chunk,
                        on_tool_call=on_tool_call,
                        on_thinking=on_thinking,
                        supports_vision=supports_vision,
                        cancel_event=cancel_prompt,
                    )
                    elapsed_llm = time.monotonic() - t_llm

                    cancelled = reply == "[Cancelled]"
                    logger.info(
                        "[ACP] call_llm RETURN session_id={} cancelled={} reply_len={} elapsed_s={:.2f}",
                        session_id,
                        cancelled,
                        len(reply or ""),
                        elapsed_llm,
                    )
                    if elapsed_llm > 45.0:
                        logger.warning(
                            "ACP call_llm slow session_id={} elapsed_s={:.2f}",
                            session_id,
                            elapsed_llm,
                        )
                    if _acp_verbose():
                        logger.info(
                            "ACP outbound stream summary session_id={} chunks={} total_chars={}",
                            session_id,
                            stream_stats["chunks"],
                            stream_stats["chars"],
                        )
                    history.append({"role": "assistant", "content": reply})

                    _t = asyncio.create_task(
                        _persist_chat_turn(
                            agent_id=turn_agent.id,
                            session_id=session_id,
                            user_text=user_display,
                            reply_text=reply,
                            user_id=user_id,
                        )
                    )
                    _acp_background_tasks.add(_t)
                    _t.add_done_callback(_acp_background_tasks.discard)

                    if cancelled:
                        logger.info("ACP outbound cancelled session_id={}", session_id)
                        await websocket.send_json(
                            _acp_ws_envelope({"type": "cancelled", "session_id": session_id})
                        )
                    else:
                        logger.info(
                            "[ACP] ws-send type=done session_id={} reply_len={} chunks_out={}",
                            session_id,
                            len(reply or ""),
                            stream_stats["chunks"],
                        )
                        # N3: send usage stats before done so thin client can show IDE token meter
                        await websocket.send_json(_acp_ws_envelope({
                            "type": "usage",
                            "used": stream_stats["chars"] // 4,   # output chars → token estimate
                            "size": 200000,                        # approx context window (tokens)
                        }))
                        await websocket.send_json(_acp_ws_envelope({"type": "done"}))
                        logger.info("[ACP] ws-send type=done COMPLETE session_id={}", session_id)
                except Exception as e:
                    logger.error("ACP Error calling LLM session_id={}: {}", session_id, e)
                    await websocket.send_json(_acp_ws_envelope({"type": "error", "content": str(e)}))
                finally:
                    current_acp_prompt_session_id.reset(_prompt_sid_token)
                    async with _acp_cancel_registry_lock:
                        if _acp_cancel_registry.get(cancel_key) is cancel_prompt:
                            _acp_cancel_registry.pop(cancel_key, None)
                continue

            logger.warning(
                "ACP WS ignored unknown message type={} keys_sample={}",
                msg_type,
                list(data.keys())[:8] if isinstance(data, dict) else [],
            )
    finally:
        recv_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await recv_task
        current_acp_ws.set(None)

async def _persist_chat_turn(agent_id, session_id: str, user_text: str, reply_text: str, user_id):
    try:
        from app.models.chat_session import ChatSession
        from app.models.audit import ChatMessage
        from app.models.participant import Participant  # noqa
        from datetime import datetime, timezone as tz_persist

        async with async_session() as db:
            try:
                sid_uuid = uuid.UUID(session_id)
            except ValueError:
                return

            sr = await db.execute(select(ChatSession).where(ChatSession.id == sid_uuid))
            sess = sr.scalar_one_or_none()
            now = datetime.now(tz_persist.utc)
            local_now = datetime.now()
            
            if not sess:
                sess = ChatSession(
                    id=sid_uuid,
                    agent_id=agent_id,
                    user_id=user_id,
                    title=f"IDE {local_now.strftime('%m-%d %H:%M')}",
                    source_channel="ide_acp",
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