"""后台清理任务：定期清理过期审计日志和活动日志。

问题 #NEW-015：audit_logs 和 agent_activity_logs 无自动清理机制，数据无限增长。
修复：每 6 小时运行一次，删除超过 90 天的记录。
"""

import asyncio
from datetime import datetime, timedelta, timezone

from loguru import logger
from sqlalchemy import delete, text

from app.database import async_session

# 保留天数：超过此天数的记录将被清理
RETENTION_DAYS = 90
# 清理间隔（秒）：每 6 小时执行一次
CLEANUP_INTERVAL_SECONDS = 6 * 3600


async def start_audit_log_cleanup():
    """后台任务：定期清理过期的审计日志和活动日志。

    清理策略：
    - audit_logs: 保留 90 天
    - agent_activity_logs: 保留 90 天
    - 使用 DELETE + LIMIT 分批删除，避免长事务锁表
    - 每批最多删除 10000 条
    """
    # 服务启动后延迟 5 分钟再开始第一次清理
    await asyncio.sleep(300)

    while True:
        try:
            cutoff = datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)
            total_deleted = 0

            # 分批清理 audit_logs
            async with async_session() as db:
                while True:
                    result = await db.execute(
                        text(
                            "DELETE FROM audit_logs WHERE created_at < :cutoff AND ctid IN ("
                            "  SELECT ctid FROM audit_logs WHERE created_at < :cutoff LIMIT 10000"
                            ")"
                        ),
                        {"cutoff": cutoff},
                    )
                    deleted = result.rowcount
                    if deleted == 0:
                        break
                    total_deleted += deleted
                    await db.commit()

            # 分批清理 agent_activity_logs
            async with async_session() as db:
                while True:
                    result = await db.execute(
                        text(
                            "DELETE FROM agent_activity_logs WHERE created_at < :cutoff AND ctid IN ("
                            "  SELECT ctid FROM agent_activity_logs WHERE created_at < :cutoff LIMIT 10000"
                            ")"
                        ),
                        {"cutoff": cutoff},
                    )
                    deleted = result.rowcount
                    if deleted == 0:
                        break
                    total_deleted += deleted
                    await db.commit()

            if total_deleted > 0:
                logger.info(
                    f"[AuditCleanup] Deleted {total_deleted} records older than "
                    f"{RETENTION_DAYS} days (cutoff: {cutoff.isoformat()})"
                )
        except Exception:
            logger.exception("[AuditCleanup] Cleanup iteration failed")

        await asyncio.sleep(CLEANUP_INTERVAL_SECONDS)
