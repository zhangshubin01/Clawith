"""Clawith ACP Thin Client — IDE 侧瘦客户端（JetBrains Agent Client Protocol）

通过 WebSocket 连接云端 Clawith，不访问本地数据库。详见同目录 README.md。

环境变量:
  CLAWITH_URL, CLAWITH_API_KEY, CLAWITH_DEFAULT_AGENT_ID（可选，默认 WL4）
  CLAWITH_WS_PROXY — 见 README

与云端 WebSocket 的 JSON 信封字段 schemaVersion 须与 Clawith 插件
`backend/app/plugins/clawith_acp/router.py` 中 ACP_WS_SCHEMA_VERSION 一致（当前 v3：`cancelled`、跨连接 cancel）。
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import platform
import sys
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse
from uuid import uuid4

import websockets
from acp import (
    Agent,
    InitializeResponse,
    NewSessionResponse,
    PromptResponse,
    run_agent,
    start_tool_call,
    text_block,
    update_agent_message,
    update_agent_thought_text,
    update_tool_call,
)
from acp.interfaces import Client
from acp.schema import (
    AgentCapabilities,
    AuthenticateResponse,
    ClientCapabilities,
    CloseSessionResponse,
    ForkSessionResponse,
    HttpMcpServer,
    Implementation as AgentImplementation,
    ListSessionsResponse,
    LoadSessionResponse,
    McpServerStdio,
    PromptCapabilities,
    ResumeSessionResponse,
    SessionInfo,
    SetSessionConfigOptionResponse,
    SetSessionModeResponse,
    SetSessionModelResponse,
    SseMcpServer,
)

logger = logging.getLogger("clawith_acp")
handler = logging.StreamHandler(sys.stderr)
handler.setFormatter(logging.Formatter("[ACP Thin Client] %(levelname)s: %(message)s"))
logger.addHandler(handler)
logger.setLevel(logging.INFO)

_IS_WINDOWS = platform.system().lower().startswith("win")

# Keep in sync with backend `ACP_WS_SCHEMA_VERSION`
CLOUD_WS_SCHEMA_VERSION = 3

# Max image file size when resolving `file:` / local path from IDE (bytes).
_MAX_IMAGE_BYTES = int(os.environ.get("CLAWITH_ACP_MAX_IMAGE_BYTES", str(20 * 1024 * 1024)))

# JetBrains often sends pasted screenshots as ResourceContentBlock (`resource_link`) with file://, not `image`.
_IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"})


def _mime_type_for_image_path(path: Path) -> str:
    ext = path.suffix.lower()
    return {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".webp": "image/webp",
        ".bmp": "image/bmp",
    }.get(ext, "image/png")


def _local_path_from_image_uri(uri: str) -> Path | None:
    """Map ACP ImageContentBlock `uri` (file:// or absolute path) to a readable local file."""
    u = (uri or "").strip()
    if not u:
        return None
    if u.startswith("file:"):
        parsed = urlparse(u)
        if parsed.scheme != "file":
            return None
        path = unquote(parsed.path or "")
        if _IS_WINDOWS and path.startswith("/") and len(path) > 2 and path[2] == ":":
            path = path[1:]
        try:
            p = Path(path)
            return p if p.is_file() else None
        except OSError:
            return None
    try:
        p = Path(u)
        if p.is_absolute() and p.is_file():
            return p
    except OSError:
        return None
    return None


def _read_image_file_as_base64(path: Path) -> str | None:
    try:
        raw = path.read_bytes()
        if len(raw) > _MAX_IMAGE_BYTES:
            logger.warning(
                "ACP image file too large (%s bytes, max %s): %s",
                len(raw),
                _MAX_IMAGE_BYTES,
                path,
            )
            return None
        return base64.standard_b64encode(raw).decode("ascii")
    except OSError as e:
        logger.warning("ACP could not read image file %s: %s", path, e)
        return None


def _resolve_image_payload_for_cloud(d: dict[str, Any]) -> tuple[str, str]:
    """Return (mime_type, base64_without_data_prefix). Empty if no pixels to send."""
    mime = d.get("mimeType") or d.get("mime_type") or "image/png"
    data = (d.get("data") or "").strip()
    if data.startswith("data:") and ";base64," in data:
        return mime, data.split(";base64,", 1)[1].strip()
    if data:
        return mime, data
    uri = (d.get("uri") or "").strip()
    if uri:
        lp = _local_path_from_image_uri(uri)
        if lp is not None:
            b64 = _read_image_file_as_base64(lp)
            if b64:
                return mime, b64
        logger.warning("ACP image: empty data and uri not readable: %s", uri[:200])
    return mime, ""


def _websocket_proxy_kw() -> dict[str, Any]:
    """websockets 15+ 默认会读系统代理；SOCKS 需 python-socks。直连 Clawith 建议 proxy=None。"""
    raw = (os.environ.get("CLAWITH_WS_PROXY") or "").strip().lower()
    if raw in ("", "none", "direct", "0", "false", "off"):
        return {"proxy": None}
    if raw in ("auto", "env", "1", "true", "yes", "on"):
        return {"proxy": True}
    return {"proxy": os.environ.get("CLAWITH_WS_PROXY", "").strip()}


def _cloud_msg(payload: dict[str, Any]) -> dict[str, Any]:
    return {"schemaVersion": CLOUD_WS_SCHEMA_VERSION, **payload}


def _acp_block_to_dict(block: Any) -> dict[str, Any] | None:
    if block is None:
        return None
    if hasattr(block, "model_dump"):
        return block.model_dump(by_alias=True, mode="json")
    if isinstance(block, dict):
        return block
    return None


def _serialize_acp_prompt_for_cloud(prompt: list[Any]) -> tuple[list[dict[str, Any]], str]:
    """Turn ACP content blocks into WS `prompt_parts` + a plain-text summary for logs."""
    wire: list[dict[str, Any]] = []
    plain_bits: list[str] = []
    for block in prompt:
        d = _acp_block_to_dict(block)
        if not d:
            continue
        t = d.get("type")
        if t == "text":
            text = d.get("text") or ""
            plain_bits.append(text)
            if text.strip():
                wire.append({"type": "text", "text": text})
        elif t == "image":
            mime, payload = _resolve_image_payload_for_cloud(d)
            if not payload:
                wire.append(
                    {
                        "type": "text",
                        "text": (
                            "[用户添加了图片，但本机未能读出像素数据（可改直接粘贴截图）。"
                            + (
                                f" 路径/URI: {(d.get('uri') or '')[:200]}"
                                if (d.get("uri") or "").strip()
                                else ""
                            )
                        ),
                    }
                )
                plain_bits.append("[图片/未编码]")
                continue
            wire.append({"type": "image", "mime_type": mime, "data": payload})
            plain_bits.append("[图片]")
        elif t == "resource_link":
            uri = (d.get("uri") or "").strip()
            name = (d.get("name") or "").strip()
            lp = _local_path_from_image_uri(uri)
            if (
                lp is not None
                and lp.suffix.lower() in _IMAGE_SUFFIXES
                and (b64 := _read_image_file_as_base64(lp))
            ):
                mime = _mime_type_for_image_path(lp)
                wire.append({"type": "image", "mime_type": mime, "data": b64})
                plain_bits.append("[图片]")
                continue
            wire.append({"type": "resource_link", "uri": uri, "name": name})
            plain_bits.append(f"[链接: {name or uri or 'resource'}]")
        elif t == "resource":
            wire.append({"type": "resource", "resource": d.get("resource")})
            plain_bits.append("[资源]")
        elif t == "audio":
            logger.warning("ACP audio block omitted for cloud (not supported)")
        else:
            logger.debug("ACP block type %s skipped for cloud bridge", t)
    return wire, "".join(plain_bits)


def _check_server_schema(data: dict[str, Any]) -> None:
    sv = data.get("schemaVersion")
    if sv is not None and sv > CLOUD_WS_SCHEMA_VERSION:
        logger.warning(
            "Cloud schemaVersion %s > thin client %s — upgrade integrations/clawith-ide-acp",
            sv,
            CLOUD_WS_SCHEMA_VERSION,
        )


class ClawithThinClientAgent(Agent):
    _conn: Client

    def __init__(self, agent_id: str, api_key: str, backend_url: str):
        super().__init__()
        self.agent_id = agent_id
        self.api_key = api_key
        ws_base = backend_url.replace("http://", "ws://").replace("https://", "wss://")
        self.ws_endpoint = f"{ws_base}/api/plugins/clawith-acp/ws?agent_id={agent_id}&token={api_key}"
        # Session cwd from IDE (for create_terminal / shell without explicit cwd)
        self._session_cwds: dict[str, str] = {}
        # Same WebSocket used for an in-flight `prompt` — used by `cancel` to reach the cloud loop.
        self._active_prompt_ws: Any = None
        logger.info("Initialized Thin Client for agent_id: %s", self.agent_id)

    def on_connect(self, conn: Client) -> None:
        self._conn = conn

    async def initialize(
        self,
        protocol_version: int,
        client_capabilities: ClientCapabilities | None = None,
        client_info: AgentImplementation | None = None,
        **kwargs: Any,
    ) -> InitializeResponse:
        logger.info("Connected to IDE: %s", client_info)
        return InitializeResponse(
            protocol_version=protocol_version,
            agent_info=AgentImplementation(
                name="clawith-acp-thin",
                version="0.2.0",
                title="Clawith ACP Thin Client",
            ),
            agent_capabilities=AgentCapabilities(
                load_session=True,
                prompt_capabilities=PromptCapabilities(
                    image=True,
                    embedded_context=False,
                    audio=False,
                ),
            ),
        )

    async def new_session(
        self,
        cwd: str,
        mcp_servers: list[HttpMcpServer | SseMcpServer | McpServerStdio] | None = None,
        **kwargs: Any,
    ) -> NewSessionResponse:
        session_id = uuid4().hex
        self._session_cwds[session_id] = cwd or "/"
        logger.info("New session. cwd=%s session_id=%s", cwd, session_id)
        return NewSessionResponse(session_id=session_id)

    async def load_session(
        self,
        cwd: str,
        session_id: str,
        mcp_servers: list[HttpMcpServer | SseMcpServer | McpServerStdio] | None = None,
        **kwargs: Any,
    ) -> LoadSessionResponse | None:
        """IDE 恢复会话：云端会在首次 prompt 时从 DB 水合历史；此处仅声明成功。"""
        self._session_cwds[session_id] = cwd or "/"
        logger.info("load_session session_id=%s cwd=%s", session_id, cwd)
        return LoadSessionResponse()

    async def list_sessions(
        self, cursor: str | None = None, cwd: str | None = None, **kwargs: Any
    ) -> ListSessionsResponse:
        proxy_kw = _websocket_proxy_kw()
        try:
            async with websockets.connect(self.ws_endpoint, **proxy_kw) as ws:
                await ws.send(
                    json.dumps(
                        _cloud_msg(
                            {
                                "type": "list_sessions",
                                "cursor": cursor,
                                "cwd": cwd,
                            }
                        )
                    )
                )
                raw = await ws.recv()
                data = json.loads(raw)
                _check_server_schema(data)
                if data.get("type") != "list_sessions_result":
                    logger.warning("Unexpected list_sessions response: %s", data.get("type"))
                    return ListSessionsResponse(sessions=[])
                sessions: list[SessionInfo] = []
                for item in data.get("sessions") or []:
                    sid = item.get("sessionId") or item.get("session_id")
                    if not sid:
                        continue
                    sessions.append(
                        SessionInfo(
                            cwd=item.get("cwd") or cwd or "/",
                            session_id=sid,
                            title=item.get("title"),
                            updated_at=item.get("updatedAt") or item.get("updated_at"),
                        )
                    )
                return ListSessionsResponse(
                    sessions=sessions,
                    next_cursor=data.get("nextCursor"),
                )
        except Exception as e:
            logger.error("list_sessions cloud error: %s", e)
            return ListSessionsResponse(sessions=[])

    async def close_session(self, session_id: str, **kwargs: Any) -> CloseSessionResponse | None:
        logger.info("close_session session_id=%s", session_id)
        return CloseSessionResponse()

    async def cancel(self, session_id: str, **kwargs: Any) -> None:
        ws = self._active_prompt_ws
        if ws is None:
            logger.info("cancel session_id=%s (no active cloud prompt)", session_id)
            return
        try:
            await ws.send(
                json.dumps(_cloud_msg({"type": "cancel", "session_id": session_id}))
            )
            logger.info("cancel session_id=%s sent to cloud", session_id)
        except Exception as e:
            logger.warning("cancel session_id=%s failed: %s", session_id, e)

    async def authenticate(self, method_id: str, **kwargs: Any) -> AuthenticateResponse | None:
        """Clawith 使用 query token；IDE 若走 ACP OAuth 流可后续扩展。"""
        logger.debug("authenticate method_id=%s (noop)", method_id)
        return AuthenticateResponse()

    async def set_session_mode(self, mode_id: str, session_id: str, **kwargs: Any) -> SetSessionModeResponse | None:
        return SetSessionModeResponse()

    async def set_session_model(self, model_id: str, session_id: str, **kwargs: Any) -> SetSessionModelResponse | None:
        return SetSessionModelResponse()

    async def set_config_option(
        self, config_id: str, session_id: str, value: str | bool, **kwargs: Any
    ) -> SetSessionConfigOptionResponse | None:
        return SetSessionConfigOptionResponse(config_options=[])

    async def fork_session(
        self,
        cwd: str,
        session_id: str,
        mcp_servers: list[HttpMcpServer | SseMcpServer | McpServerStdio] | None = None,
        **kwargs: Any,
    ) -> ForkSessionResponse:
        new_id = uuid4().hex
        self._session_cwds[new_id] = self._session_cwds.get(session_id) or cwd or "/"
        logger.info("fork_session -> new session_id=%s", new_id)
        return ForkSessionResponse(session_id=new_id)

    async def resume_session(
        self,
        cwd: str,
        session_id: str,
        mcp_servers: list[HttpMcpServer | SseMcpServer | McpServerStdio] | None = None,
        **kwargs: Any,
    ) -> ResumeSessionResponse:
        self._session_cwds[session_id] = cwd or "/"
        return ResumeSessionResponse()

    async def ext_method(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        logger.debug("ext_method %s", method)
        return {}

    async def ext_notification(self, method: str, params: dict[str, Any]) -> None:
        logger.debug("ext_notification %s", method)

    async def _run_shell_command(self, user_command: str, session_id: str) -> str:
        """macOS/Linux: /bin/sh -c；Windows: cmd.exe /c。"""
        cwd = self._session_cwds.get(session_id)
        if _IS_WINDOWS:
            term = await self._conn.create_terminal(
                command="cmd.exe",
                args=["/c", user_command],
                session_id=session_id,
                cwd=cwd,
            )
        else:
            term = await self._conn.create_terminal(
                command="/bin/sh",
                args=["-c", user_command],
                session_id=session_id,
                cwd=cwd,
            )
        tid = term.terminal_id
        await self._conn.wait_for_terminal_exit(terminal_id=tid, session_id=session_id)
        out = await self._conn.terminal_output(terminal_id=tid, session_id=session_id)
        return out.content or "Command executed successfully (no output)."

    async def _resolve_cloud_permission(
        self,
        perm_id: str | None,
        tool_name: str,
        summary: str,
        session_id: str,
    ) -> bool:
        """Map cloud `permission_request` to IDE `request_permission` (or env override)."""
        mode = (os.environ.get("CLAWITH_ACP_PERMISSION") or "ide").strip().lower()
        if mode in ("deny", "reject", "0", "false", "no"):
            return False
        if mode in ("allow", "always", "yes", "1", "true"):
            return True
        try:
            from acp.schema import AllowedOutcome, PermissionOption, ToolCallUpdate

            opts = [
                PermissionOption(name="Allow once", option_id="allow_once", kind="allow_once"),
                PermissionOption(name="Deny", option_id="deny_once", kind="reject_once"),
            ]
            tid = perm_id or uuid4().hex
            tc = ToolCallUpdate(
                tool_call_id=tid,
                title=tool_name,
                kind="execute",
                status="pending",
                raw_input=(summary[:2000] if summary else None),
            )
            resp = await self._conn.request_permission(
                options=opts, session_id=session_id, tool_call=tc
            )
            return isinstance(resp.outcome, AllowedOutcome)
        except Exception as e:
            logger.warning("IDE permission dialog failed (%s); denying cloud tool", e)
            return False

    async def prompt(
        self,
        prompt: list[Any],
        session_id: str,
        **kwargs: Any,
    ) -> PromptResponse:
        wire_parts, user_text = _serialize_acp_prompt_for_cloud(prompt)
        if not user_text and not wire_parts:
            user_text = ""

        logger.info("Forwarding prompt to cloud: %s...", user_text[:100] or "(multipart)")
        await self._conn.session_update(
            session_id=session_id,
            update=update_agent_message(text_block("⏳ 连接云端 Clawith 智能体进行处理...\n\n")),
        )

        final_stop: str = "end_turn"
        try:
            proxy_kw = _websocket_proxy_kw()
            if proxy_kw.get("proxy") is None:
                logger.info(
                    "WebSocket: direct (no system proxy); set CLAWITH_WS_PROXY=auto for env-based proxy"
                )
            async with websockets.connect(self.ws_endpoint, **proxy_kw) as ws:
                self._active_prompt_ws = ws
                try:
                    prompt_body: dict[str, Any] = {
                        "type": "prompt",
                        "session_id": session_id,
                    }
                    if wire_parts:
                        prompt_body["prompt_parts"] = wire_parts
                    else:
                        prompt_body["text"] = user_text
                    await ws.send(json.dumps(_cloud_msg(prompt_body)))

                    while True:
                        msg = await ws.recv()
                        data = json.loads(msg)
                        _check_server_schema(data)
                        msg_type = data.get("type")
                        if msg_type == "chunk":
                            logger.debug(
                                "ACP thin: cloud msg type=chunk len=%s",
                                len(data.get("content") or ""),
                            )
                        else:
                            logger.info("ACP thin: cloud msg type=%s", msg_type)

                        if msg_type == "permission_request":
                            perm_id = data.get("permission_id")
                            tool_name = data.get("tool_name") or "ide_tool"
                            summary = data.get("args_summary") or ""
                            logger.info(
                                "ACP thin: permission_request from cloud tool=%s perm_id=%s",
                                tool_name,
                                perm_id,
                            )
                            granted = await self._resolve_cloud_permission(
                                perm_id, tool_name, summary, session_id
                            )
                            logger.info(
                                "ACP thin: permission_result sending perm_id=%s granted=%s tool=%s",
                                perm_id,
                                granted,
                                tool_name,
                            )
                            await ws.send(
                                json.dumps(
                                    _cloud_msg(
                                        {
                                            "type": "permission_result",
                                            "permission_id": perm_id,
                                            "granted": granted,
                                        }
                                    )
                                )
                            )

                        elif msg_type == "chunk":
                            await self._conn.session_update(
                                session_id=session_id,
                                update=update_agent_message(text_block(data.get("content", ""))),
                            )

                        elif msg_type == "thinking":
                            t = data.get("content") or ""
                            if t:
                                await self._conn.session_update(
                                    session_id=session_id,
                                    update=update_agent_thought_text(t),
                                )

                        elif msg_type == "tool_call_start":
                            tid = (data.get("tool_call_id") or "").strip()
                            title = data.get("title") or "tool"
                            if tid:
                                await self._conn.session_update(
                                    session_id=session_id,
                                    update=start_tool_call(
                                        tid,
                                        title,
                                        kind="other",
                                        status="in_progress",
                                    ),
                                )

                        elif msg_type == "tool_call_update":
                            tid = (data.get("tool_call_id") or "").strip()
                            title = data.get("title")
                            raw_st = (data.get("status") or "completed").lower()
                            acp_st = "failed" if raw_st == "failed" else "completed"
                            if tid:
                                await self._conn.session_update(
                                    session_id=session_id,
                                    update=update_tool_call(tid, title=title, status=acp_st),
                                )

                        elif msg_type == "execute_tool":
                            tool_name = data.get("name")
                            args = data.get("args", {}) or {}
                            call_id = data.get("call_id")
                            logger.info("Cloud requested IDE tool: %s", tool_name)
                            result = "Not implemented"

                            try:
                                if tool_name == "ide_read_file":
                                    rf_kw: dict[str, Any] = {
                                        "path": args["path"],
                                        "session_id": session_id,
                                    }
                                    if args.get("limit") is not None:
                                        rf_kw["limit"] = int(args["limit"])
                                    if args.get("line") is not None:
                                        rf_kw["line"] = int(args["line"])
                                    res = await self._conn.read_text_file(**rf_kw)
                                    result = res.content
                                elif tool_name == "ide_write_file":
                                    await self._conn.write_text_file(
                                        path=args["path"],
                                        content=args["content"],
                                        session_id=session_id,
                                    )
                                    result = f"File {args['path']} successfully written."
                                elif tool_name == "ide_execute_command":
                                    result = await self._run_shell_command(
                                        args["command"], session_id
                                    )
                                elif tool_name == "ide_create_terminal":
                                    term = await self._conn.create_terminal(
                                        command=args["command"],
                                        args=args.get("args") or [],
                                        cwd=args.get("cwd")
                                        or self._session_cwds.get(session_id),
                                        session_id=session_id,
                                    )
                                    result = json.dumps(
                                        {"terminal_id": term.terminal_id, "ok": True},
                                        ensure_ascii=False,
                                    )
                                elif tool_name == "ide_kill_terminal":
                                    await self._conn.kill_terminal(
                                        session_id=session_id,
                                        terminal_id=args["terminal_id"],
                                    )
                                    result = "Terminal kill requested."
                                elif tool_name == "ide_release_terminal":
                                    await self._conn.release_terminal(
                                        session_id=session_id,
                                        terminal_id=args["terminal_id"],
                                    )
                                    result = "Terminal release requested."
                            except Exception as e:
                                logger.error("IDE tool error: %s", e)
                                result = f"Error executing on IDE: {e}"

                            logger.info(
                                "ACP thin: tool_result sending tool=%s call_id=%s result_len=%s",
                                tool_name,
                                call_id,
                                len(result or ""),
                            )
                            await ws.send(
                                json.dumps(
                                    _cloud_msg(
                                        {
                                            "type": "tool_result",
                                            "call_id": call_id,
                                            "result": result,
                                        }
                                    )
                                )
                            )
                            logger.info(
                                "ACP thin: tool_result sent call_id=%s",
                                call_id,
                            )

                        elif msg_type == "done":
                            logger.info("ACP thin: cloud msg type=done, ending prompt loop")
                            break

                        elif msg_type == "cancelled":
                            logger.info("ACP thin: cloud cancelled")
                            await self._conn.session_update(
                                session_id=session_id,
                                update=update_agent_message(
                                    text_block("\n\n⏹ 云端已取消本次生成。\n\n")
                                ),
                            )
                            final_stop = "cancelled"
                            break

                        elif msg_type == "error":
                            logger.warning(
                                "ACP thin: cloud error: %s",
                                (data.get("content") or "")[:500],
                            )
                            await self._conn.session_update(
                                session_id=session_id,
                                update=update_agent_message(
                                    text_block(f"\n\n❌ 云端服务错误: {data.get('content')}")
                                ),
                            )
                            final_stop = "error"
                            break
                finally:
                    self._active_prompt_ws = None

        except Exception as e:
            logger.exception("WebSocket connection error")
            await self._conn.session_update(
                session_id=session_id,
                update=update_agent_message(
                    text_block(f"\n\n❌ 网络连接失败，请检查 Clawith URL 或网络配置: {e}")
                ),
            )
            return PromptResponse(stop_reason="error")

        return PromptResponse(stop_reason=final_stop)  # type: ignore[arg-type]


async def main():
    agent_id = os.environ.get("CLAWITH_DEFAULT_AGENT_ID", "WL4")
    api_key = os.environ.get("CLAWITH_API_KEY", "")
    backend_url = os.environ.get("CLAWITH_URL", "http://localhost:8008").rstrip("/")

    agent = ClawithThinClientAgent(agent_id=agent_id, api_key=api_key, backend_url=backend_url)
    logger.info("Starting Clawith ACP Thin Client on stdio (unstable ACP routes enabled)...")
    await run_agent(agent, use_unstable_protocol=True)


if __name__ == "__main__":
    asyncio.run(main())
