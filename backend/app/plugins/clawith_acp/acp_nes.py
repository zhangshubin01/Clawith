"""NES 占位 — 无独立 NES 服务时返回空 suggestions。"""

from __future__ import annotations

import os
import uuid
from typing import Any

from loguru import logger

_nes_sessions: dict[str, str] = {}


def nes_enabled() -> bool:
    return os.getenv("ACP_NES_ENABLED", "0").strip().lower() in {"1", "true", "yes", "on"}


async def handle_nes_start(session_id: str | None, params: dict[str, Any]) -> dict[str, Any]:
    if not session_id:
        raise ValueError("session_id required")
    nes_id = str(uuid.uuid4())
    _nes_sessions[session_id] = nes_id
    logger.info("[ACP-NES] start session={} nes_session={}", session_id[:8], nes_id[:8])
    return {"nesSessionId": nes_id}


async def handle_nes_suggest(session_id: str | None, params: dict[str, Any]) -> dict[str, Any]:
    if not nes_enabled():
        logger.debug("[ACP-NES] suggest 降级空列表 session={}", (session_id or "?")[:8])
        return {"suggestions": []}
    logger.warning("[ACP-NES] ACP_NES_ENABLED=1 但无 NES 后端，仍返回空列表")
    return {"suggestions": []}


async def handle_nes_close(session_id: str | None, params: dict[str, Any]) -> dict[str, Any]:
    if session_id:
        _nes_sessions.pop(session_id, None)
    logger.info("[ACP-NES] close session={}", (session_id or "?")[:8])
    return {"closed": True}


def handle_nes_accept_reject(session_id: str | None, method: str, params: dict[str, Any]) -> None:
    logger.info("[ACP-NES] {} session={} keys={}", method, (session_id or "?")[:8], list(params.keys()))
