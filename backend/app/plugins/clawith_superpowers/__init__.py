from __future__ import annotations

from typing import ClassVar

from fastapi import FastAPI
from app.plugins.base import ClawithPlugin

from .skill_manager import SkillManager
from .routes import router


class ClawithSuperpowersPlugin(ClawithPlugin):
    name: ClassVar[str] = "clawith-superpowers"
    version: ClassVar[str] = "1.0.0"
    description: ClassVar[str] = (
        "Superpowers agentic skills framework integration - "
        "provides structured development workflows for Clawith agents"
    )

    def register(self, app: FastAPI) -> None:
        """Register the plugin with the FastAPI app."""
        # Register API routes
        app.include_router(router, prefix="/api/plugins/clawith-superpowers", tags=["superpowers"])


plugin = ClawithSuperpowersPlugin()
