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
# N10: session config from client (auto_approve_diff, etc.)
current_acp_session_config = ContextVar("current_acp_session_config", default={})
# Whether client supports terminal_output incremental streaming via _meta
# This enables native terminal output with incremental updates in IDE
_acp_supports_terminal_output = ContextVar("_acp_supports_terminal_output", default=False)
# Whether client supports structured diff blocks for editing preview via sessionUpdate
_acp_supports_structured_diff = ContextVar("_acp_supports_structured_diff", default=False)

# Global registry so the Clawith frontend WebSocket (/ws/chat/) can also resolve permission futures.
# Keys are permission_id UUIDs (unique per request), so no collision risk across concurrent ACP connections.
_pending_permission_futures: dict[str, asyncio.Future] = {}


def _acp_verbose() -> bool:
    return (os.environ.get("CLAWITH_ACP_VERBOSE") or "").strip().lower() in ("1", "true", "yes", "on")


def _acp_log_chunks() -> bool:
    return (os.environ.get("CLAWITH_ACP_LOG_CHUNKS") or "").strip().lower() in ("1", "true", "yes", "on")

# IDE tools that must receive an affirmative `permission_result` before `execute_tool` is sent.
# ide_write_file requires explicit approval via the Clawith web UI PermissionModal (IDEA ACP plugin
# does not implement session_request_permission natively, so web UI is the only approval surface).
_IDE_TOOLS_REQUIRING_PERMISSION = frozenset(
    {
        "ide_write_file",
        "ide_append",
        "ide_move",
        "delete_file",
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
        "ide_list_files",
        "ide_mkdir",
        "ide_move",
        "ide_append",
        "delete_file",
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
    {
        "type": "function",
        "function": {
            "name": "delete_file",
            "description": "Delete a file from the IDE client's local filesystem.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Absolute or relative path to the file to delete"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ide_list_files",
            "description": "List files and directories in a local directory on the IDE client's filesystem. Use this to explore the user's project structure when you need to find files.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Absolute or relative path to the directory to list"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ide_mkdir",
            "description": "Create a new directory (and any missing parent directories) on the IDE client's filesystem.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Absolute or relative path of the directory to create"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ide_move",
            "description": "Move/rename a file or directory on the IDE client's filesystem. Can be used to rename an existing file or move it to a different directory.",
            "parameters": {
                "type": "object",
                "properties": {
                    "source": {"type": "string", "description": "Absolute or relative path to the source file/directory to move"},
                    "destination": {"type": "string", "description": "Absolute or relative path to the destination"},
                },
                "required": ["source", "destination"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ide_append",
            "description": "Append content to the end of an existing file on the IDE client's filesystem. Useful for adding lines to an existing file without rewriting it entirely.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Absolute or relative path to the file"},
                    "content": {"type": "string", "description": "Content to append to the end of the file"},
                },
                "required": ["path", "content"],
            },
        },
    },
]


