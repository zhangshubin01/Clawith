"""clawith-acp - Agent Client Protocol (ACP) plugin for JetBrains IDE integration.

This plugin provides a WebSocket endpoint for JetBrains IDE (IDEA/Android Studio) to connect to
Clawith as an AI backend. It supports:
- Text/image/audio multi-modal prompts
- IDE file system proxy (ide_read_file/ide_write_file etc)
- Terminal command execution in IDE
- Structured diff preview for file editing with permission approval
- Cancellation support for running prompts
- Dynamic model/mode selection
"""
from __future__ import annotations

from typing import ClassVar

from fastapi import FastAPI
from app.plugins.base import ClawithPlugin

from .router import router

class ClawithAcpPlugin(ClawithPlugin):
    name: ClassVar[str] = "clawith-acp"
    version: ClassVar[str] = "0.2.0"
    description: ClassVar[str] = "Agent Client Protocol (ACP) Remote WebSocket Server"

    def register(self, app: FastAPI) -> None:
        """Register the plugin with the FastAPI app."""
        # Register API routes
        app.include_router(router, prefix="/api/plugins/clawith-acp", tags=["acp"])

plugin = ClawithAcpPlugin()
