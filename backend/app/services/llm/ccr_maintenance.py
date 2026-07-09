"""CCR 后台维护：过期 purge + metrics 周期日志。"""

from __future__ import annotations

import asyncio

from loguru import logger


async def ccr_maintenance_loop() -> None:
    """lifespan 后台任务：定期 purge 过期 CCR 并打 metrics。"""
    from app.config import get_settings

    while True:
        settings = get_settings()
        interval = max(int(getattr(settings, "CTX_CCR_PURGE_INTERVAL_SEC", 3600)), 60)
        await asyncio.sleep(interval)
        try:
            from app.services.llm.ccr_store import log_ccr_metrics, purge_expired

            deleted = await purge_expired()
            if deleted:
                logger.info("[CTX-CCR] maintenance purge deleted={}", deleted)
            if getattr(settings, "CTX_CCR_METRICS_LOG", True):
                log_ccr_metrics()
        except Exception as e:
            logger.warning("[CTX-CCR] maintenance loop failed err={}", e)