async def _acp_await_client_permission(
    websocket: WebSocket,
    pending_permissions: dict[str, asyncio.Future],
    tool_name: str,
    args: dict[str, Any],
    *,
    agent_id: str = "",
    session_id: str = "",
    timeout: float = 120.0,
    extra_payload: dict | None = None,
) -> bool:
    """Ask the thin client / IDE to confirm a sensitive ide_* action; returns True if allowed.

    The permission_request is forwarded to BOTH the ACP WebSocket (IDE thin client) and the
    Clawith frontend chat WebSocket so either the IDE PermissionBroker dialog or the web UI
    PermissionModal can approve. Whichever resolves first wins. The thin client cancels its
    pending _bg_permission task when execute_tool arrives, preventing ACP connection deadlocks
    caused by IntelliJ's modal blocking write_text_file.
    """
    perm_id = str(uuid.uuid4())
    loop = asyncio.get_running_loop()
    fut = loop.create_future()
    pending_permissions[perm_id] = fut
    _pending_permission_futures[perm_id] = fut  # global registry (kept for forward compat)
    try:
        # ide_write_file: send full path+content so thin client can generate a proper diff.
        # Other tools: truncate to 800 chars (content is only needed for display).
        if tool_name == "ide_write_file":
            summary = json.dumps(
                {"path": args.get("path", ""), "content": args.get("content", "")},
                ensure_ascii=False,
            )
        else:
            summary = json.dumps(args, ensure_ascii=False)[:800]
        payload: dict[str, Any] = {
            "type": "permission_request",
            "permission_id": perm_id,
            "tool_name": tool_name,
            "args_summary": summary,
        }
        if extra_payload:
            _RESERVED = {"type", "permission_id", "tool_name", "args_summary", "schemaVersion"}
            payload.update({k: v for k, v in extra_payload.items() if k not in _RESERVED})
        await websocket.send_json(_acp_ws_envelope(payload))
        logger.info(
            "ACP permission_request → thin client session_id={} tool={} perm_id={}",
            session_id or "-",
            tool_name,
            perm_id,
        )
        # Also forward to the web UI via the chat WebSocket so browser users can approve.
        # Whichever dialog (IDE or web) resolves first will unblock the future.
        if agent_id and session_id:
            try:
                await ws_module.manager.send_to_session(str(agent_id), str(session_id), payload)
                logger.debug(
                    "ACP permission_request → frontend chat ws session_id={} perm_id={}",
                    session_id or "-",
                    perm_id,
                )
            except Exception:
                pass
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
        _pending_permission_futures.pop(perm_id, None)


def resolve_acp_permission(perm_id: str, granted: bool) -> bool:
    """Resolve a pending ACP permission from the Clawith frontend WebSocket.

    Returns True if the permission was found and resolved, False if not found or already done.
    """
    fut = _pending_permission_futures.get(perm_id)
    if fut is not None and not fut.done():
        fut.set_result(granted)
        logger.info("ACP permission resolved via frontend ws perm_id={} granted={}", perm_id, granted)
        return True
    logger.warning("ACP permission resolve: perm_id={} not found or already done", perm_id)
    return False

def _generate_structured_diff_blocks(file_path: str, old_content: str, new_content: str) -> list[dict[str, Any]]:
    """Generate ACP structured diff blocks from old/new content.
    
    Follows the claude-agent-acp pattern: creates structured hunk objects with
    location information that the IDE can render inline in permission dialog.
    """
    # Simple diff generation based on line-by-line comparison
    # For more complex diffs, we'd use a proper diff algorithm like difflib,
    # but this follows the ACP structured block format expected by the protocol.
    old_lines = old_content.splitlines(keepends=True)
    new_lines = new_content.splitlines(keepends=True)
    
    diff_blocks: list[dict[str, Any]] = []
    
    # For new file (no old content)
    if not old_lines:
        diff_blocks.append({
            "type": "diff_block",
            "new": new_content,
            "old": "",
            "start_line": 1,
            "end_line": 0,
            "file_path": file_path,
        })
        return diff_blocks
    
    # For empty new file (delete)
    if not new_lines:
        diff_blocks.append({
            "type": "diff_block",
            "new": "",
            "old": old_content,
            "start_line": 1,
            "end_line": len(old_lines),
            "file_path": file_path,
        })
        return diff_blocks
    
    # Use a simple approach: when the whole file changed, send as a single block
    # In future, we could optimize this with proper diff hunk splitting
    diff_blocks.append({
        "type": "diff_block",
        "file_path": file_path,
        "old": old_content,
        "new": new_content,
        "start_line": 1,
        "end_line": len(old_lines),
    })
    
    return diff_blocks

