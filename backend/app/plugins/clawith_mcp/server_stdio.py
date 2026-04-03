# backend/app/plugins/clawith_mcp/server_stdio.py
"""Stdio MCP 服务器，供 Claude Code / Claude Desktop 全局 MCP 配置使用。

用法:
    CLAWITH_URL=http://localhost:8008 CLAWITH_API_KEY=cw-xxx python server_stdio.py

Claude Code ~/.claude/settings.json:
    {
      "mcpServers": {
        "clawith": {
          "command": "/opt/homebrew/bin/node",
          "args": ["/path/to/clawith_mcp/server.py"]
        }
      }
    }
"""
import asyncio
import os

import httpx
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp import types

from app.plugins.clawith_mcp.tools import (
    TOOL_DEFINITIONS,
    http_list_agents,
    http_call_agent,
)

CLAWITH_URL = os.environ.get("CLAWITH_URL", "http://localhost:8008").rstrip("/")
CLAWITH_API_KEY = os.environ.get("CLAWITH_API_KEY", "")
CLAWITH_DEFAULT_AGENT_ID = os.environ.get("CLAWITH_DEFAULT_AGENT_ID", "")

_mcp_app = Server("clawith")


@_mcp_app.list_tools()
async def _list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name=t["name"],
            description=t["description"],
            inputSchema=t["inputSchema"],
        )
        for t in TOOL_DEFINITIONS
    ]


@_mcp_app.call_tool()
async def _call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    if not CLAWITH_API_KEY:
        raise ValueError("CLAWITH_API_KEY 未设置，请在环境变量中配置")
    headers = {"X-Api-Key": CLAWITH_API_KEY, "Content-Type": "application/json"}
    async with httpx.AsyncClient(
        base_url=CLAWITH_URL, headers=headers, timeout=120.0
    ) as client:
        if name == "list_agents":
            text = await http_list_agents(client)
        elif name == "call_agent":
            agent_id = arguments.get("agent_id") or CLAWITH_DEFAULT_AGENT_ID
            text = await http_call_agent(
                client,
                agent_id=agent_id,
                message=arguments["message"],
                session_id=arguments.get("session_id"),
            )
        else:
            raise ValueError(f"未知工具: {name}")
    return [types.TextContent(type="text", text=text)]


async def main() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await _mcp_app.run(
            read_stream, write_stream, _mcp_app.create_initialization_options()
        )


if __name__ == "__main__":
    asyncio.run(main())
