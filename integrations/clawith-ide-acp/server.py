"""Clawith ACP Thin Client — IDE 侧瘦客户端（JetBrains Agent Client Protocol）

通过 WebSocket 连接云端 Clawith，不访问本地数据库。详见同目录 README.md。

环境变量:
  CLAWITH_URL, CLAWITH_API_KEY, CLAWITH_DEFAULT_AGENT_ID（可选，默认 WL4）
  CLAWITH_WS_PROXY — 见 README
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import platform
import sys
from typing import Any
from uuid import uuid4

import websockets
from acp import (
    Agent,
    InitializeResponse,
    NewSessionResponse,
    PromptResponse,
    run_agent,
    text_block,
    update_agent_message,
)
from acp.interfaces import Client
from acp.schema import (
    ClientCapabilities,
    HttpMcpServer,
    Implementation,
    McpServerStdio,
    SseMcpServer,
)

logger = logging.getLogger("clawith_acp")
handler = logging.StreamHandler(sys.stderr)
handler.setFormatter(logging.Formatter("[ACP Thin Client] %(levelname)s: %(message)s"))
logger.addHandler(handler)
logger.setLevel(logging.INFO)

_IS_WINDOWS = platform.system().lower().startswith("win")


def _websocket_proxy_kw() -> dict[str, Any]:
    """websockets 15+ 默认会读系统代理；SOCKS 需 python-socks。直连 Clawith 建议 proxy=None。"""
    raw = (os.environ.get("CLAWITH_WS_PROXY") or "").strip().lower()
    if raw in ("", "none", "direct", "0", "false", "off"):
        return {"proxy": None}
    if raw in ("auto", "env", "1", "true", "yes", "on"):
        return {"proxy": True}
    return {"proxy": os.environ.get("CLAWITH_WS_PROXY", "").strip()}


class ClawithThinClientAgent(Agent):
    _conn: Client

    def __init__(self, agent_id: str, api_key: str, backend_url: str):
        super().__init__()
        self.agent_id = agent_id
        self.api_key = api_key
        ws_base = backend_url.replace("http://", "ws://").replace("https://", "wss://")
        self.ws_endpoint = f"{ws_base}/api/plugins/clawith-acp/ws?agent_id={agent_id}&token={api_key}"
        logger.info("Initialized Thin Client for agent_id: %s", self.agent_id)

    def on_connect(self, conn: Client) -> None:
        self._conn = conn

    async def initialize(
        self,
        protocol_version: int,
        client_capabilities: ClientCapabilities | None = None,
        client_info: Implementation | None = None,
        **kwargs: Any,
    ) -> InitializeResponse:
        logger.info("Connected to IDE: %s", client_info)
        return InitializeResponse(protocol_version=protocol_version)

    async def new_session(
        self, cwd: str, mcp_servers: list[HttpMcpServer | SseMcpServer | McpServerStdio], **kwargs: Any
    ) -> NewSessionResponse:
        session_id = uuid4().hex
        logger.info("New session. cwd=%s session_id=%s", cwd, session_id)
        return NewSessionResponse(session_id=session_id)

    async def _run_shell_command(self, user_command: str, session_id: str) -> str:
        """macOS/Linux: /bin/sh -c；Windows: cmd.exe /c。"""
        if _IS_WINDOWS:
            term = await self._conn.create_terminal(
                command="cmd.exe",
                args=["/c", user_command],
                session_id=session_id,
            )
        else:
            term = await self._conn.create_terminal(
                command="/bin/sh",
                args=["-c", user_command],
                session_id=session_id,
            )
        await self._conn.wait_for_terminal_exit(terminal_id=term.id, session_id=session_id)
        out = await self._conn.terminal_output(terminal_id=term.id, session_id=session_id)
        return out.content or "Command executed successfully (no output)."

    async def prompt(
        self,
        prompt: list[Any],
        session_id: str,
        **kwargs: Any,
    ) -> PromptResponse:
        user_text = ""
        for block in prompt:
            text = block.get("text", "") if isinstance(block, dict) else getattr(block, "text", "")
            if text:
                user_text += text

        logger.info("Forwarding prompt to cloud: %s...", user_text[:100])
        await self._conn.session_update(
            session_id=session_id,
            update=update_agent_message(text_block("⏳ 连接云端 Clawith 智能体进行处理...\n\n")),
        )

        try:
            proxy_kw = _websocket_proxy_kw()
            if proxy_kw.get("proxy") is None:
                logger.info(
                    "WebSocket: direct (no system proxy); set CLAWITH_WS_PROXY=auto for env-based proxy"
                )
            async with websockets.connect(self.ws_endpoint, **proxy_kw) as ws:
                await ws.send(
                    json.dumps({"type": "prompt", "text": user_text, "session_id": session_id})
                )

                while True:
                    msg = await ws.recv()
                    data = json.loads(msg)
                    msg_type = data.get("type")

                    if msg_type == "chunk":
                        await self._conn.session_update(
                            session_id=session_id,
                            update=update_agent_message(text_block(data.get("content", ""))),
                        )

                    elif msg_type == "execute_tool":
                        tool_name = data.get("name")
                        args = data.get("args", {}) or {}
                        call_id = data.get("call_id")
                        logger.info("Cloud requested IDE tool: %s", tool_name)
                        result = "Not implemented"

                        try:
                            if tool_name == "ide_read_file":
                                res = await self._conn.read_text_file(path=args["path"], session_id=session_id)
                                result = res.content
                            elif tool_name == "ide_write_file":
                                await self._conn.write_text_file(
                                    path=args["path"], content=args["content"], session_id=session_id
                                )
                                result = f"File {args['path']} successfully written."
                            elif tool_name == "ide_execute_command":
                                result = await self._run_shell_command(args["command"], session_id)
                        except Exception as e:
                            logger.error("IDE tool error: %s", e)
                            result = f"Error executing on IDE: {e}"

                        await ws.send(
                            json.dumps({"type": "tool_result", "call_id": call_id, "result": result})
                        )

                    elif msg_type == "done":
                        break

                    elif msg_type == "error":
                        await self._conn.session_update(
                            session_id=session_id,
                            update=update_agent_message(
                                text_block(f"\n\n❌ 云端服务错误: {data.get('content')}")
                            ),
                        )
                        break

        except Exception as e:
            logger.exception("WebSocket connection error")
            await self._conn.session_update(
                session_id=session_id,
                update=update_agent_message(
                    text_block(f"\n\n❌ 网络连接失败，请检查 Clawith URL 或网络配置: {e}")
                ),
            )
            return PromptResponse(stop_reason="error")

        return PromptResponse(stop_reason="end_turn")


async def main():
    agent_id = os.environ.get("CLAWITH_DEFAULT_AGENT_ID", "WL4")
    api_key = os.environ.get("CLAWITH_API_KEY", "")
    backend_url = os.environ.get("CLAWITH_URL", "http://localhost:8008").rstrip("/")

    agent = ClawithThinClientAgent(agent_id=agent_id, api_key=api_key, backend_url=backend_url)
    logger.info("Starting Clawith ACP Thin Client on stdio...")
    await run_agent(agent)


if __name__ == "__main__":
    asyncio.run(main())
