"""Clawith IDE Bridge Plugin - Enables deep integration with IDEA plugins."""

from fastapi import FastAPI
from app.plugins.base import ClawithPlugin


class ClawithIdeBridgePlugin(ClawithPlugin):
    name = "clawith-ide-bridge"
    version = "1.0.0"
    description = "Provides bidirectional tool calling, context sync, and diff preview for IDEA plugins."

    def register(self, app: FastAPI) -> None:
        from app.plugins.clawith_ide_bridge.router import router
        app.include_router(router)


plugin = ClawithIdeBridgePlugin()
