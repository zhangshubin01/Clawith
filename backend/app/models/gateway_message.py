"""Gateway messages for OpenClaw agent communication."""

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import DateTime, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


def _default_expires_at() -> datetime:
    """消息默认过期时间：创建后 7 天。

    用于 GatewayMessage.expires_at 的 Python 层默认值，
    确保通过 ORM 创建的消息自动获得过期时间。
    """
    return datetime.now(timezone.utc) + timedelta(days=7)


class GatewayMessage(Base):
    """Message queued for delivery to an OpenClaw agent.

    Lifecycle: pending → delivered → completed (or expired).
    """

    __tablename__ = "gateway_messages"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # Target OpenClaw agent
    agent_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("agents.id"), nullable=False)
    # Sender (one of these may be None)
    sender_agent_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("agents.id"))
    sender_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    # Chat session tracking for routing responses back
    conversation_id: Mapped[str | None] = mapped_column(String(100))
    # Message content
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # Status tracking
    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)  # pending | delivered | completed
    result: Mapped[str | None] = mapped_column(Text)
    # 消息过期时间：创建后 7 天自动过期，防止离线节点消息永久堆积
    # None 表示永不过期（仅用于已迁移的历史数据，新消息始终设置此值）
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=_default_expires_at)
    # Timestamps
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
