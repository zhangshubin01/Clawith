"""Clawith ACP Plugin — WebSocket bridge for IDE thin clients."""

from fastapi import FastAPI

from app.plugins.base import ClawithPlugin


class ClawithAcpPlugin(ClawithPlugin):
    name = "clawith-acp"
    version = "0.2.0"
    description = "Agent Client Protocol (ACP) remote WebSocket for IDE thin clients"

    def register(self, app: FastAPI) -> None:
        from app.plugins.clawith_acp.router import install_acp_tool_hooks, router

        # Idempotent; prefer explicit install here over relying on import side effects only.
        install_acp_tool_hooks()

        # Must match integrations/clawith-ide-acp/server.py WebSocket path
        app.include_router(router, prefix="/api/plugins/clawith-acp")


plugin = ClawithAcpPlugin()