async def _read_file_for_diff(
    ws: WebSocket,
    pending: dict[str, asyncio.Future],
    file_path: str,
    session_id: str = "",
) -> str:
    """通过 IDE bridge 读取文件现有内容，供 diff 展示用。文件不存在或读取失败时返回空字符串。"""
    call_id = str(uuid.uuid4())
    loop = asyncio.get_running_loop()
    future = loop.create_future()
    pending[call_id] = future
    try:
        await ws.send_json(
            _acp_ws_envelope(
                {
                    "type": "execute_tool",
                    "call_id": call_id,
                    "name": "ide_read_file",
                    "args": {"path": file_path},
                }
            )
        )
        result = await asyncio.wait_for(future, timeout=10.0)
        return str(result) if result else ""
    except asyncio.TimeoutError:
        logger.warning(
            "_read_file_for_diff timeout session_id={} path={}",
            session_id or "-",
            file_path,
        )
        return ""
    except Exception as exc:
        logger.debug(
            "_read_file_for_diff failed session_id={} path={} err={}",
            session_id or "-",
            file_path,
            exc,
        )
        return ""
    finally:
        pending.pop(call_id, None)

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

    # delete_file is an IDE bridge tool - delete happens locally on the IDE side

    if ws and tool_name in _IDE_BRIDGE_TOOL_NAMES:
        if tool_name in _IDE_TOOLS_REQUIRING_PERMISSION:
            # Check if auto_approve_diff is enabled in session config
            # When enabled, IDE already shows the diff and no need for extra permission popup on web UI
            # Default to False: IDE will ask user for approval before applying each change
            config = current_acp_session_config.get()
            auto_approve = config.get("auto_approve_diff") or config.get("autoApproval")
            # Check permission mode from session config
            mode = config.get("mode")
            if mode == "acceptEdits":
                # Auto approve all edits - no need to ask user
                allowed = True
            elif mode == "dontAsk":
                # Don't ask, auto reject all unapproved operations
                allowed = False
            elif mode == "bypassPermissions":
                # Bypass all permission checks - always allow
                allowed = True
            else:
                # For other modes, use normal permission flow
                allowed = None
            # Only auto-approve if user explicitly sets it to True
            if auto_approve is not True:
                auto_approve = False
            # If mode already decided permission, use it directly
            if allowed is not None:
                if not allowed:
                    logger.info(
                        "ACP ide tool blocked by permission mode session_id=%s tool=%s mode=%s",
                        session_id or "-",
                        tool_name,
                        mode,
                    )
                    return f"Permission denied for {tool_name} (mode={mode})."
                # allowed is True, skip permission request
            else:
                extra: dict[str, Any] | None = None
                if tool_name == "ide_write_file" or tool_name == "ide_append":
                    file_path = args.get("path", "")
                    if tool_name == "ide_append":
                        old_content = await _read_file_for_diff(ws, pending, file_path, session_id) if file_path else ""
                        new_content = old_content + (args.get("content", "") or "")
                    else:
                        old_content = await _read_file_for_diff(ws, pending, file_path, session_id) if file_path else ""
                        new_content = args.get("content", "")
                    MAX_DIFF_SIZE = 100_000  # 100KB per side
                    if len(old_content) > MAX_DIFF_SIZE:
                        old_content = old_content[:MAX_DIFF_SIZE] + f"\n... (内容过长，已截断，共 {len(old_content)} 字符)"
                    if len(new_content) > MAX_DIFF_SIZE:
                        new_content = new_content[:MAX_DIFF_SIZE] + f"\n... (内容过长，已截断，共 {len(new_content)} 字符)"
                    extra = {
                        "file_path": file_path,
                        "old_content": old_content,
                        "new_content": new_content,
                    }
                    # If client supports structured diff, send diff blocks via sessionUpdate
                    # This allows IDE to preview diff inline in the permission dialog
                    if _acp_supports_structured_diff.get():
                        diff_blocks = _generate_structured_diff_blocks(file_path, old_content, new_content)
                        if diff_blocks:
                            await ws.send_json(_acp_ws_envelope({
                                "type": "sessionUpdate",
                                "_meta": {
                                    "structured_diff": {
                                        "file_path": file_path,
                                        "diff_blocks": diff_blocks,
                                        "tool_name": tool_name,
                                    }
                                }
                            }))
                            logger.info(
                                "ACP sent structured diff via sessionUpdate session_id={} file={} blocks={}",
                                session_id or "-",
                                file_path,
                                len(diff_blocks),
                            )
                elif tool_name == "delete_file":
                    file_path = args.get("path", "")
                    if file_path:
                        extra = {
                            "file_path": file_path,
                        }
                        # For delete, send an empty structured diff to indicate deletion
                        if _acp_supports_structured_diff.get():
                            await ws.send_json(_acp_ws_envelope({
                                "type": "sessionUpdate",
                                "_meta": {
                                    "structured_diff": {
                                        "file_path": file_path,
                                        "diff_blocks": [{
                                            "type": "diff_block",
                                            "file_path": file_path,
                                            "old": "",
                                            "new": "",
                                            "is_deletion": True,
                                        }],
                                        "tool_name": tool_name,
                                    }
                                }
                            }))
                elif tool_name == "ide_move":
                    source = args.get("source", "")
                    destination = args.get("destination", "")
                    extra = {
                        "source": source,
                        "destination": destination,
                    }
                    # For move, send structured diff notification
                    if _acp_supports_structured_diff.get() and source:
                        await ws.send_json(_acp_ws_envelope({
                            "type": "sessionUpdate",
                            "_meta": {
                                "structured_diff": {
                                    "source_path": source,
                                    "destination_path": destination,
                                    "is_move": True,
                                    "tool_name": tool_name,
                                }
                            }
                        }))
                allowed = await _acp_await_client_permission(
                    ws, pending_perm, tool_name, args,
                    agent_id=str(agent_id),
                    session_id=session_id,
                    extra_payload=extra,
                )
                if not allowed:
                    logger.info(
                        "ACP ide tool blocked by permission session_id=%s tool=%s",
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
            
            # Terminal incremental output optimization: when client supports it,
            # send incremental output via sessionUpdate with _meta extension
            if tool_name == "ide_terminal_output" and _acp_supports_terminal_output.get():
                # Result is expected to be structured with incremental chunks and exit info
                # Format: {"output": "...", "exit_code": int, "incremental": list}
                # Or if incremental is not provided, treat entire output as single chunk
                if isinstance(result, dict):
                    terminal_id = args.get("terminal_id")
                    output = result.get("output", "")
                    exit_code = result.get("exit_code")
                    incremental = result.get("incremental")
                    
                    if isinstance(incremental, list):
                        # Send each incremental chunk
                        for chunk in incremental:
                            if chunk:
                                await ws.send_json(_acp_ws_envelope({
                                    "type": "sessionUpdate",
                                    "_meta": {
                                        "terminal_output": {
                                            "terminal_id": terminal_id,
                                            "data": chunk,
                                            "incremental": True
                                        }
                                    }
                                }))
                    elif output:
                        # Send entire output as one chunk
                        await ws.send_json(_acp_ws_envelope({
                            "type": "sessionUpdate",
                            "_meta": {
                                "terminal_output": {
                                    "terminal_id": terminal_id,
                                    "data": output,
                                    "incremental": False
                                }
                            }
                        }))
                    
                    # Send exit code notification if available
                    if exit_code is not None:
                        await ws.send_json(_acp_ws_envelope({
                            "type": "sessionUpdate",
                            "_meta": {
                                "terminal_exit": {
                                    "terminal_id": terminal_id,
                                    "exit_code": exit_code
                                }
                            }
                        }))
            
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

                if msg_type == "ext_method":
                    # IDE -> cloud: extension method call
                    method = data.get("method")
                    params = data.get("params", {})
                    logger.info("[ACP] ext_method method={} params_len={}", method, len(str(params)))
                    # Currently a placeholder - future extensions can register handlers here
                    result: dict[str, Any] = {}
                    await websocket.send_json(
                        _acp_ws_envelope({
                            "type": "ext_method_result",
                            "result": result,
                        })
                    )
                    continue

                if msg_type == "ext_notification":
                    # IDE -> cloud: extension notification (no response)
                    method = data.get("method")
                    params = data.get("params", {})
                    logger.debug("[ACP] ext_notification method={}", method)
                    # No response needed
                    continue

                if msg_type == "close_session":
                    # IDE -> cloud: request to close the session
                    session_id = data.get("session_id")
                    logger.info("[ACP] close_session requested by IDE session_id={}", session_id)
                    # The backend doesn't keep active session state in memory
                    # The session is already persisted to DB, so nothing to clean
                    # This is just a notification for logging/future cleanup
                    continue

                if msg_type == "set_session_mode":
                    # IDE -> cloud: dynamically change session mode
                    session_id = data.get("session_id")
                    mode_id = data.get("mode_id")
                    logger.info("[ACP] set_session_mode requested session_id={} mode_id={}", session_id, mode_id)
                    # Update session config for this connection
                    current_config = current_acp_session_config.get() or {}
                    current_config["mode"] = mode_id
                    current_acp_session_config.set(current_config)
                    # Send acknowledgment back to client
                    await websocket.send_json(
                        _acp_ws_envelope({
                            "type": "current_mode_update",
                            "session_id": session_id,
                            "current_mode": mode_id,
                        })
                    )
                    continue

                if msg_type == "set_session_model":
                    # IDE -> cloud: dynamically change session model
                    session_id = data.get("session_id")
                    model_id = data.get("model_id")
                    logger.info("[ACP] set_session_model requested session_id={} model_id={}", session_id, model_id)
                    # Update session config for this connection
                    current_config = current_acp_session_config.get() or {}
                    current_config["model"] = model_id
                    current_acp_session_config.set(current_config)
                    # Send acknowledgment back to client
                    await websocket.send_json(
                        _acp_ws_envelope({
                            "type": "current_model_update",
                            "session_id": session_id,
                            "current_model_id": model_id,
                        })
                    )
                    continue

                if msg_type == "set_session_config_option":
                    # IDE -> cloud: generic config option update
                    session_id = data.get("session_id")
                    config_id = data.get("config_id")
                    value = data.get("value")
                    logger.info("[ACP] set_session_config_option requested session_id={} config_id={} value={}", session_id, config_id, value)
                    
                    # Handle common config IDs specially
                    if config_id == "mode" and isinstance(value, str):
                        # Update mode
                        current_config = current_acp_session_config.get() or {}
                        current_config["mode"] = value
                        current_acp_session_config.set(current_config)
                        await websocket.send_json(
                            _acp_ws_envelope({
                                "type": "current_mode_update",
                                "session_id": session_id,
                                "current_mode": value,
                            })
                        )
                    elif config_id == "model" and isinstance(value, str):
                        # Update model
                        current_config = current_acp_session_config.get() or {}
                        current_config["model"] = value
                        current_acp_session_config.set(current_config)
                        await websocket.send_json(
                            _acp_ws_envelope({
                                "type": "current_model_update",
                                "session_id": session_id,
                                "current_model_id": value,
                            })
                        )
                    else:
                        # Generic config update - store it
                        current_config = current_acp_session_config.get() or {}
                        current_config[config_id] = value
                        current_acp_session_config.set(current_config)
                    
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

            if msg_type == "initialize":
                # Store client capabilities for this connection
                # Detect terminal_output capability from client metadata
                client_capabilities = data.get("clientCapabilities") or data.get("client_capabilities")
                supports_terminal_output = False
                supports_structured_diff = False
                if isinstance(client_capabilities, dict):
                    # Check _meta.terminal_output === true
                    meta = client_capabilities.get("_meta") or {}
                    supports_terminal_output = meta.get("terminal_output") is True
                    # Check _meta.structured_diff === true
                    supports_structured_diff = meta.get("structured_diff") is True
                
                # Store in context var for this connection
                _acp_supports_terminal_output.set(supports_terminal_output)
                _acp_supports_structured_diff.set(supports_structured_diff)
                if _acp_verbose():
                    logger.info(
                        "ACP initialize received client supports_terminal_output=%s supports_structured_diff=%s session_id=%s",
                        supports_terminal_output,
                        supports_structured_diff,
                        data.get("session_id"),
                    )
                
                # The initialize response is handled by the acp library upstream
                continue

            if msg_type == "list_available_models":
                if _acp_verbose():
                    logger.info(
                        "ACP list_available_models user_id={} agent_id={}",
                        user_id,
                        agent_obj.id,
                    )
                # Get available enabled models from database
                from app.models.llm import LLMModel
                from app.database import async_session
                
                models_out: list[dict[str, str | None]] = []
                current_model_id: str = ""

                async with async_session() as db:
                    from sqlalchemy import select
                    
                    # If agent has specific enabled models configured, filter to those
                    query = select(LLMModel).where(LLMModel.enabled == True)
                    
                    # Check if agent_obj has public_enabled_models attribute and it's not empty
                    if (hasattr(agent_obj, 'public_enabled_models') and 
                        agent_obj.public_enabled_models and 
                        isinstance(agent_obj.public_enabled_models, list) and
                        len(agent_obj.public_enabled_models) > 0):
                        query = query.where(LLMModel.id.in_(agent_obj.public_enabled_models))
                    
                    result = await db.execute(query)
                    for model in result.scalars():
                        models_out.append({
                            "model_id": str(model.id),
                            "id": str(model.id),
                            "name": model.label,
                            "display_name": model.label,
                            "description": f"{model.provider} - {model.model}",
                            "is_enabled": model.enabled,
                        })
                    
                    # If we got any models, pick the first one as default
                    if models_out:
                        current_model_id = models_out[0]["model_id"]

                await websocket.send_json(
                    _acp_ws_envelope(
                        {
                            "type": "list_available_models_result",
                            "available_models": models_out,
                            "current_model_id": current_model_id,
                        }
                    )
                )
                continue

            if msg_type == "prompt":
                session_id = data.get("session_id")
                session_cwd = (data.get("cwd") or "").strip() or "/"
                parent_session_id = (data.get("parent_session_id") or "").strip() or None  # O4
                agent_override_id = (data.get("agent_override") or "").strip() or None      # O5
                # N9: session mode from client (set by set_session_mode)
                session_mode = data.get("mode")
                # N10: session config options from client
                session_config = data.get("config") or {}
                # Merge with stored connection config - dynamic changes via set_session_config_option take precedence
                current_conn_config = current_acp_session_config.get() or {}
                if current_conn_config:
                    # Apply dynamic config changes from RPC calls - they override prompt's initial config
                    session_config.update(current_conn_config)
                    # If mode or model was updated dynamically, use those
                    if "mode" in current_conn_config:
                        session_mode = current_conn_config["mode"]
                        logger.info("[ACP] session_mode from dynamic update: session_id={} mode={}", session_id, session_mode)
                    if "model" in current_conn_config:
                        agent_override_id = current_conn_config["model"]
                        logger.info("[ACP] model from dynamic update: session_id={} model_id={}", session_id, agent_override_id)
                # Store session mode and config in the connection state
                if session_mode and "mode" not in current_conn_config:
                    logger.info("[ACP] session_mode received: session_id={} mode={}", session_id, session_mode)
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
                    # Only send tool UI updates to IDE for actual IDE bridge tools
                    # Internal Clawith tools (like send_message_to_agent) should not be shown in IDE
                    if name not in _IDE_BRIDGE_TOOL_NAMES:
                        return

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
                    "- `ide_list_files`: 列出本地目录内容，探索项目结构找到文件\n"
                    "- `ide_mkdir`: 创建本地目录（自动创建父目录）\n"
                    "- `ide_move`: 移动/重命名本地文件或目录\n"
                    "- `ide_append`: 追加内容到已有文件末尾\n"
                    "- `ide_execute_command`: 在本地终端执行命令（需确认）\n"
                    "- `ide_create_terminal`: 创建不阻塞的终端会话，返回 `terminal_id`（需确认）\n"
                    "- `ide_kill_terminal` / `ide_release_terminal`: 结束或释放终端（需确认）\n"
                    "- `delete_file`: 删除本地文件系统中的文件（需用户在 IDE 确认）\n"
                    "若用户本条消息已含 **图片/截图**（多模态），请直接根据图像回答；**不要**用 `read_file` / `ide_read_file` "
                    "去读取 `.png` `.jpg` 等二进制图片路径来「看图」。\n"
                    "遇到需要修改代码或查看本地**源码文本**时，请优先使用这些 `ide_` 开头的工具！\n"
                    "当你需要删除整个文件时，**请直接使用 `delete_file` 工具**，不要告诉用户手动删除！\n"
                "当你需要探索项目结构、查找文件位置时，**请先用 `ide_list_files` 列出目录**，不要用后端的 `list_files`。\n"
                "当你需要创建新目录时，请直接使用 `ide_mkdir`。\n"
                "当你需要移动文件或重命名文件时，请直接使用 `ide_move`。\n"
                "当你需要追加内容到文件末尾时，请直接使用 `ide_append`。\n"
                )
                # Add session mode prompt if available
                if session_mode:
                    # Extend permission modes inspired by claude-agent-acp
                    mode_descriptions = {
                        "chat": "聊天模式 - 自由对话，回答问题，提供建议",
                        "code-review": "代码评审模式 - 专注于代码评审，分析代码质量，提出改进建议",
                        "planning": "规划模式 - 帮助用户规划任务、设计架构、分解需求",
                        # Additional permission modes from claude-agent-acp
                        "auto": "自动模式 - AI自动分类权限请求，需要批准时询问用户",
                        "default": "默认模式 - 标准行为，危险操作需要用户批准",
                        "acceptEdits": "自动接受编辑 - 自动接受所有文件编辑操作，不需要用户批准",
                        "dontAsk": "不询问 - 不询问权限，直接拒绝未预先批准的操作",
                        "bypassPermissions": "绕过权限检查 - 绕过所有权限检查，直接执行所有工具（仅限非root用户）",
                    }
                    desc = mode_descriptions.get(session_mode, "")
                    if desc:
                        ide_prompt += f"\n\n[工作模式]\n当前模式: {session_mode} - {desc}"
                    else:
                        ide_prompt += f"\n\n[工作模式]\n当前模式: {session_mode}"

                # Add MCP servers information if available from IDE
                if mcp_servers_from_ide:
                    ide_prompt += f"\n\n[MCP 服务器]\n用户IDE提供了 {len(mcp_servers_from_ide)} 个MCP服务器连接可用：\n"
                    for i, server in enumerate(mcp_servers_from_ide, 1):
                        name = server.get("name") or f"server-{i}"
                        server_type = server.get("type") or "unknown"
                        url = server.get("url") or server.get("command") or "custom"
                        ide_prompt += f"  - {name}: {server_type} @ {url}\n"
                    ide_prompt += "你可以使用这些MCP服务器获取额外能力。"

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
                _config_token = current_acp_session_config.set(session_config)
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
                    current_acp_session_config.reset(_config_token)
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

            # Notify Clawith frontend WebSocket so the web UI refreshes in real-time.
            try:
                sid_normalized = str(sid_uuid)
                await ws_module.manager.send_to_session(
                    str(agent_id),
                    sid_normalized,
                    {"type": "done", "role": "assistant", "content": reply_text},
                )
            except Exception as _fe:
                logger.debug("ACP persist: frontend notify failed: {}", _fe)

            from app.services.activity_logger import log_activity
            await log_activity(
                agent_id=agent_id,
                action_type="chat_reply",
                summary=f"回复了IDEA编辑器 内容: {reply_text[:80]}...",
                detail={"channel": "ide_acp", "user_text": user_text[:200], "reply": reply_text[:500]},
                related_id=sid_uuid,
            )
    except Exception as e:
        logger.error(f"Failed to persist chat turn: {e}")