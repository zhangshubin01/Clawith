"""前端日志上报端点 — 接收前端 console.log 批量上报到后端日志系统。

端点:
  POST /api/log/frontend  — 接收批量前端日志
"""

from fastapi import APIRouter, Request
from loguru import logger
from pydantic import BaseModel
from typing import Literal

router = APIRouter(prefix="/api/log", tags=["log"])


class FrontendLogEntry(BaseModel):
    level: Literal["info", "warn", "error"]
    module: str  # 'WS' | 'Stream' | 'Render' | 'App' | 'Auth'
    message: str
    data: dict | None = None
    ts: int = 0  # 前端时间戳 (ms)


class FrontendLogBatch(BaseModel):
    logs: list[FrontendLogEntry]


@router.post("/frontend")
async def receive_frontend_logs(request: Request, batch: FrontendLogBatch):
    """接收前端批量日志并写入后端 loguru 日志系统。

    前端通过 navigator.sendBeacon / fetch keepalive 上报,
    每 5s 或 50 条批量一次, error 级别立即上报。
    """
    for entry in batch.logs:
        prefix = f"[Frontend-{entry.module}]"
        extra = entry.data or {}

        if entry.level == "error":
            logger.error(f"{prefix} {entry.message} | {extra}")
        elif entry.level == "warn":
            logger.warning(f"{prefix} {entry.message} | {extra}")
        else:
            # info/debug → 减少日志量, 仅打 info
            logger.info(f"{prefix} {entry.message} | {extra}")

    return {"received": len(batch.logs)}
