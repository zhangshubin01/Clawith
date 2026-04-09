"""Clawith ACP Thin Client — IDE 侧瘦客户端（JetBrains Agent Client Protocol）

通过 WebSocket 连接云端 Clawith，不访问本地数据库。详见同目录 README.md。

环境变量:
  CLAWITH_URL, CLAWITH_API_KEY, CLAWITH_DEFAULT_AGENT_ID（可选，默认 WL4）
  CLAWITH_WS_PROXY — 见 README
  CLAWITH_ACP_SESSION_UPDATE_TIMEOUT_SEC — IDE `session_update` 超时秒数（默认 120），防止 JetBrains 侧阻塞导致永远「正在等待」
  CLAWITH_ACP_SESSION_UPDATE_SOURCE — 传给 `session_update(..., source=...)` 的标识（默认 clawith-acp-thin），与官方 echo_agent 一致
  CLAWITH_ACP_VERBOSE=1 — 等价于 DEBUG：打印每条云端 chunk 长度、每条 IDE session_update 成功（label/kind/耗时）
  CLAWITH_ACP_LOG_LEVEL — 可选 DEBUG/INFO/WARNING，覆盖默认 INFO（与 VERBOSE 二选一即可）

与云端 WebSocket 的 JSON 信封字段 schemaVersion 须与 Clawith 插件
`backend/app/plugins/clawith_acp/router.py` 中 ACP_WS_SCHEMA_VERSION 一致（当前 v3：`cancelled`、跨连接 cancel）。

排障日志（复现 ACP 卡住时请同时收集）:
  后端: tail -f <项目>/.data/log/backend.log ，过滤含 [ACP] 的行
  瘦客户端: JetBrains 侧 ACP 子进程 stderr，搜 ACP thin: [trace]
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import os
import platform
import sys
import time
from datetime import datetime, timezone as _tz
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlparse
from uuid import uuid4

import websockets
from acp import (
    Agent,
    InitializeResponse,
    NewSessionResponse,
    PromptResponse,
    plan_entry,                  # N1
    run_agent,
    start_edit_tool_call,
    start_read_tool_call,        # N6
    start_tool_call,
    text_block,
    tool_content,                # inline tool results in IDE panel
    tool_diff_content,
    tool_terminal_ref,           # N7
    update_agent_message,
    update_agent_message_text,   # N11
    update_agent_thought_text,
    update_plan,                 # N1
    update_tool_call,
)
from acp.interfaces import Client
from acp.schema import (
    AgentCapabilities,
    AuthenticateResponse,
    AvailableCommand,            # N4
    AvailableCommandsUpdate,     # N4
    ClientCapabilities,
    ConfigOptionUpdate,          # N10
    CloseSessionResponse,
    CurrentModeUpdate,           # N9
    ForkSessionResponse,
    HttpMcpServer,
    Implementation as AgentImplementation,
    ListSessionsResponse,
    LoadSessionResponse,
    McpCapabilities,             # N13
    McpServerStdio,
    PromptCapabilities,
    ResumeSessionResponse,
    SessionCapabilities,         # N5
    SessionCloseCapabilities,    # N5
    SessionForkCapabilities,     # N5
    SessionInfo,
    SessionInfoUpdate,           # N2
    SessionListCapabilities,
    SessionResumeCapabilities,   # N5
    SetSessionConfigOptionResponse,
    SetSessionModeResponse,
    SetSessionModelResponse,
    SseMcpServer,
    ToolCallLocation,            # N8
    UnstructuredCommandInput,    # N4 slash-command hints
    UsageUpdate,                 # N3
)

logger = logging.getLogger("clawith_acp")
handler = logging.StreamHandler(sys.stderr)
handler.setFormatter(logging.Formatter("[ACP Thin Client] %(levelname)s: %(message)s"))
logger.addHandler(handler)
logger.setLevel(logging.INFO)


def _configure_thin_client_logging() -> None:
    """Optional verbose logs for TC-14 style debugging (chunk + session_update trail)."""
    v = (os.environ.get("CLAWITH_ACP_VERBOSE") or "").strip().lower()
    if v in ("1", "true", "yes", "on"):
        logger.setLevel(logging.DEBUG)
        return
    name = (os.environ.get("CLAWITH_ACP_LOG_LEVEL") or "").strip().upper()
    if name and hasattr(logging, name):
        logger.setLevel(getattr(logging, name))


_configure_thin_client_logging()

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


# O7: regex to detect annotated code blocks  ``` lang \n # file: path \n code ```
# Supports both `# file:` (Python/shell) and `// file:` (JS/Go/Java/etc.)
import re as _re
_EDIT_BLOCK_RE = _re.compile(
    r"```(?P<lang>\w*)\n(?:#|//)\s*file:\s*(?P<path>[^\n]+)\n(?P<code>.*?)```",
    _re.DOTALL,
)


def _parse_edit_blocks(text: str) -> list[tuple[str, str]]:
    """O7: Return list of (abs-or-relative path, code) for annotated code blocks."""
    return [
        (m.group("path").strip(), m.group("code"))
        for m in _EDIT_BLOCK_RE.finditer(text)
        if m.group("path").strip()
    ]


def _infer_tool_kind(name: str) -> str:
    """E: Infer ACP ToolKind from tool name for richer IDE visualisation."""
    n = name.lower()
    if any(k in n for k in ("delete", "remove", "rm", "unlink", "drop", "trash")):
        return "delete"
    if any(k in n for k in ("move", "rename", "mv", "copy", "cp")):
        return "move"
    if any(k in n for k in ("search", "find", "grep", "query", "lookup", "list", "glob")):
        return "search"
    if any(k in n for k in ("fetch", "browse", "navigate", "http", "url", "web", "download")):
        return "fetch"
    if any(k in n for k in ("think", "reason", "analyze", "plan", "reflect")):
        return "think"
    if any(k in n for k in ("execute", "run", "shell", "terminal", "cmd", "bash", "command")):
        return "execute"
    if any(k in n for k in ("write", "edit", "create", "update", "patch", "insert", "save")):
        return "edit"
    if any(k in n for k in ("read", "cat", "view", "show", "open", "load", "get")):
        return "read"
    return "other"


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
                _part: dict[str, Any] = {"type": "text", "text": text}
                if d.get("annotations"):  # H: preserve audience/priority metadata
                    _part["annotations"] = d["annotations"]
                wire.append(_part)
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
        # Query params must be percent-encoded (+, &, =, spaces break parsing → false Unauthorized).
        _qe = lambda x: quote(str(x), safe="")
        self.ws_endpoint = (
            f"{ws_base}/api/plugins/clawith-acp/ws"
            f"?agent_id={_qe(agent_id)}&token={_qe(api_key)}"
        )
        # Session cwd from IDE (for create_terminal / shell without explicit cwd)
        self._session_cwds: dict[str, str] = {}
        # O4: fork parent mapping — forked_session_id → parent_session_id
        self._fork_parents: dict[str, str] = {}
        # O5: per-session model override — session_id → model_id (from IDE model picker)
        self._session_models: dict[str, str] = {}
        # N9: per-session mode — session_id → mode_id (from IDE mode switcher)
        self._session_modes: dict[str, str] = {}
        # N10: per-session config options — session_id → (config_id → value)
        self._session_config: dict[str, dict[str, str | bool]] = {}
        # Same WebSocket used for an in-flight `prompt` — used by `cancel` to reach the cloud loop.
        self._active_prompt_ws: Any = None
        self._sessions_titled: set[str] = set()            # N2: sessions already auto-titled
        self._session_mcp_servers: dict[str, list] = {}    # A: mcp_servers per session
        self._ide_session_update_timeout = float(
            os.environ.get("CLAWITH_ACP_SESSION_UPDATE_TIMEOUT_SEC", "120")
        )
        # Match official examples/echo_agent.py: session_update(..., source="…") for IDE routing.
        self._session_update_source = (
            os.environ.get("CLAWITH_ACP_SESSION_UPDATE_SOURCE", "clawith-acp-thin").strip()
            or "clawith-acp-thin"
        )
        logger.info("Initialized Thin Client for agent_id: %s", self.agent_id)

    def on_connect(self, conn: Client) -> None:
        self._conn = conn

    async def _ide_session_update(self, session_id: str, update: Any, label: str) -> None:
        """ACP → IDE UI update with timeout so a stuck IDE never blocks the cloud recv queue."""
        kind = getattr(update, "session_update", None)
        t0 = time.monotonic()
        try:
            await asyncio.wait_for(
                self._conn.session_update(
                    session_id=session_id,
                    update=update,
                    source=self._session_update_source,
                ),
                timeout=self._ide_session_update_timeout,
            )
            dt = time.monotonic() - t0
            logger.debug(
                "ACP thin: ide session_update ok session_id=%s label=%s kind=%s %.3fs",
                session_id,
                label,
                kind,
                dt,
            )
            if dt > 5.0:
                logger.warning(
                    "ACP thin: ide session_update slow session_id=%s label=%s kind=%s %.3fs",
                    session_id,
                    label,
                    kind,
                    dt,
                )
        except asyncio.TimeoutError:
            logger.error(
                "ACP thin: IDE session_update timed out after %ss session_id=%s label=%s kind=%s; "
                "continuing cloud handling",
                self._ide_session_update_timeout,
                session_id,
                label,
                kind,
            )
        except Exception as e:
            logger.error(
                "ACP thin: IDE session_update failed session_id=%s label=%s kind=%s: %s",
                session_id,
                label,
                kind,
                e,
            )

    async def initialize(
        self,
        protocol_version: int,
        client_capabilities: ClientCapabilities | None = None,
        client_info: AgentImplementation | None = None,
        **kwargs: Any,
    ) -> InitializeResponse:
        logger.info("Connected to IDE: %s", client_info)

        # Extend SessionCapabilities to add mode and config capabilities
        # This is needed because ACP 0.9.0 doesn't include these fields in the base class
        # but JetBrains IDE expects them to be present for newer features (mode switching)
        from pydantic import BaseModel
        from typing import Annotated, Optional, Any
        class ExtendedSessionCapabilities(SessionCapabilities):
            mode: Optional[Any] = None
            config: Optional[Any] = None

        # Now add the capabilities if the types exist
        extended_kwargs = {
            "close": SessionCloseCapabilities(),
            "fork": SessionForkCapabilities(),
            "list": SessionListCapabilities(),
            "resume": SessionResumeCapabilities(),
        }

        # Try to add mode capability if available
        try:
            from acp.schema import SessionModeCapabilities
            extended_kwargs["mode"] = SessionModeCapabilities(
                available_modes=["chat", "code-review", "planning"]
            )
            logger.debug("ACP thin: SessionModeCapabilities added with available modes %s", ", ".join(["chat", "code-review", "planning"]))
        except ImportError:
            # If not available, leave as None (still works for JSON serialization)
            pass

        # Try to add config capability if available
        try:
            from acp.schema import ConfigCapabilities
            extended_kwargs["config"] = ConfigCapabilities()
            logger.debug("ACP thin: ConfigCapabilities added")
        except ImportError:
            # If not available, leave as None (still works for JSON serialization)
            pass

        session_capabilities = ExtendedSessionCapabilities(**extended_kwargs)

        return InitializeResponse(
            protocol_version=protocol_version,
            agent_info=AgentImplementation(
                name="clawith-acp-thin",
                version="0.2.0",
                title="Clawith ACP Thin Client",
            ),
            agent_capabilities=AgentCapabilities(
                load_session=True,
                mcp_capabilities=McpCapabilities(http=True, sse=True),  # N13
                prompt_capabilities=PromptCapabilities(
                    image=True,
                    embedded_context=True,
                    audio=False,
                ),
                session_capabilities=session_capabilities,
            ),
        )

    @staticmethod
    def _log_mcp_servers(
        label: str,
        session_id: str,
        mcp_servers: list[HttpMcpServer | SseMcpServer | McpServerStdio] | None,
    ) -> None:
        """O3: Log IDE-provided MCP servers so we know what's available (not yet forwarded to cloud)."""
        if not mcp_servers:
            return
        names = []
        for s in mcp_servers:
            if hasattr(s, "url"):
                names.append(f"{type(s).__name__}({s.url})")
            elif hasattr(s, "command"):
                names.append(f"{type(s).__name__}({s.command})")
            else:
                names.append(type(s).__name__)
        logger.info(
            "ACP thin: %s session_id=%s mcp_servers=[%s] (not yet forwarded to cloud)",
            label,
            session_id,
            ", ".join(names),
        )

    async def _send_available_commands(self, session_id: str) -> None:
        """N4: Advertise slash commands available in this agent session."""
        await self._ide_session_update(
            session_id,
            AvailableCommandsUpdate(
                available_commands=[
                    AvailableCommand(name="clear",  description="清除当前对话历史"),
                    AvailableCommand(name="agent",  description="切换 Clawith 智能体（名称或 UUID）",
                                     input=UnstructuredCommandInput(hint="agent_name_or_uuid")),
                    AvailableCommand(name="model",  description="切换模型（model_id 或智能体名称）",
                                     input=UnstructuredCommandInput(hint="model_id")),
                ],
                session_update="available_commands_update",
            ),
            "available_commands",
        )

    async def new_session(
        self,
        cwd: str,
        mcp_servers: list[HttpMcpServer | SseMcpServer | McpServerStdio] | None = None,
        **kwargs: Any,
    ) -> NewSessionResponse:
        session_id = uuid4().hex
        self._session_cwds[session_id] = cwd or "/"
        if mcp_servers:  # A: store for forwarding in prompt
            self._session_mcp_servers[session_id] = [s.model_dump(by_alias=True, mode="json") for s in mcp_servers]
        logger.info("New session. cwd=%s session_id=%s mcp_servers=%d", cwd, session_id, len(mcp_servers or []))
        self._log_mcp_servers("new_session", session_id, mcp_servers)
        asyncio.create_task(self._send_available_commands(session_id))  # N4
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
        if mcp_servers:  # A
            self._session_mcp_servers[session_id] = [s.model_dump(by_alias=True, mode="json") for s in mcp_servers]
        logger.info("load_session session_id=%s cwd=%s mcp_servers=%d", session_id, cwd, len(mcp_servers or []))
        self._log_mcp_servers("load_session", session_id, mcp_servers)
        asyncio.create_task(self._send_available_commands(session_id))  # N4
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
                        ),
                        ensure_ascii=False,
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
        # O1: clean up all per-session state to prevent unbounded memory growth
        self._session_cwds.pop(session_id, None)
        self._fork_parents.pop(session_id, None)      # O4
        self._session_models.pop(session_id, None)    # O5
        self._session_modes.pop(session_id, None)      # N9
        self._session_config.pop(session_id, None)     # N10
        self._sessions_titled.discard(session_id)     # N2
        self._session_mcp_servers.pop(session_id, None)  # A
        logger.info("close_session session_id=%s (session state cleared)", session_id)
        return CloseSessionResponse()

    async def cancel(self, session_id: str, **kwargs: Any) -> None:
        ws = self._active_prompt_ws
        if ws is None:
            logger.info("cancel session_id=%s (no active cloud prompt)", session_id)
            return
        try:
            await ws.send(
                json.dumps(_cloud_msg({"type": "cancel", "session_id": session_id}), ensure_ascii=False)
            )
            logger.info("cancel session_id=%s sent to cloud", session_id)
        except Exception as e:
            logger.warning("cancel session_id=%s failed: %s", session_id, e)

    async def authenticate(self, method_id: str, **kwargs: Any) -> AuthenticateResponse | None:
        """Clawith 使用 query token；IDE 若走 ACP OAuth 流可后续扩展。"""
        logger.debug("authenticate method_id=%s (noop)", method_id)
        return AuthenticateResponse()

    async def set_session_mode(self, mode_id: str, session_id: str, **kwargs: Any) -> SetSessionModeResponse | None:
        self._session_modes[session_id] = mode_id
        logger.info("set_session_mode session_id=%s mode_id=%s (will forward to cloud on next prompt)", session_id, mode_id)
        # N9: confirm mode switch back to IDE so the UI reflects the change
        await self._ide_session_update(
            session_id,
            CurrentModeUpdate(current_mode_id=mode_id, session_update="current_mode_update"),
            "set_session_mode",
        )
        return SetSessionModeResponse()

    async def set_session_model(self, model_id: str, session_id: str, **kwargs: Any) -> SetSessionModelResponse | None:
        # O5: store model override — will be forwarded on next prompt as agent_override
        self._session_models[session_id] = model_id
        logger.info("set_session_model session_id=%s model_id=%s (will forward to cloud on next prompt)", session_id, model_id)
        return SetSessionModelResponse()

    async def set_config_option(
        self, config_id: str, session_id: str, value: str | bool, **kwargs: Any
    ) -> SetSessionConfigOptionResponse | None:
        logger.debug("set_config_option session_id=%s config_id=%s value=%r", session_id, config_id, value)
        # N10: store config option
        if session_id not in self._session_config:
            self._session_config[session_id] = {}
        self._session_config[session_id][config_id] = value
        # Convert to list of config options for response
        config_options = [
            {"config_id": cid, "value": cval}
            for cid, cval in self._session_config[session_id].items()
        ]
        # N10: echo current config state back to IDE so it reflects the acknowledged change
        await self._ide_session_update(
            session_id,
            ConfigOptionUpdate(
                config_options=config_options,
                session_update="config_option_update"
            ),
            "config_option_update"
        )
        return SetSessionConfigOptionResponse(config_options=config_options)

    async def fork_session(
        self,
        cwd: str,
        session_id: str,
        mcp_servers: list[HttpMcpServer | SseMcpServer | McpServerStdio] | None = None,
        **kwargs: Any,
    ) -> ForkSessionResponse:
        new_id = uuid4().hex
        self._session_cwds[new_id] = self._session_cwds.get(session_id) or cwd or "/"
        self._fork_parents[new_id] = session_id  # O4
        # A: inherit or override mcp_servers for forked session
        if mcp_servers:
            self._session_mcp_servers[new_id] = [s.model_dump(by_alias=True, mode="json") for s in mcp_servers]
        elif session_id in self._session_mcp_servers:
            self._session_mcp_servers[new_id] = self._session_mcp_servers[session_id]
        logger.info("fork_session parent=%s -> new session_id=%s", session_id, new_id)
        return ForkSessionResponse(session_id=new_id)

    async def resume_session(
        self,
        cwd: str,
        session_id: str,
        mcp_servers: list[HttpMcpServer | SseMcpServer | McpServerStdio] | None = None,
        **kwargs: Any,
    ) -> ResumeSessionResponse:
        self._session_cwds[session_id] = cwd or "/"
        if mcp_servers:  # A
            self._session_mcp_servers[session_id] = [s.model_dump(by_alias=True, mode="json") for s in mcp_servers]
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
        return out.output or "Command executed successfully (no output)."

    def _abs_path(self, p: str, session_id: str) -> str:
        """Resolve relative path against session cwd."""
        if not p:
            return p
        if Path(p).is_absolute():
            return p
        cwd = self._session_cwds.get(session_id) or "/"
        return str(Path(cwd) / p)

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
            from acp.contrib.permissions import PermissionBroker, default_permission_options
            from acp.schema import AllowedOutcome, PermissionOption, ToolCallUpdate
            from acp.schema import RequestPermissionRequest, RequestPermissionResponse

            tid = perm_id or uuid4().hex
            description = summary[:2000] if summary else None
            content = None
            raw_input = None

            logger.debug("ACP thin: _resolve_cloud_permission tool=%s summary=%s", tool_name, summary[:100])

            # For ide_write_file: generate diff before asking permission so user can review changes
            if tool_name == "ide_write_file":
                try:
                    args = json.loads(summary)
                    path = args.get("path")
                    new_content = args.get("content")
                    logger.debug("ACP thin: ide_write_file diff generation path=%s content_len=%d", path, len(new_content) if new_content else 0)
                    if path and new_content is not None:
                        abs_path = Path(self._abs_path(path, session_id))
                        old_content: str | None = None
                        # Read directly from local filesystem (thin client runs locally, no need for IDE RPC)
                        # This avoids deadlock where we wait for IDE while IDE waits for us
                        try:
                            if abs_path.exists():
                                logger.debug("ACP thin: reading existing file %s", abs_path)
                                old_content = abs_path.read_text(encoding="utf-8")
                                logger.debug("ACP thin: existing file read ok, %d chars", len(old_content) if old_content else 0)
                        except Exception as e:
                            # File doesn't exist yet (new file) or can't be read - that's ok
                            logger.debug("ACP thin: cannot read existing file %s: %s (new file or non-utf-8)", abs_path, e)
                            pass

                        # Use ACP's built-in tool_diff_content for proper diff rendering in IDE
                        abs_path_str = str(abs_path)
                        content = [tool_diff_content(abs_path_str, new_content, old_content)]
                        description = f"ide_write_file: {path} - Review changes before approve"
                        raw_input = {"path": abs_path_str, "chars": len(new_content)}
                        logger.info("ACP thin: generated diff for ide_write_file %s", path)
                except Exception as e:
                    logger.warning("Failed to generate diff for ide_write_file: %s", e)
                    # Fall back to original summary if diff generation fails
                    pass

            # For delete_file: use kind="delete" with file location (no diff needed)
            _tool_kind = "edit"
            _tool_locations = None
            if tool_name == "delete_file":
                try:
                    args = json.loads(summary)
                    path = args.get("path")
                    if path:
                        abs_path = Path(self._abs_path(path, session_id))
                        abs_path_str = str(abs_path)
                        _tool_kind = "delete"
                        description = f"delete_file: {abs_path_str}"
                        raw_input = {"path": abs_path_str}
                        from acp.schema import ToolCallLocation
                        _tool_locations = [ToolCallLocation(path=abs_path_str)]
                        logger.info("ACP thin: delete_file permission for %s", abs_path_str)
                except Exception as e:
                    logger.warning("Failed to parse delete_file args: %s", e)

            tc = ToolCallUpdate(
                tool_call_id=tid,
                title=tool_name,
                kind=_tool_kind,
                status="pending",
                description=description,
            )
            if _tool_locations is not None:
                try:
                    tc.locations = _tool_locations
                except Exception:
                    pass
            # Some ACP versions don't have content field in ToolCallUpdate, try to add if available
            if content is not None:
                try:
                    tc.content = content
                except Exception:
                    pass
            if raw_input is not None:
                try:
                    tc.raw_input = raw_input
                except Exception:
                    pass

            # PermissionBroker 需要接收单个 RequestPermissionRequest 参数的回调
            # 但 Client.request_permission 接受分开的位置参数，需要做适配器
            async def _requester(req: RequestPermissionRequest) -> RequestPermissionResponse:
                return await self._conn.request_permission(
                    options=req.options,
                    session_id=req.session_id,
                    tool_call=req.tool_call,
                )

            broker = PermissionBroker(session_id, _requester)
            # B: 使用 SDK 提供的完整选项（Approve / Approve for session / Reject）
            # Timeout slightly less than backend's 120s so we can send permission_result before backend times out.
            logger.info("ACP thin: calling broker.request_for tid=%s tool=%s", tid, tool_name)
            try:
                resp = await asyncio.wait_for(
                    broker.request_for(
                        tid,
                        description=description,
                        tool_call=tc,
                        options=list(default_permission_options()),
                    ),
                    timeout=110.0,
                )
            except asyncio.TimeoutError:
                logger.warning("ACP thin: broker.request_for timed out tool=%s tid=%s; denying", tool_name, tid)
                return False
            logger.info("ACP thin: broker.request_for returned tool=%s tid=%s", tool_name, tid)
            out = resp.outcome
            # JetBrains/ACP 版本差异兼容：allow_once 或 allow_always 都视为允许
            selected_id = getattr(out, "selected_id", None) if out is not None else None
            if selected_id is None:
                selected_id = getattr(out, "option_id", None) if out is not None else None
            allowed = (
                isinstance(out, AllowedOutcome)
                or selected_id in ("approve", "approve_for_session", "allow_once")
                or (getattr(out, "outcome", None) == "selected")
                or (getattr(out, "kind", None) in ("allow_once", "allow_always"))
            )
            logger.info(
                "ACP thin: IDE request_permission tool=%s cloud_perm_id=%s allowed=%s "
                "outcome_cls=%s selected_id=%r",
                tool_name,
                perm_id,
                allowed,
                type(out).__name__,
                selected_id,
            )
            return allowed
        except Exception as e:
            logger.exception("IDE permission dialog failed completely (%s); denying cloud tool", e)
            return False

    async def _request_edit_approval(
        self,
        path: str,
        new_content: str,
        old_content: str | None,
        session_id: str,
    ) -> bool:
        """写文件前展示 diff 并请求用户审批。
        CLAWITH_ACP_PERMISSION=allow/deny 可跳过对话框（CI/自动化场景）。
        """
        mode = (os.environ.get("CLAWITH_ACP_PERMISSION") or "ide").strip().lower()
        if mode in ("deny", "reject", "0", "false", "no"):
            return False
        if mode in ("allow", "always", "yes", "1", "true"):
            return True
        try:
            from acp.contrib.permissions import PermissionBroker, default_permission_options
            from acp.schema import AllowedOutcome, ToolCallUpdate
            from acp.schema import RequestPermissionRequest, RequestPermissionResponse

            tid = uuid4().hex
            tc = ToolCallUpdate(
                tool_call_id=tid,
                title=Path(path).name,
                kind="edit",
                status="pending",
                content=[tool_diff_content(path, new_content, old_content)],
                raw_input={"path": path, "chars": len(new_content)},
            )

            async def _requester(req: RequestPermissionRequest) -> RequestPermissionResponse:
                return await self._conn.request_permission(
                    options=req.options,
                    session_id=req.session_id,
                    tool_call=req.tool_call,
                )

            broker = PermissionBroker(session_id, _requester)
            resp = await broker.request_for(
                tid,
                description=f"是否将修改应用到 {path}？",
                tool_call=tc,
                options=list(default_permission_options()),
            )
            out = resp.outcome
            selected_id = getattr(out, "selected_id", None) or getattr(out, "option_id", None)
            allowed = (
                isinstance(out, AllowedOutcome)
                or selected_id in ("approve", "approve_for_session", "allow_once")
                or getattr(out, "outcome", None) == "selected"
                or getattr(out, "kind", None) in ("allow_once", "allow_always")
            )
            logger.info(
                "ACP thin: edit approval path=%s allowed=%s outcome_cls=%s selected_id=%r",
                path, allowed, type(out).__name__, selected_id,
            )
            return allowed
        except Exception as e:
            logger.warning("ide_write_file approval dialog failed (%s); allowing write", e)
            return True  # 对话框失败时默认允许，避免阻塞 AI 流程

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
        await self._ide_session_update(
            session_id,
            update_agent_message_text("⏳ 连接云端 Clawith 智能体进行处理...\n\n"),
            "prompt_connecting",
        )

        final_stop: str = "end_turn"
        _chunks: list[str] = []  # O7: accumulate full response for edit-block post-processing
        _plan_entries: list[tuple[str, str, str]] = []   # N1: (tid, title, status)
        _tool_call_id_by_name: dict[str, str] = {}        # N7/N8: tool_name → latest ACP tool_call_id
        _active_perm_tasks: dict[str, asyncio.Task] = {}  # perm_id → _bg_permission task (for cancellation)
        try:
            proxy_kw = _websocket_proxy_kw()
            if proxy_kw.get("proxy") is None:
                logger.info(
                    "WebSocket: direct (no system proxy); set CLAWITH_WS_PROXY=auto for env-based proxy"
                )
            async with websockets.connect(self.ws_endpoint, **proxy_kw) as ws:
                self._active_prompt_ws = ws
                reader_task: asyncio.Task[None] | None = None
                try:
                    # IDE updates run in a worker so the main loop can keep draining `inbound`
                    # (permission / execute_tool / done) while JetBrains applies chunks in order.
                    ide_q: asyncio.Queue[Any] = asyncio.Queue()

                    _ide_chunk_count = 0

                    async def _ide_emit_worker() -> None:
                        nonlocal _ide_chunk_count
                        while True:
                            item = await ide_q.get()
                            lab_i = "?"
                            try:
                                if item is None:
                                    break
                                sid_i, upd_i, lab_i = item
                                if lab_i == "chunk":
                                    _ide_chunk_count += 1
                                    n_c = _ide_chunk_count
                                    if n_c == 1 or n_c % 25 == 0:
                                        logger.info(
                                            "ACP thin: [trace] ide_worker chunk n=%d session_id=%s",
                                            n_c,
                                            session_id,
                                        )
                                else:
                                    logger.info(
                                        "ACP thin: [trace] ide_worker START label=%s session_id=%s",
                                        lab_i,
                                        session_id,
                                    )
                                await self._ide_session_update(sid_i, upd_i, lab_i)
                                if lab_i == "chunk":
                                    n_c = _ide_chunk_count
                                    if n_c == 1 or n_c % 25 == 0:
                                        logger.info(
                                            "ACP thin: [trace] ide_worker chunk DONE n=%d session_id=%s",
                                            n_c,
                                            session_id,
                                        )
                                else:
                                    logger.info(
                                        "ACP thin: [trace] ide_worker END label=%s session_id=%s",
                                        lab_i,
                                        session_id,
                                    )
                            except asyncio.CancelledError:
                                raise
                            except Exception as _iw_e:
                                logger.exception(
                                    "ACP thin: [trace] ide_worker FAIL label=%s: %s",
                                    lab_i,
                                    _iw_e,
                                )
                            finally:
                                ide_q.task_done()

                    ide_worker = asyncio.create_task(_ide_emit_worker())

                    async def _enqueue_ide(update: Any, label: str) -> None:
                        await ide_q.put((session_id, update, label))

                    async def _trace_join(where: str) -> None:
                        logger.info(
                            "ACP thin: [trace] ide_q.join ENTER %s ide_qsize=%s session_id=%s",
                            where,
                            ide_q.qsize(),
                            session_id,
                        )
                        await ide_q.join()
                        logger.info(
                            "ACP thin: [trace] ide_q.join LEAVE %s session_id=%s",
                            where,
                            session_id,
                        )

                    prompt_body: dict[str, Any] = {
                        "type": "prompt",
                        "session_id": session_id,
                        "cwd": self._session_cwds.get(session_id) or "/",
                    }
                    # O4: tell cloud to copy parent history on first prompt of a forked session
                    _parent = self._fork_parents.pop(session_id, None)
                    if _parent:
                        prompt_body["parent_session_id"] = _parent
                        logger.info("ACP thin: fork prompt session_id=%s parent=%s", session_id, _parent)
                    # O5: forward model override so cloud uses the right agent
                    _model = self._session_models.get(session_id)
                    if _model:
                        prompt_body["agent_override"] = _model
                    # N9: forward session mode so cloud uses the right mode
                    _mode = self._session_modes.get(session_id)
                    if _mode:
                        prompt_body["mode"] = _mode
                    # N10: forward session config options so cloud applies settings
                    _config = self._session_config.get(session_id)
                    if _config:
                        prompt_body["config"] = _config
                    # A: forward IDE-declared MCP servers so cloud knows what's available
                    _mcp = self._session_mcp_servers.get(session_id)
                    if _mcp:
                        prompt_body["mcp_servers"] = _mcp
                        logger.info("ACP thin: forwarding mcp_servers=%d session_id=%s", len(_mcp), session_id)
                    if wire_parts:
                        prompt_body["prompt_parts"] = wire_parts
                    else:
                        prompt_body["text"] = user_text
                    await ws.send(json.dumps(_cloud_msg(prompt_body), ensure_ascii=False))

                    # Dedicated reader so IDE-bound `session_update` never blocks `recv()`.
                    # Otherwise the cloud may stall sending `chunk`/`done` (TCP backpressure),
                    # leaving the chat stuck on "Waiting..." after tools complete.
                    inbound: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()

                    async def _acp_ws_reader() -> None:
                        try:
                            while True:
                                raw = await ws.recv()
                                await inbound.put(json.loads(raw))
                        except asyncio.CancelledError:
                            raise
                        except Exception as e:
                            logger.exception("ACP thin WebSocket reader error: %s", e)
                            await inbound.put(
                                _cloud_msg(
                                    {
                                        "type": "_acp_reader_error",
                                        "content": str(e),
                                    }
                                )
                            )
                        finally:
                            await inbound.put(None)

                    reader_task = asyncio.create_task(_acp_ws_reader())

                    while True:
                        data = await inbound.get()
                        if data is None:
                            logger.info("ACP thin: inbound closed (WS reader ended)")
                            break
                        _check_server_schema(data)
                        msg_type = data.get("type")
                        _chunk_len = (
                            len(data.get("content") or "")
                            if msg_type == "chunk"
                            else -1
                        )
                        logger.info(
                            "ACP thin: [trace] cloud→handler type=%s session_id=%s chunk_len=%s ide_qsize=%s",
                            msg_type,
                            session_id,
                            _chunk_len if msg_type == "chunk" else "-",
                            ide_q.qsize(),
                        )

                        if msg_type == "_acp_reader_error":
                            await _trace_join("cloud_reader_error")
                            await self._ide_session_update(
                                session_id,
                                update_agent_message_text(f"\n\n❌ 云端连接中断: {data.get('content', '')}\n\n"),
                                "cloud_reader_error",
                            )
                            final_stop = "error"
                            break

                        if msg_type == "permission_request":
                            await _trace_join("permission_request")
                            _pid = data.get("permission_id")
                            perm_id_str = str(_pid) if _pid is not None else ""
                            _tool_name_perm = data.get("tool_name") or "ide_tool"
                            _summary_perm = data.get("args_summary") or ""
                            logger.info(
                                "ACP thin: permission_request session_id=%s tool=%s perm_id=%s",
                                session_id,
                                _tool_name_perm,
                                perm_id_str,
                            )

                            # Handle permission in a background task so the inbound queue keeps
                            # draining. This prevents a deadlock when the backend/web-UI resolves
                            # the permission first and sends execute_tool before we finish here.
                            async def _bg_permission(
                                _pid_str=perm_id_str,
                                _tname=_tool_name_perm,
                                _summ=_summary_perm,
                                _ws=ws,
                            ) -> None:
                                _granted = False
                                try:
                                    _granted = await self._resolve_cloud_permission(
                                        _pid_str or None, _tname, _summ, session_id
                                    )
                                except Exception as _e:
                                    logger.error("ACP thin: _resolve_cloud_permission failed: %s", _e)
                                logger.info(
                                    "ACP thin: permission_result bg perm_id=%s granted=%s tool=%s",
                                    _pid_str,
                                    _granted,
                                    _tname,
                                )
                                try:
                                    await _ws.send(
                                        json.dumps(
                                            _cloud_msg(
                                                {
                                                    "type": "permission_result",
                                                    "permission_id": _pid_str,
                                                    "granted": _granted,
                                                }
                                            ),
                                            ensure_ascii=False,
                                        )
                                    )
                                except Exception as _se:
                                    logger.debug("ACP thin: permission_result send failed: %s", _se)

                            _perm_task = asyncio.create_task(_bg_permission())
                            _active_perm_tasks[perm_id_str] = _perm_task
                            _perm_task.add_done_callback(
                                lambda _t, _k=perm_id_str: _active_perm_tasks.pop(_k, None)
                            )

                        elif msg_type == "chunk":
                            _chunk_text = data.get("content", "")
                            _chunks.append(_chunk_text)  # O7: accumulate
                            await _enqueue_ide(
                                update_agent_message_text(_chunk_text),
                                "chunk",
                            )

                        elif msg_type == "thinking":
                            t = data.get("content") or ""
                            if t:
                                await _enqueue_ide(
                                    update_agent_thought_text(t),
                                    "thinking",
                                )

                        elif msg_type == "tool_call_start":
                            tid = (data.get("tool_call_id") or "").strip()
                            title = data.get("title") or "tool"
                            if tid:
                                # N6: use specialised helpers / kinds per tool type
                                _tool_name_hint = title.lower()
                                if _tool_name_hint == "ide_read_file":
                                    _tc_start = start_read_tool_call(tid, title, "")
                                elif _tool_name_hint in ("ide_write_file", "delete_file"):
                                    _tc_start = start_tool_call(tid, title, kind="edit", status="in_progress")
                                elif _tool_name_hint in ("ide_execute_command", "ide_create_terminal",
                                                         "ide_kill_terminal", "ide_release_terminal",
                                                         "ide_terminal_output"):
                                    _tc_start = start_tool_call(tid, title, kind="execute", status="in_progress")
                                else:
                                    _tc_start = start_tool_call(tid, title, kind=_infer_tool_kind(_tool_name_hint), status="in_progress")  # E
                                await _enqueue_ide(_tc_start, "tool_call_start")
                                # N1: add plan entry; N7/N8: track ACP tid by tool name
                                _plan_entries.append((tid, title, "in_progress"))
                                await _enqueue_ide(
                                    update_plan([plan_entry(t, status=s) for _, t, s in _plan_entries]),
                                    "plan_update",
                                )
                                _tool_call_id_by_name[_tool_name_hint] = tid

                        elif msg_type == "tool_call_update":
                            tid = (data.get("tool_call_id") or "").strip()
                            title = data.get("title")
                            raw_st = (data.get("status") or "completed").lower()
                            acp_st = "failed" if raw_st == "failed" else "completed"
                            if tid:
                                await _enqueue_ide(
                                    update_tool_call(tid, title=title, status=acp_st),
                                    "tool_call_update",
                                )
                                # N1: update plan entry status
                                for _pi, (_ptid, _ptitle, _pst) in enumerate(_plan_entries):
                                    if _ptid == tid:
                                        _plan_entries[_pi] = (_ptid, _ptitle, acp_st)
                                        break
                                if _plan_entries:
                                    await _enqueue_ide(
                                        update_plan([plan_entry(t, status=s) for _, t, s in _plan_entries]),
                                        "plan_update",
                                    )

                        elif msg_type == "available_commands":
                            # F: backend can push dynamic command list for this agent
                            _cmds_raw = data.get("commands") or []
                            _cmds = [
                                AvailableCommand(name=c["name"], description=c.get("description", ""))
                                for c in _cmds_raw if isinstance(c, dict) and c.get("name")
                            ]
                            if _cmds:
                                await _enqueue_ide(
                                    AvailableCommandsUpdate(available_commands=_cmds, session_update="available_commands_update"),
                                    "available_commands_dyn",
                                )

                        elif msg_type == "usage":
                            # N3: forward token-usage stats to IDE so it can show context meter
                            _used_tok = int(data.get("used") or 0)
                            _size_tok = int(data.get("size") or 200000)
                            await _enqueue_ide(UsageUpdate(used=_used_tok, size=_size_tok, session_update="usage_update"), "usage_update")

                        elif msg_type == "execute_tool":
                            await _trace_join("execute_tool")
                            tool_name = data.get("name")
                            args = data.get("args", {}) or {}
                            call_id = data.get("call_id")
                            call_id_str = str(call_id) if call_id is not None else ""
                            logger.info(
                                "ACP thin: execute_tool session_id=%s name=%s call_id=%s",
                                session_id,
                                tool_name,
                                call_id,
                            )
                            result = "Not implemented"
                            _write_old_text: str | None = None  # captured before ide_write_file overwrites
                            _write_approved: bool = True        # set to False when user rejects edit

                            def _abs(p: str) -> str:
                                """Resolve relative path against session cwd."""
                                return self._abs_path(p, session_id)

                            try:
                                if tool_name == "ide_read_file":
                                    abs_path = _abs(args["path"])
                                    rf_kw: dict[str, Any] = {
                                        "path": abs_path,
                                        "session_id": session_id,
                                    }
                                    if args.get("limit") is not None:
                                        rf_kw["limit"] = int(args["limit"])
                                    if args.get("line") is not None:
                                        rf_kw["line"] = int(args["line"])
                                    res = await self._conn.read_text_file(**rf_kw)
                                    result = res.content
                                elif tool_name == "ide_write_file":
                                    abs_path = _abs(args["path"])
                                    # Permission was already granted by backend via permission_request/result
                                    # before execute_tool was sent — write directly without asking again.
                                    #
                                    # Cancel any pending _bg_permission tasks BEFORE calling write_text_file.
                                    # If the web UI approved first while the IDE dialog was still open,
                                    # IntelliJ's modal blocks all ACP requests (including write_text_file),
                                    # causing a deadlock. Cancelling the task triggers CancelledError in
                                    # the pending request_permission call, which dismisses the IDE dialog.
                                    if _active_perm_tasks:
                                        for _pt in list(_active_perm_tasks.values()):
                                            if not _pt.done():
                                                _pt.cancel()
                                                logger.info(
                                                    "ACP thin: cancelled pending permission task to unblock ide_write_file"
                                                )
                                        _active_perm_tasks.clear()
                                    logger.info(
                                        "ACP thin: execute_tool ide_write_file path=%s session_id=%s",
                                        abs_path,
                                        session_id,
                                    )
                                    try:
                                        await asyncio.wait_for(
                                            self._conn.write_text_file(
                                                path=abs_path,
                                                content=args["content"],
                                                session_id=session_id,
                                            ),
                                            timeout=90.0,
                                        )
                                    except asyncio.TimeoutError:
                                        raise RuntimeError(
                                            f"ide_write_file timed out after 90s (path: {abs_path})"
                                        )
                                    result = f"File {abs_path} successfully written."
                                elif tool_name == "ide_execute_command":
                                    result = await self._run_shell_command(
                                        args["command"], session_id
                                    )
                                elif tool_name == "ide_create_terminal":
                                    _ct_kw: dict[str, Any] = {
                                        "command": args["command"],
                                        "args": args.get("args") or [],
                                        "cwd": args.get("cwd") or self._session_cwds.get(session_id),
                                        "session_id": session_id,
                                    }
                                    if args.get("env"):  # N14: dict → EnvVariable list
                                        from acp.schema import EnvVariable
                                        _ct_kw["env"] = [
                                            EnvVariable(name=k, value=str(v))
                                            for k, v in args["env"].items()
                                        ]
                                    if args.get("output_byte_limit") is not None:  # N14
                                        _ct_kw["output_byte_limit"] = int(args["output_byte_limit"])
                                    term = await self._conn.create_terminal(**_ct_kw)
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
                                elif tool_name == "ide_terminal_output":
                                    out = await self._conn.terminal_output(
                                        terminal_id=args["terminal_id"],
                                        session_id=session_id,
                                    )
                                    result = out.output or "(no output yet)"
                                    if out.truncated:
                                        result += "\n[输出已截断]"
                                    if out.exit_status is not None:
                                        _ec = out.exit_status.exit_code
                                        _sig = out.exit_status.signal
                                        if _sig:
                                            result += f"\n[信号: {_sig}]"
                                        elif _ec is not None:
                                            result += f"\n[退出码: {_ec}]"
                                elif tool_name == "delete_file":
                                    abs_path = _abs(args["path"])
                                    abs_path_obj = Path(abs_path)
                                    logger.info(
                                        "ACP thin: delete_file executing path=%s session_id=%s",
                                        abs_path, session_id,
                                    )
                                    # Delete the file from local filesystem
                                    # ACP delete_textFile in IDE may just clear content, we do actual rm
                                    import os
                                    if abs_path_obj.exists():
                                        os.remove(abs_path_obj)
                                        logger.info(
                                            "ACP thin: delete_file removed path=%s session_id=%s",
                                            abs_path, session_id,
                                        )
                                        result = f"File {abs_path} successfully deleted from filesystem."
                                    else:
                                        result = f"File {abs_path} does not exist."
                            except Exception as e:
                                logger.error("IDE tool error: %s", e)
                                result = f"Error executing on IDE: {e}"

                            # N7: terminal_ref; N8: file diff/location; C: raw_input; D: line number
                            _acp_tid = _tool_call_id_by_name.get(tool_name)
                            if _acp_tid:
                                if tool_name == "ide_create_terminal":
                                    with contextlib.suppress(Exception):
                                        _r = json.loads(result)
                                        if isinstance(_r, dict) and _r.get("terminal_id"):
                                            await _enqueue_ide(
                                                update_tool_call(
                                                    _acp_tid,
                                                    content=[tool_terminal_ref(_r["terminal_id"])],
                                                    raw_input={"command": args.get("command"), "args": args.get("args", [])},  # C
                                                ),
                                                "terminal_ref",
                                            )
                                elif tool_name in ("ide_read_file", "ide_write_file", "delete_file"):
                                    _fp = _abs(args.get("path", ""))
                                    if _fp:
                                        with contextlib.suppress(Exception):
                                            if tool_name == "ide_write_file":
                                                if _write_approved:
                                                    # 审批通过：在工具面板展示最终 diff
                                                    await _enqueue_ide(
                                                        update_tool_call(
                                                            _acp_tid,
                                                            content=[tool_diff_content(_fp, args.get("content", ""), _write_old_text)],
                                                            locations=[ToolCallLocation(path=_fp)],
                                                            raw_input={"path": _fp, "chars": len(args.get("content", ""))},
                                                            raw_output=result,
                                                        ),
                                                        "file_diff",
                                                    )
                                                else:
                                                    # 审批拒绝：标记工具调用失败
                                                    await _enqueue_ide(
                                                        update_tool_call(
                                                            _acp_tid,
                                                            status="failed",
                                                            raw_output=result,
                                                        ),
                                                        "file_diff_rejected",
                                                    )
                                            elif tool_name == "delete_file":
                                                # Delete already done; just show file location
                                                await _enqueue_ide(
                                                    update_tool_call(
                                                        _acp_tid,
                                                        locations=[ToolCallLocation(path=_fp)],
                                                        raw_input={"path": _fp},
                                                    ),
                                                    "file_location",
                                                )
                                            elif tool_name == "ide_read_file":
                                                _line = int(args["line"]) if args.get("line") is not None else None  # D
                                                _read_preview = (result or "")[:4000]
                                                await _enqueue_ide(
                                                    update_tool_call(
                                                        _acp_tid,
                                                        content=[tool_content(text_block(_read_preview))] if _read_preview else None,
                                                        locations=[ToolCallLocation(path=_fp, line=_line)],
                                                        raw_input={"path": _fp},  # C
                                                        raw_output=_read_preview or None,
                                                    ),
                                                    "file_location",
                                                )
                                elif tool_name == "ide_terminal_output":
                                    # Show terminal output inline in IDE tool panel
                                    with contextlib.suppress(Exception):
                                        _term_preview = (result or "")[:4000]
                                        await _enqueue_ide(
                                            update_tool_call(
                                                _acp_tid,
                                                content=[tool_content(text_block(_term_preview))] if _term_preview else None,
                                                raw_input={"terminal_id": args.get("terminal_id")},
                                                raw_output=_term_preview or None,
                                            ),
                                            "terminal_output",
                                        )
                                elif args:  # C: raw_input for all other tools
                                    with contextlib.suppress(Exception):
                                        await _enqueue_ide(
                                            update_tool_call(_acp_tid, raw_input=args),
                                            "raw_input",
                                        )

                            logger.info(
                                "ACP thin: tool_result sending session_id=%s tool=%s call_id=%s result_len=%s",
                                session_id,
                                tool_name,
                                call_id_str or call_id,
                                len(result or ""),
                            )
                            await ws.send(
                                json.dumps(
                                    _cloud_msg(
                                        {
                                            "type": "tool_result",
                                            "call_id": call_id_str or call_id,
                                            "result": result,
                                        }
                                    )
                                )
                            )
                            logger.info(
                                "ACP thin: tool_result sent session_id=%s call_id=%s",
                                session_id,
                                call_id_str or call_id,
                            )

                        elif msg_type == "done":
                            await _trace_join("done")
                            logger.info(
                                "ACP thin: [trace] received DONE; breaking prompt loop session_id=%s ide_chunks_delivered=%d",
                                session_id,
                                _ide_chunk_count,
                            )
                            # O7: post-process full response for annotated edit blocks
                            _full_text = "".join(_chunks)
                            _edits = _parse_edit_blocks(_full_text)
                            if _edits:
                                logger.info(
                                    "ACP thin: O7 found %d edit block(s) in response session_id=%s",
                                    len(_edits),
                                    session_id,
                                )
                            for _ei, (_raw_path, _code) in enumerate(_edits):
                                _cwd = self._session_cwds.get(session_id) or "/"
                                _abs_path = _raw_path if Path(_raw_path).is_absolute() else str(Path(_cwd) / _raw_path)
                                _tid = f"edit_{session_id[:8]}_{_ei}"
                                _title = Path(_abs_path).name
                                # Read existing file to provide old_text for proper diff window
                                _o7_old: str | None = None
                                with contextlib.suppress(Exception):
                                    _o7_rf = await self._conn.read_text_file(path=_abs_path, session_id=session_id)
                                    _o7_old = _o7_rf.content
                                try:
                                    await _enqueue_ide(
                                        start_edit_tool_call(
                                            _tid, _title, _abs_path,
                                            tool_diff_content(_abs_path, _code, _o7_old),
                                        ),
                                        f"edit_start_{_ei}",
                                    )
                                    await _enqueue_ide(
                                        update_tool_call(_tid, status="completed"),
                                        f"edit_done_{_ei}",
                                    )
                                    logger.info(
                                        "ACP thin: O7 edit_block sent path=%s tid=%s",
                                        _abs_path, _tid,
                                    )
                                except Exception as _oe:
                                    logger.warning("ACP thin: O7 edit_block failed path=%s: %s", _abs_path, _oe)
                            await _trace_join("done_edits")
                            # N2: auto-update session title on first response
                            if _full_text and session_id not in self._sessions_titled:
                                _raw_title = _full_text[:120].split('\n')[0][:60].strip()
                                _title = _raw_title.lstrip('#> *').strip()
                                if _title:
                                    self._sessions_titled.add(session_id)
                                    _now_iso = datetime.now(_tz.utc).isoformat().replace('+00:00', 'Z')
                                    await _enqueue_ide(
                                        SessionInfoUpdate(title=_title, updated_at=_now_iso, session_update="session_info_update"),
                                        "session_info",
                                    )
                            break

                        elif msg_type == "cancelled":
                            logger.info("ACP thin: cloud cancelled")
                            await _trace_join("cancelled")
                            await self._ide_session_update(
                                session_id,
                                update_agent_message_text("\n\n⏹ 云端已取消本次生成。\n\n"),
                                "cancelled",
                            )
                            final_stop = "cancelled"
                            break

                        elif msg_type == "error":
                            logger.warning(
                                "ACP thin: cloud error: %s",
                                (data.get("content") or "")[:500],
                            )
                            await _trace_join("cloud_error")
                            await self._ide_session_update(
                                session_id,
                                update_agent_message_text(f"\n\n❌ 云端服务错误: {data.get('content')}"),
                                "cloud_error",
                            )
                            final_stop = "error"
                            break
                finally:
                    logger.info(
                        "ACP thin: [trace] prompt ws cleanup session_id=%s",
                        session_id,
                    )
                    with contextlib.suppress(Exception):
                        await ide_q.put(None)
                        await ide_worker
                    logger.info(
                        "ACP thin: [trace] prompt ws cleanup done session_id=%s",
                        session_id,
                    )
                    if reader_task is not None:
                        reader_task.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await reader_task
                    self._active_prompt_ws = None

        except Exception as e:
            logger.exception("WebSocket connection error")
            await self._ide_session_update(
                session_id,
                update_agent_message_text(f"\n\n❌ 网络连接失败，请检查 Clawith URL 或网络配置: {e}"),
                "ws_connect_error",
            )
            return PromptResponse(stop_reason="error")

        logger.info(
            "ACP thin: [trace] prompt RETURN stop_reason=%s session_id=%s",
            final_stop,
            session_id,
        )
        return PromptResponse(stop_reason=final_stop)  # type: ignore[arg-type]


async def main():
    agent_id = os.environ.get("CLAWITH_DEFAULT_AGENT_ID", "WL4")
    api_key = (os.environ.get("CLAWITH_API_KEY") or "").strip()
    backend_url = os.environ.get("CLAWITH_URL", "http://localhost:8008").rstrip("/")

    if not api_key:
        logger.error(
            "CLAWITH_API_KEY is empty — cloud WebSocket will return Unauthorized. "
            "Set API key from Clawith Web (cw-…) in acp.json env or shell."
        )
    agent = ClawithThinClientAgent(agent_id=agent_id, api_key=api_key, backend_url=backend_url)
    logger.info("Starting Clawith ACP Thin Client on stdio (unstable ACP routes enabled)...")
    await run_agent(agent, use_unstable_protocol=True)


if __name__ == "__main__":
    asyncio.run(main())
