"""Clawith ACP Plugin — WebSocket bridge for IDE thin clients."""

from fastapi import FastAPI

from app.plugins.base import ClawithPlugin


class ClawithAcpPlugin(ClawithPlugin):
    name = "clawith-acp"
    version = "0.2.0"
    description = "Agent Client Protocol (ACP) remote WebSocket for IDE thin clients"

    def register(self, app: FastAPI) -> None:
        from app.plugins.clawith_acp.router import router

        # Must match clawith_acp/server.py (thin client) ws path
        app.include_router(router, prefix="/api/plugins/clawith-acp")


plugin = ClawithAcpPlugin()
