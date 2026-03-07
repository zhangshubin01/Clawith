"""MCP (Model Context Protocol) Client — connects to external MCP servers.

Supports the Streamable HTTP transport (the modern standard).
Reference: https://modelcontextprotocol.io/docs
"""

import httpx
import json
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse


class MCPClient:
    """Client for connecting to MCP servers via HTTP+SSE transport."""

    def __init__(self, server_url: str):
        # Extract apiKey from URL query params and move to Authorization header
        parsed = urlparse(server_url)
        qs = parse_qs(parsed.query, keep_blank_values=True)

        self.api_key = None
        if "apiKey" in qs:
            self.api_key = qs.pop("apiKey")[0]

        # Rebuild URL without apiKey in query string
        remaining_qs = urlencode({k: v[0] for k, v in qs.items()}) if qs else ""
        self.server_url = urlunparse(parsed._replace(query=remaining_qs)).rstrip("/")

    def _headers(self) -> dict:
        """Build headers with Authorization if apiKey is available."""
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    async def list_tools(self) -> list[dict]:
        """Fetch available tools from the MCP server."""
        try:
            async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
                # MCP uses JSON-RPC 2.0 over HTTP
                resp = await client.post(
                    self.server_url,
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "tools/list",
                    },
                    headers=self._headers(),
                )
                data = resp.json()

                if "error" in data:
                    err = data["error"]
                    msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
                    raise Exception(f"MCP error: {msg}")

                result = data.get("result", {})
                tools = result.get("tools", []) if isinstance(result, dict) else []
                return [
                    {
                        "name": t.get("name", ""),
                        "description": t.get("description", ""),
                        "inputSchema": t.get("inputSchema", {}),
                    }
                    for t in tools
                ]
        except httpx.HTTPError as e:
            raise Exception(f"Connection failed: {str(e)[:200]}")

    async def call_tool(self, tool_name: str, arguments: dict) -> str:
        """Execute a tool on the MCP server."""
        try:
            async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
                resp = await client.post(
                    self.server_url,
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "tools/call",
                        "params": {
                            "name": tool_name,
                            "arguments": arguments,
                        },
                    },
                    headers=self._headers(),
                )
                data = resp.json()

                if "error" in data:
                    err = data["error"]
                    msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
                    return f"❌ MCP 工具执行错误: {msg[:200]}"

                result = data.get("result", {})
                # Some servers return a plain string instead of a structured result
                if isinstance(result, str):
                    return result

                # MCP returns content as list of content blocks
                content_blocks = result.get("content", []) if isinstance(result, dict) else []
                texts = []
                for block in content_blocks:
                    if isinstance(block, str):
                        texts.append(block)
                    elif isinstance(block, dict):
                        if block.get("type") == "text":
                            texts.append(block.get("text", ""))
                        elif block.get("type") == "image":
                            texts.append(f"[图片: {block.get('mimeType', 'image')}]")
                        else:
                            texts.append(str(block))
                    else:
                        texts.append(str(block))

                return "\n".join(texts) if texts else str(result)

        except httpx.HTTPError as e:
            return f"❌ MCP 连接失败: {str(e)[:200]}"
