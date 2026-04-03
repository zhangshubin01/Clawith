# backend/app/plugins/clawith_mcp/__init__.py
from fastapi import FastAPI
from app.plugins.base import ClawithPlugin


class ClawithMcpPlugin(ClawithPlugin):
    name = "clawith-mcp"
    version = "1.0.0"
    description = "MCP server (stdio+HTTP) and OpenAI-compatible API for Clawith agents"

    def register(self, app: FastAPI) -> None:
        from app.plugins.clawith_mcp.router import router
        app.include_router(router)


plugin = ClawithMcpPlugin()
