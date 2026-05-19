"""Notification service — unified entry point for sending in-app notifications.

特性：
- 去重：60 秒内相同 (recipient, type, ref_id) 的通知自动跳过（#NEW-028）
- 广播限流：无 ref_id 的通知（如 broadcast 类型）每接收方每分钟最多 10 条（#NEW-029）
"""

import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.notification import Notification

# 广播通知频率限制：每接收方每分钟最多 10 条无 ref_id 的通知（#NEW-029）
# 使用进程内存存储，服务重启后清空（可接受，极限情况下丢失的是限流状态而非数据）
# 格式: {recipient_key: [timestamp, ...]}
_broadcast_rate_limit: dict[str, list[float]] = {}
_BROADCAST_MAX_PER_MINUTE = 10
_BROADCAST_WINDOW_SECONDS = 60


def _cleanup_expired_rate_limits():
    """清理过期的限流记录，防止内存无限增长。"""
    now = time.time()
    expired_keys = []
    for key, timestamps in _broadcast_rate_limit.items():
        # 移除窗口外的旧时间戳
        _broadcast_rate_limit[key] = [t for t in timestamps if now - t < _BROADCAST_WINDOW_SECONDS]
        if not _broadcast_rate_limit[key]:
            expired_keys.append(key)
    for key in expired_keys:
        del _broadcast_rate_limit[key]


async def send_notification(
    db: AsyncSession,
    user_id: Optional[uuid.UUID] = None,
    *,
    agent_id: Optional[uuid.UUID] = None,
    type: str,
    title: str,
    body: str = "",
    link: Optional[str] = None,
    ref_id: Optional[uuid.UUID] = None,
    sender_name: Optional[str] = None,
) -> Optional[Notification]:
    """Create and persist a notification for a user or an agent.

    Args:
        db: Database session.
        user_id: The user who should receive this notification (for human recipients).
        agent_id: The agent who should receive this notification (for agent recipients).
        type: Notification category (approval_pending, plaza_comment, mention, broadcast, etc.).
        title: Short summary shown in the notification list.
        body: Extended detail text.
        link: Frontend route path for click-through navigation.
        ref_id: ID of the related object (approval, comment, etc.).
        sender_name: Display name of the sender.

    Returns:
        The created Notification, or None if deduplicated/rate-limited.
    """
    if not user_id and not agent_id:
        raise ValueError("Either user_id or agent_id must be provided")

    # 去重检查（#NEW-028）：60秒内相同 (recipient, type, ref_id) 的通知跳过
    if ref_id is not None:
        existing = await db.execute(
            select(Notification).where(
                Notification.type == type,
                Notification.ref_id == ref_id,
                Notification.agent_id == agent_id,
                Notification.user_id == user_id,
                Notification.created_at >= datetime.now(timezone.utc) - timedelta(seconds=60),
            )
        )
        if existing.scalar_one_or_none():
            logger.debug(f"Notification [{type}] deduplicated for ref_id={ref_id}")
            return None

    # 广播频率限制（#NEW-029）：无 ref_id 的通知（如 broadcast 类型）按接收方限流
    # 每接收方每分钟最多 10 条，防止广播风暴耗尽数据库连接
    if ref_id is None:
        recipient_key = f"u:{user_id}" if user_id else f"a:{agent_id}"
        now = time.time()
        # 定期清理过期记录（每 100 次调用执行一次）
        if len(_broadcast_rate_limit) > 0 and hash(recipient_key) % 100 == 0:
            _cleanup_expired_rate_limits()
        timestamps = _broadcast_rate_limit.get(recipient_key, [])
        timestamps = [t for t in timestamps if now - t < _BROADCAST_WINDOW_SECONDS]
        if len(timestamps) >= _BROADCAST_MAX_PER_MINUTE:
            logger.warning(
                f"Notification [{type}] rate-limited for {recipient_key} "
                f"({len(timestamps)} in {_BROADCAST_WINDOW_SECONDS}s)"
            )
            return None
        timestamps.append(now)
        _broadcast_rate_limit[recipient_key] = timestamps

    notif = Notification(
        user_id=user_id,
        agent_id=agent_id,
        type=type,
        title=title,
        body=body,
        link=link,
        ref_id=ref_id,
        sender_name=sender_name,
    )
    db.add(notif)
    await db.flush()
    recipient = f"user {user_id}" if user_id else f"agent {agent_id}"
    logger.info(f"Notification [{type}] sent to {recipient}: {title}")
    return notif

