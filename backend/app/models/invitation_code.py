"""Invitation code model for registration gating."""

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


def _default_invitation_expires_at() -> datetime:
    """邀请码默认过期时间：创建后 30 天。

    用于 InvitationCode.expires_at 的 Python 层默认值，
    确保通过 ORM 创建的邀请码自动获得过期时间。
    """
    return datetime.now(timezone.utc) + timedelta(days=30)


class InvitationCode(Base):
    """An invitation code that can be used to register new accounts."""

    __tablename__ = "invitation_codes"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    code: Mapped[str] = mapped_column(String(32), unique=True, nullable=False, index=True)
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=True, index=True)
    max_uses: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    used_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # 邀请码过期时间：创建后 30 天自动过期，防止旧邀请码被滥用
    # None 表示永不过期（仅用于已迁移的历史数据，新邀请码始终设置此值）
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=_default_invitation_expires_at)
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
