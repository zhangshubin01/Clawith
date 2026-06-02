from contextvars import ContextVar
from typing import Any

from loguru import logger

current_acp_handler: ContextVar[Any | None] = ContextVar("current_acp_handler", default=None)
_installed = False


def install_acp_tool_hooks() -> None:
    global _installed
    if _installed:
        return
    _installed = True
    logger.info("[ACP] tool bridge installed")
